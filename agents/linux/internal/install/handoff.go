package install

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// The handoff between the unprivileged agent and the privileged update unit.
//
// # The problem this solves
//
// The agent service runs as an unprivileged account. It therefore cannot
// replace a root-owned binary or restart its own unit, so it cannot install an
// update — the first real run of `scripts/update-e2e.sh` ended with
// "upgrading needs root and this process is uid 996" after everything else
// had worked.
//
// The agent does the parts it can do safely and unprivileged: fetch the
// archive over TLS, bound the download, hash it, verify the publisher's
// signature. Then it writes a request, and a root-owned one-shot unit does the
// one privileged thing.
//
// # Why a compromised agent account cannot install arbitrary code
//
// This is the question that decides whether the design is acceptable, because
// the agent account can create the request file whenever it likes.
//
// The request carries **the publisher's signed release metadata and its
// detached signature** — not a digest. [ApplyUpdateRequest] verifies that
// signature against the trust store compiled into the binary, and takes the
// expected SHA-256 out of the verified metadata. There is no digest field to
// forge, and no path by which the request's author chooses what the bytes
// should hash to.
//
// So the privilege this grants is exactly: "cause root to install a genuinely
// published, publisher-signed release of this agent". Not "run arbitrary
// code as root". A fleet-wide compromise through this channel needs the
// publisher's signing key, which is the property the whole trust model is
// built to give.
//
// Everything else is defence in depth and is still enforced:
//
//   - The archive must be a regular file — checked with lstat, so a symlink
//     is refused rather than followed — inside the agent's own state
//     directory.
//   - The request must be a regular file, not world-writable, and must parse
//     with no unknown fields.
//   - Every string is length-bounded before it is used.
//
// # Why not setuid, polkit or D-Bus
//
// A setuid helper is a new attack surface and a famously difficult thing to
// get right. Polkit rules are per-distribution configuration. A D-Bus service
// is a daemon to write and harden. A path unit is none of those: systemd is
// already pid 1, already watches paths, and the trigger is a file an
// administrator can read.

// HandoffSchemaVersion is the shape of the request file.
//
// Checked rather than assumed, because the reader may be a *newer* binary
// than the writer: an update that replaces the agent mid-flight means the
// request was written by the old version and could be read by the new one.
const HandoffSchemaVersion = 1

// Bounds on everything read across the privilege boundary.
//
// Checked before use rather than trusted, because the writer is less
// privileged than the reader. The sizes are generous for real values and
// small enough that a malformed file is a refusal rather than an allocation.
const (
	// MaxHandoffBytes bounds the request file. A manifest for a handful of
	// versions and three architectures is a couple of kilobytes.
	MaxHandoffBytes = 512 * 1024
	maxVersionChars = 64
	maxPlatformChar = 64
	maxPathChars    = 4096
	maxIDChars      = 64
)

// UpdateRequest is the typed, allow-listed instruction the agent leaves for
// the privileged unit.
//
// **There is nothing executable here, and no digest.** Every field is data
// describing a release the publisher signed, plus where the agent staged the
// archive. What the bytes must hash to is derived from Metadata, whose
// signature the reader verifies.
type UpdateRequest struct {
	SchemaVersion int `json:"schema_version"`
	// InstructionID is the controller's authorization, carried through so the
	// outcome can be matched to it and so the journal lines join up.
	InstructionID string `json:"instruction_id"`
	// Version is the release to install. Must appear in Metadata.
	Version string `json:"version"`
	// Platform is GOOS/GOARCH, used to select the archive in Metadata.
	Platform string `json:"platform"`
	// ArchivePath is where the agent staged the downloaded archive. Must be a
	// regular file inside the agent's state directory.
	ArchivePath string `json:"archive_path"`
	// Metadata is the release manifest's exact signed bytes.
	Metadata string `json:"metadata"`
	// Signature is the detached signature document over Metadata.
	Signature string `json:"signature"`
	// RequestedAt is RFC 3339, for the journal. Not used for any decision:
	// the controller's expiry was already enforced by the agent, and a
	// timestamp the requester controls is not evidence of anything.
	RequestedAt string `json:"requested_at"`
}

// UpdateOutcome is what the privileged unit writes back.
//
// The agent is usually dead by the time this is written — the install ends by
// restarting it — so this exists for the case where it is not: an install
// that failed before the restart. The replacement agent reads it on startup
// and can report a failure the controller would otherwise wait out.
type UpdateOutcome struct {
	SchemaVersion int    `json:"schema_version"`
	InstructionID string `json:"instruction_id"`
	// State is one of the protocol's update states. A string rather than a
	// typed enum because internal/install must not depend on the protocol
	// package: the privileged installer has no business knowing about the
	// socket.
	State string `json:"state"`
	// Version is what is installed now, as the new binary reports it.
	Version string `json:"version"`
	// Error is empty on success.
	Error       string `json:"error,omitempty"`
	CompletedAt string `json:"completed_at"`
}

// Terminal states [ApplyUpdateRequest] records in the outcome file.
//
// Declared beside the Step* constants' siblings in upgrade.go and for the
// same reason: the strings match hcc.models.updates.UpdateState, and this
// package must not depend on the wire format to say what happened.
const (
	StateSucceeded  = "succeeded"
	StateFailed     = "failed"
	StateRolledBack = "rolled_back"
)

// ErrNoUpdateRequest is returned when there is nothing to do.
//
// Distinguished from a malformed request because the two need different exit
// behaviour: nothing to do is success, and a malformed request is a refusal
// worth a non-zero exit and a journal line.
var ErrNoUpdateRequest = errors.New("no update request")

// WriteUpdateRequest stages a request atomically.
//
// Written to a temporary name in the same directory and renamed, so the path
// unit's PathExists trigger cannot see a partially written file. The directory
// must already exist: it is created by the service's StateDirectory=, and
// creating it here would mean the agent could place it with the wrong owner.
func WriteUpdateRequest(path string, request UpdateRequest) error {
	request.SchemaVersion = HandoffSchemaVersion
	if request.RequestedAt == "" {
		request.RequestedAt = time.Now().UTC().Format(time.RFC3339)
	}
	payload, err := json.MarshalIndent(request, "", "  ")
	if err != nil {
		return fmt.Errorf("cannot encode the update request: %w", err)
	}
	return writeFileAtomically(path, append(payload, '\n'), HandoffFileMode)
}

// ReadUpdateRequest reads and validates a request, refusing anything odd.
//
// Every check here runs on the less-privileged side's input. The expensive
// one — verifying the publisher's signature — is in [ApplyUpdateRequest],
// because it needs a trust store and this does not.
func ReadUpdateRequest(path string) (UpdateRequest, error) {
	info, err := os.Lstat(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return UpdateRequest{}, ErrNoUpdateRequest
	case err != nil:
		return UpdateRequest{}, fmt.Errorf("cannot read %s: %w", path, err)
	}
	// Lstat, so a symlink is refused rather than followed. A root process
	// following a link planted by a less-privileged account is the oldest
	// mistake in this family.
	if !info.Mode().IsRegular() {
		return UpdateRequest{}, fmt.Errorf(
			"%s is not a regular file (mode %s); refusing to read it", path, info.Mode(),
		)
	}
	if info.Mode().Perm()&0o002 != 0 {
		return UpdateRequest{}, fmt.Errorf(
			"%s is world-writable (mode %#o); refusing to act on it", path, info.Mode().Perm(),
		)
	}
	if info.Size() > MaxHandoffBytes {
		return UpdateRequest{}, fmt.Errorf(
			"%s is %d bytes, over the %d byte limit", path, info.Size(), MaxHandoffBytes,
		)
	}

	payload, err := os.ReadFile(path) //nolint:gosec // lstat'ed above; path is a constant
	if err != nil {
		return UpdateRequest{}, fmt.Errorf("cannot read %s: %w", path, err)
	}

	decoder := json.NewDecoder(strings.NewReader(string(payload)))
	// An unknown field may be load-bearing in whatever wrote it, and this is
	// a privilege boundary: ignoring something is how a check gets skipped.
	decoder.DisallowUnknownFields()
	var request UpdateRequest
	if err := decoder.Decode(&request); err != nil {
		return UpdateRequest{}, fmt.Errorf("%s is not a usable update request: %w", path, err)
	}
	if err := request.validate(); err != nil {
		return UpdateRequest{}, fmt.Errorf("%s is not a usable update request: %w", path, err)
	}
	return request, nil
}

// validate checks the shape, never the authenticity.
func (r UpdateRequest) validate() error {
	if r.SchemaVersion != HandoffSchemaVersion {
		return fmt.Errorf(
			"schema_version is %d; this binary understands %d",
			r.SchemaVersion, HandoffSchemaVersion,
		)
	}
	for _, field := range []struct {
		name  string
		value string
		limit int
	}{
		{"instruction_id", r.InstructionID, maxIDChars},
		{"version", r.Version, maxVersionChars},
		{"platform", r.Platform, maxPlatformChar},
		{"archive_path", r.ArchivePath, maxPathChars},
	} {
		if field.value == "" {
			return fmt.Errorf("%s is empty", field.name)
		}
		if len(field.value) > field.limit {
			return fmt.Errorf(
				"%s is %d characters, over the %d limit", field.name, len(field.value), field.limit,
			)
		}
	}
	if r.Metadata == "" {
		return errors.New("metadata is empty, so nothing establishes what this release is")
	}
	if r.Signature == "" {
		return errors.New("signature is empty, so the metadata cannot be verified")
	}
	if _, err := version.Parse(r.Version); err != nil {
		return fmt.Errorf("version %q is not a semantic version: %w", r.Version, err)
	}
	return nil
}

// ApplyOptions is everything [ApplyUpdateRequest] needs.
type ApplyOptions struct {
	Paths Paths
	// TrustStore holds the publisher keys this binary trusts. A nil or empty
	// store refuses everything, which is correct for a build nobody signs
	// releases for.
	TrustStore *release.TrustStore
	System     System
	Out        io.Writer
	// ConnectTimeout bounds how long the upgrade waits for the restarted
	// agent to come back before rolling back.
	ConnectTimeout time.Duration
	// Upgrade is injected by tests. Nil means the real upgrader.
	Upgrade func(ctx context.Context, options UpgradeOptions) error
	// Now is injected by tests.
	Now func() time.Time
}

// ApplyUpdateRequest is the privileged half of an update.
//
// Reads the request, **removes it**, verifies the publisher's signature over
// the metadata it carries, derives the expected digest from that verified
// metadata, and runs the ordinary upgrader.
//
// The request is removed before anything is installed, deliberately. The path
// unit's PathExists trigger fires while the file is there, so a crash
// mid-install with the request still present would be an install loop — and
// an install loop on a service that replaces its own binary is the worst
// available failure. Removing first means the cost of a crash is one failed
// update rather than a machine that will not settle.
//
// Returns ErrNoUpdateRequest when there is nothing to do, which is not a
// failure: the unit may be triggered by a request another invocation already
// took.
func ApplyUpdateRequest(ctx context.Context, options ApplyOptions) error {
	if options.Out == nil {
		options.Out = io.Discard
	}
	if options.System == nil {
		options.System = LinuxSystem{Root: options.Paths.Root}
	}
	if options.Upgrade == nil {
		// Built here rather than through NewUpgrader, because the paths and
		// the system have to be the ones this call was given: a test runs the
		// whole privileged path against a temporary root, and NewUpgrader
		// hardcodes the real machine.
		options.Upgrade = func(ctx context.Context, upgrade UpgradeOptions) error {
			upgrader := &Upgrader{
				System:  options.System,
				Paths:   options.Paths,
				Options: upgrade,
				out:     upgrade.Out,
			}
			return upgrader.Run(ctx)
		}
	}
	if options.Now == nil {
		options.Now = time.Now
	}
	if options.ConnectTimeout == 0 {
		options.ConnectTimeout = DefaultConnectTimeout
	}

	requestPath := options.Paths.UpdateRequest()
	request, err := ReadUpdateRequest(requestPath)
	if err != nil {
		if errors.Is(err, ErrNoUpdateRequest) {
			return err
		}
		// A malformed request is removed too. Leaving it would re-trigger the
		// path unit for ever on a file that will never be usable.
		_ = os.Remove(requestPath)
		return err
	}
	if err := os.Remove(requestPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		// Refusing to proceed is the right call: an install that cannot clear
		// its own trigger is an install loop waiting to happen.
		return fmt.Errorf("cannot remove %s, so refusing to install: %w", requestPath, err)
	}

	fmt.Fprintf(options.Out, "Update request for %s (instruction %s)\n",
		request.Version, request.InstructionID)

	artifact, err := request.verify(options.TrustStore)
	if err != nil {
		options.writeOutcome(request, StateFailed, "", err.Error())
		return err
	}
	fmt.Fprintf(options.Out, "  signature verified; expecting sha256 %s\n", artifact.SHA256)

	archive, err := options.resolveArchive(request.ArchivePath)
	if err != nil {
		options.writeOutcome(request, StateFailed, "", err.Error())
		return err
	}

	upgradeErr := options.Upgrade(ctx, UpgradeOptions{
		ArchivePath:    archive,
		ExpectedSHA256: artifact.SHA256,
		Out:            options.Out,
		ConnectTimeout: options.ConnectTimeout,
	})
	// Removed whatever happened: a verified archive left in the state
	// directory is a few megabytes and a puzzle for whoever finds it.
	if err := os.Remove(archive); err != nil && !errors.Is(err, os.ErrNotExist) {
		fmt.Fprintf(options.Out, "  ! could not remove the staged archive: %v\n", err)
	}

	if upgradeErr != nil {
		state := StateFailed
		// The upgrader's own error says whether the previous binary was
		// restored, and the two need different reactions from an operator.
		if strings.Contains(upgradeErr.Error(), "rolled back") ||
			strings.Contains(upgradeErr.Error(), "rolling back") {
			state = StateRolledBack
		}
		options.writeOutcome(request, state, "", upgradeErr.Error())
		return upgradeErr
	}

	options.writeOutcome(request, StateSucceeded, request.Version, "")
	return nil
}

// verify establishes what the bytes must be, from metadata the publisher
// signed.
//
// The whole security argument of this file is in these fifteen lines: the
// digest is taken from verified metadata, so the request's author does not
// choose it.
func (r UpdateRequest) verify(store *release.TrustStore) (release.Platform, error) {
	verification := release.VerifyMetadata([]byte(r.Metadata), []byte(r.Signature), store)
	if !verification.Authentic() {
		return release.Platform{}, fmt.Errorf(
			"the release metadata is not authentic (%s): %s",
			verification.Status, verification.Detail,
		)
	}
	manifest, err := release.ParseManifest([]byte(r.Metadata))
	if err != nil {
		return release.Platform{}, fmt.Errorf("the verified metadata is not usable: %w", err)
	}
	artifact, ok := manifest.PlatformFor(r.Version, r.Platform)
	if !ok {
		return release.Platform{}, fmt.Errorf(
			"the verified metadata has no %s archive for version %s", r.Platform, r.Version,
		)
	}
	return artifact, nil
}

// resolveArchive refuses anything that is not a plain file the agent staged.
//
// Constrained to the state directory because there is no legitimate reason
// for it to be anywhere else, and because a root process that will read any
// path a less-privileged account names is a root process doing that account's
// filesystem reconnaissance for it. The digest check is what makes the
// contents safe; this is what keeps the *path* uninteresting.
func (o ApplyOptions) resolveArchive(named string) (string, error) {
	cleaned := filepath.Clean(named)
	if !filepath.IsAbs(cleaned) {
		return "", fmt.Errorf("archive_path %q is not absolute", named)
	}
	stateDir := filepath.Clean(o.Paths.StateDir())
	if cleaned != stateDir && !strings.HasPrefix(cleaned, stateDir+string(filepath.Separator)) {
		return "", fmt.Errorf(
			"archive_path %q is outside the agent's state directory %s", named, stateDir,
		)
	}
	info, err := os.Lstat(cleaned)
	if err != nil {
		return "", fmt.Errorf("cannot stat the staged archive %s: %w", cleaned, err)
	}
	// Lstat again: a symlink inside the state directory pointing at /etc/shadow
	// would otherwise be hashed, fail the digest check, and be reported — but
	// it would have been *read*, which is more than this needs to allow.
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf(
			"the staged archive %s is not a regular file (mode %s)", cleaned, info.Mode(),
		)
	}
	return cleaned, nil
}

// writeOutcome records what happened, best-effort.
//
// Best-effort because the agent is usually already gone — the install ends by
// restarting it — so a failure to write this costs nothing in the common
// case, and refusing to finish an otherwise successful install because a
// bookkeeping file would not write would be absurd.
func (o ApplyOptions) writeOutcome(request UpdateRequest, state, version, failure string) {
	outcome := UpdateOutcome{
		SchemaVersion: HandoffSchemaVersion,
		InstructionID: request.InstructionID,
		State:         state,
		Version:       version,
		Error:         failure,
		CompletedAt:   o.Now().UTC().Format(time.RFC3339),
	}
	payload, err := json.MarshalIndent(outcome, "", "  ")
	if err != nil {
		fmt.Fprintf(o.Out, "  ! could not encode the outcome: %v\n", err)
		return
	}
	path := o.Paths.UpdateOutcome()
	if err := writeFileAtomically(path, append(payload, '\n'), HandoffFileMode); err != nil {
		fmt.Fprintf(o.Out, "  ! could not write %s: %v\n", path, err)
	}
}

// ReadUpdateOutcome reads what the privileged unit last reported, or nil.
//
// Used by a restarted agent to find out how the install that replaced it
// ended. A missing file is the ordinary case and is not an error.
func ReadUpdateOutcome(path string) (*UpdateOutcome, error) {
	payload, err := os.ReadFile(path) //nolint:gosec // a constant path under /var/lib
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil //nolint:nilnil // absence is not an error here
	}
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	if len(payload) > MaxHandoffBytes {
		return nil, fmt.Errorf("%s is over the %d byte limit", path, MaxHandoffBytes)
	}
	decoder := json.NewDecoder(strings.NewReader(string(payload)))
	decoder.DisallowUnknownFields()
	var outcome UpdateOutcome
	if err := decoder.Decode(&outcome); err != nil {
		return nil, fmt.Errorf("%s is not a usable outcome: %w", path, err)
	}
	if outcome.SchemaVersion != HandoffSchemaVersion {
		return nil, fmt.Errorf(
			"%s has schema_version %d; this binary understands %d",
			path, outcome.SchemaVersion, HandoffSchemaVersion,
		)
	}
	return &outcome, nil
}
