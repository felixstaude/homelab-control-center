package install

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
)

// The privileged half of an update, and the boundary it sits on.
//
// Every test below is about one question: can the unprivileged agent account,
// which writes the request file, make root install something the publisher did
// not sign? The answer has to be no, and the reason has to be structural
// rather than a check somebody could remove — the digest is taken from
// metadata whose signature root verifies, so there is no digest field to
// forge.

// The same deterministic key the contract tests use, so a signature accepted
// here is one the controller's verifier accepts too.
var handoffSeed = []byte("hcc-contract-test-key-do-not-use")

func handoffKey(t *testing.T) string {
	t.Helper()
	private := ed25519.NewKeyFromSeed(handoffSeed)
	return base64.StdEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
}

func handoffStore(t *testing.T) *release.TrustStore {
	t.Helper()
	store, err := release.NewTrustStore([]string{handoffKey(t)})
	if err != nil {
		t.Fatalf("cannot build the trust store: %v", err)
	}
	return store
}

func signHandoff(t *testing.T, payload, seed []byte) string {
	t.Helper()
	private := ed25519.NewKeyFromSeed(seed)
	digest := sha256.Sum256(private.Public().(ed25519.PublicKey))
	document, err := json.Marshal(map[string]string{
		"algorithm": "ed25519",
		"key_id":    hex.EncodeToString(digest[:])[:release.KeyIDLength],
		"signature": base64.StdEncoding.EncodeToString(ed25519.Sign(private, payload)),
	})
	if err != nil {
		t.Fatalf("cannot encode the signature: %v", err)
	}
	return string(document)
}

// handoffManifest is a v2 manifest naming one version and one archive.
func handoffManifest(version, digest string, size int) string {
	return fmt.Sprintf(`{
  "schema_version": 2,
  "recommended": %q,
  "minimum_supported": %q,
  "channels": {"stable": %q},
  "releases": {
    %q: {
      "channel": "stable",
      "protocol_versions": [1, 2],
      "platforms": {
        "linux/amd64": {
          "archive": "hcc-agent_%s_linux_amd64.tar.gz",
          "url": "https://example.test/hcc-agent_%s_linux_amd64.tar.gz",
          "sha256": %q,
          "size_bytes": %d,
          "tier": "supported"
        }
      }
    }
  }
}`, version, version, version, version, version, version, digest, size)
}

type handoffFixture struct {
	t        *testing.T
	paths    Paths
	store    *release.TrustStore
	archive  string
	digest   string
	metadata string
	signed   string
	upgraded []UpgradeOptions
	upgrade  error
	output   *strings.Builder
}

func newHandoffFixture(t *testing.T) *handoffFixture {
	t.Helper()
	root := t.TempDir()
	paths := Paths{Root: root}
	if err := os.MkdirAll(paths.StateDir(), StateDirMode); err != nil {
		t.Fatalf("cannot create the state directory: %v", err)
	}

	payload := []byte("not really a tar archive, but it has a digest")
	archive := filepath.Join(paths.StateDir(), "hcc-agent_0.2.0_linux_amd64.tar.gz")
	if err := os.WriteFile(archive, payload, 0o640); err != nil {
		t.Fatalf("cannot stage the archive: %v", err)
	}
	sum := sha256.Sum256(payload)
	digest := hex.EncodeToString(sum[:])
	metadata := handoffManifest("0.2.0", digest, len(payload))

	return &handoffFixture{
		t:        t,
		paths:    paths,
		store:    handoffStore(t),
		archive:  archive,
		digest:   digest,
		metadata: metadata,
		signed:   signHandoff(t, []byte(metadata), handoffSeed),
		output:   &strings.Builder{},
	}
}

func (f *handoffFixture) request(mutate func(*UpdateRequest)) UpdateRequest {
	request := UpdateRequest{
		SchemaVersion: HandoffSchemaVersion,
		InstructionID: "11111111-2222-3333-4444-555555555555",
		Version:       "0.2.0",
		Platform:      "linux/amd64",
		ArchivePath:   f.archive,
		Metadata:      f.metadata,
		Signature:     f.signed,
		RequestedAt:   "2026-10-14T12:00:00Z",
	}
	if mutate != nil {
		mutate(&request)
	}
	return request
}

func (f *handoffFixture) write(request UpdateRequest) {
	f.t.Helper()
	if err := WriteUpdateRequest(f.paths.UpdateRequest(), request); err != nil {
		f.t.Fatalf("cannot write the request: %v", err)
	}
}

func (f *handoffFixture) apply() error {
	f.t.Helper()
	return ApplyUpdateRequest(context.Background(), ApplyOptions{
		Paths:      f.paths,
		TrustStore: f.store,
		System:     newFakeSystem(),
		Out:        f.output,
		Now:        func() time.Time { return time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC) },
		Upgrade: func(_ context.Context, options UpgradeOptions) error {
			f.upgraded = append(f.upgraded, options)
			return f.upgrade
		},
	})
}

func (f *handoffFixture) outcome() *UpdateOutcome {
	f.t.Helper()
	outcome, err := ReadUpdateOutcome(f.paths.UpdateOutcome())
	if err != nil {
		f.t.Fatalf("cannot read the outcome: %v", err)
	}
	return outcome
}

// --- The happy path ---------------------------------------------------------

func TestTheDigestComesFromTheSignedMetadata(t *testing.T) {
	// The whole security argument in one test. The request names no digest at
	// all; the one the installer enforces is read out of metadata whose
	// signature it verified itself.
	f := newHandoffFixture(t)
	f.write(f.request(nil))

	if err := f.apply(); err != nil {
		t.Fatalf("apply: %v\n%s", err, f.output.String())
	}

	if len(f.upgraded) != 1 {
		t.Fatalf("the installer ran %d times, want once", len(f.upgraded))
	}
	if f.upgraded[0].ExpectedSHA256 != f.digest {
		t.Errorf("expected digest = %q, want %q", f.upgraded[0].ExpectedSHA256, f.digest)
	}
	if f.upgraded[0].ArchivePath != f.archive {
		t.Errorf("archive = %q, want %q", f.upgraded[0].ArchivePath, f.archive)
	}
}

func TestTheRequestIsRemovedBeforeAnythingIsInstalled(t *testing.T) {
	// The path unit's PathExists trigger fires while the file is there, so a
	// crash mid-install with the request still present would be an install
	// loop — on a service that replaces its own binary, which is the worst
	// available failure. Removing first means a crash costs one failed update.
	f := newHandoffFixture(t)
	f.write(f.request(nil))

	var presentDuringInstall bool
	err := ApplyUpdateRequest(context.Background(), ApplyOptions{
		Paths:      f.paths,
		TrustStore: f.store,
		System:     newFakeSystem(),
		Out:        f.output,
		Upgrade: func(context.Context, UpgradeOptions) error {
			_, statErr := os.Stat(f.paths.UpdateRequest())
			presentDuringInstall = statErr == nil
			return nil
		},
	})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}

	if presentDuringInstall {
		t.Error("the request was still present while the install ran")
	}
	if _, statErr := os.Stat(f.paths.UpdateRequest()); statErr == nil {
		t.Error("the request survived the install")
	}
}

func TestASuccessfulInstallRecordsItsOutcome(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(nil))

	if err := f.apply(); err != nil {
		t.Fatalf("apply: %v", err)
	}

	outcome := f.outcome()
	if outcome == nil {
		t.Fatal("no outcome was written")
	}
	if outcome.State != StateSucceeded {
		t.Errorf("state = %q, want %q", outcome.State, StateSucceeded)
	}
	if outcome.Version != "0.2.0" {
		t.Errorf("version = %q, want 0.2.0", outcome.Version)
	}
	if outcome.InstructionID != "11111111-2222-3333-4444-555555555555" {
		t.Errorf("the outcome does not carry the instruction: %q", outcome.InstructionID)
	}
}

func TestTheStagedArchiveIsRemovedAfterwards(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(nil))

	if err := f.apply(); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if _, err := os.Stat(f.archive); err == nil {
		t.Error("the staged archive was left in the state directory")
	}
}

func TestNoRequestIsNotAFailure(t *testing.T) {
	// The path unit can fire for a request another invocation already took,
	// and a unit that reported failure for that would fill the journal with
	// alarming nothing.
	f := newHandoffFixture(t)

	err := f.apply()

	if !errors.Is(err, ErrNoUpdateRequest) {
		t.Errorf("err = %v, want ErrNoUpdateRequest", err)
	}
}

// --- The privilege boundary -------------------------------------------------

func TestAnUnsignedRequestIsRefused(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) {
		r.Signature = `{"algorithm":"ed25519","key_id":"0000000000000000","signature":"` + strings.Repeat("A", 86) + `=="}`
	}))

	err := f.apply()

	if err == nil {
		t.Fatal("a request with a bogus signature was accepted")
	}
	if !strings.Contains(err.Error(), "not authentic") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
	if len(f.upgraded) != 0 {
		t.Error("the installer ran for metadata nothing vouched for")
	}
}

func TestMetadataSignedByAnotherKeyIsRefused(t *testing.T) {
	// The case that matters most: an attacker with write access to the agent
	// account signs their own manifest with their own key. Refused, because
	// the key is not in the store this binary was built with.
	f := newHandoffFixture(t)
	other := []byte("some-other-key-that-is-32-bytes!")
	f.write(f.request(func(r *UpdateRequest) {
		r.Signature = signHandoff(t, []byte(f.metadata), other)
	}))

	err := f.apply()

	if err == nil {
		t.Fatal("metadata signed by an untrusted key was accepted")
	}
	if len(f.upgraded) != 0 {
		t.Error("the installer ran for a release signed by somebody else")
	}
}

func TestMetadataChangedAfterSigningIsRefused(t *testing.T) {
	// Substituting the digest is the obvious attack, so it is the one most
	// worth proving impossible: change the digest in the metadata and the
	// signature over those exact bytes no longer verifies.
	f := newHandoffFixture(t)
	tampered := strings.Replace(f.metadata, f.digest, strings.Repeat("b", 64), 1)
	if tampered == f.metadata {
		t.Fatal("the fixture's digest does not appear in its metadata")
	}
	f.write(f.request(func(r *UpdateRequest) { r.Metadata = tampered }))

	err := f.apply()

	if err == nil {
		t.Fatal("metadata changed after signing was accepted")
	}
	if len(f.upgraded) != 0 {
		t.Error("the installer ran with a substituted digest")
	}
}

func TestAnEmptyTrustStoreRefusesEverything(t *testing.T) {
	// A build nobody stamped a key into. It genuinely cannot tell a real
	// release from a forged one, so it installs nothing.
	f := newHandoffFixture(t)
	f.write(f.request(nil))
	empty, err := release.NewTrustStore(nil)
	if err != nil {
		t.Fatalf("cannot build an empty store: %v", err)
	}

	applyErr := ApplyUpdateRequest(context.Background(), ApplyOptions{
		Paths:      f.paths,
		TrustStore: empty,
		System:     newFakeSystem(),
		Out:        f.output,
		Upgrade: func(context.Context, UpgradeOptions) error {
			t.Error("the installer ran on a build that trusts no key")
			return nil
		},
	})

	if applyErr == nil {
		t.Fatal("a build with no trusted key installed an update")
	}
}

func TestAVersionNotInTheMetadataIsRefused(t *testing.T) {
	// The request chooses which version to install, and the metadata decides
	// whether that is a thing the publisher signed.
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) { r.Version = "9.9.9" }))

	err := f.apply()

	if err == nil {
		t.Fatal("a version the metadata does not name was accepted")
	}
	if !strings.Contains(err.Error(), "no linux/amd64 archive for version 9.9.9") {
		t.Errorf("the refusal does not say what is missing: %v", err)
	}
}

func TestAPlatformNotInTheMetadataIsRefused(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) { r.Platform = "linux/arm64" }))

	err := f.apply()

	if err == nil {
		t.Fatal("a platform the metadata does not publish was accepted")
	}
}

func TestAnArchiveOutsideTheStateDirectoryIsRefused(t *testing.T) {
	// A root process that will read any path a less-privileged account names
	// is a root process doing that account's filesystem reconnaissance for
	// it. The digest check makes the *contents* safe; this keeps the path
	// uninteresting.
	f := newHandoffFixture(t)
	elsewhere := filepath.Join(t.TempDir(), "archive.tar.gz")
	if err := os.WriteFile(elsewhere, []byte("x"), 0o644); err != nil {
		t.Fatalf("cannot write the decoy: %v", err)
	}
	f.write(f.request(func(r *UpdateRequest) { r.ArchivePath = elsewhere }))

	err := f.apply()

	if err == nil {
		t.Fatal("an archive outside the state directory was accepted")
	}
	if !strings.Contains(err.Error(), "outside the agent's state directory") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
	if len(f.upgraded) != 0 {
		t.Error("the installer ran on a path outside the state directory")
	}
}

func TestATraversingArchivePathIsRefused(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) {
		r.ArchivePath = filepath.Join(f.paths.StateDir(), "..", "..", "..", "etc", "shadow")
	}))

	err := f.apply()

	if err == nil {
		t.Fatal("a traversing path was accepted")
	}
	if !strings.Contains(err.Error(), "outside the agent's state directory") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestARelativeArchivePathIsRefused(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) { r.ArchivePath = "archive.tar.gz" }))

	err := f.apply()

	if err == nil {
		t.Fatal("a relative path was accepted")
	}
	if !strings.Contains(err.Error(), "not absolute") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestASymlinkedArchiveIsRefusedRatherThanFollowed(t *testing.T) {
	// A symlink inside the state directory pointing at something root can
	// read would otherwise be hashed — it would fail the digest check and be
	// reported, but it would have been *read*, which is more than this needs
	// to allow.
	f := newHandoffFixture(t)
	secret := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(secret, []byte("not for you"), 0o600); err != nil {
		t.Fatalf("cannot write the target: %v", err)
	}
	link := filepath.Join(f.paths.StateDir(), "link.tar.gz")
	if err := os.Symlink(secret, link); err != nil {
		t.Skipf("cannot create a symlink here: %v", err)
	}
	f.write(f.request(func(r *UpdateRequest) { r.ArchivePath = link }))

	err := f.apply()

	if err == nil {
		t.Fatal("a symlinked archive was accepted")
	}
	if !strings.Contains(err.Error(), "not a regular file") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestASymlinkedRequestIsRefusedRatherThanFollowed(t *testing.T) {
	// The oldest mistake in this family: a root process following a link
	// planted by a less-privileged account.
	f := newHandoffFixture(t)
	target := filepath.Join(t.TempDir(), "elsewhere.json")
	if err := WriteUpdateRequest(target, f.request(nil)); err != nil {
		t.Fatalf("cannot write the target: %v", err)
	}
	if err := os.Symlink(target, f.paths.UpdateRequest()); err != nil {
		t.Skipf("cannot create a symlink here: %v", err)
	}

	err := f.apply()

	if err == nil {
		t.Fatal("a symlinked request was followed")
	}
	if !strings.Contains(err.Error(), "not a regular file") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestAWorldWritableRequestIsRefused(t *testing.T) {
	// If anybody on the machine can rewrite the request, the request is not
	// the agent's statement — it is everybody's.
	f := newHandoffFixture(t)
	f.write(f.request(nil))
	if err := os.Chmod(f.paths.UpdateRequest(), 0o666); err != nil {
		t.Fatalf("cannot relax the mode: %v", err)
	}

	err := f.apply()

	if err == nil {
		t.Fatal("a world-writable request was accepted")
	}
	if !strings.Contains(err.Error(), "world-writable") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestAnUnknownFieldInTheRequestIsRefused(t *testing.T) {
	// This is a privilege boundary, and ignoring something is how a check
	// gets skipped. A field this binary does not understand may be
	// load-bearing in whatever wrote it.
	f := newHandoffFixture(t)
	if err := os.WriteFile(f.paths.UpdateRequest(), []byte(`{
  "schema_version": 1,
  "instruction_id": "x",
  "version": "0.2.0",
  "platform": "linux/amd64",
  "archive_path": "/tmp/x",
  "metadata": "{}",
  "signature": "{}",
  "sha256": "aaaa"
}`), 0o640); err != nil {
		t.Fatalf("cannot write the request: %v", err)
	}

	err := f.apply()

	if err == nil {
		t.Fatal("a request with an unknown field was accepted")
	}
	// And it is gone, so the path unit does not re-trigger on it for ever.
	if _, statErr := os.Stat(f.paths.UpdateRequest()); statErr == nil {
		t.Error("an unusable request was left in place to re-trigger")
	}
}

func TestThereIsNoDigestFieldToSupply(t *testing.T) {
	// Stated as a test because it is the structural property the design rests
	// on: if the request could name a digest, a compromised agent account
	// could install anything it liked. The refusal above proves `sha256` is
	// not accepted; this proves it is not in the type either.
	payload, err := json.Marshal(UpdateRequest{})
	if err != nil {
		t.Fatalf("cannot encode: %v", err)
	}
	for _, forbidden := range []string{"sha256", "digest", "checksum", "command", "script"} {
		if strings.Contains(strings.ToLower(string(payload)), forbidden) {
			t.Errorf("the request carries a %q field; the digest must come from signed metadata", forbidden)
		}
	}
}

func TestAFutureSchemaVersionIsRefused(t *testing.T) {
	// The reader may be a *newer* binary than the writer, or an older one: an
	// update that replaces the agent mid-flight means the request was written
	// by one version and could be read by another.
	f := newHandoffFixture(t)
	request := f.request(nil)
	payload, err := json.Marshal(map[string]any{
		"schema_version": HandoffSchemaVersion + 1,
		"instruction_id": request.InstructionID,
		"version":        request.Version,
		"platform":       request.Platform,
		"archive_path":   request.ArchivePath,
		"metadata":       request.Metadata,
		"signature":      request.Signature,
		"requested_at":   request.RequestedAt,
	})
	if err != nil {
		t.Fatalf("cannot encode: %v", err)
	}
	if err := os.WriteFile(f.paths.UpdateRequest(), payload, 0o640); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	applyErr := f.apply()

	if applyErr == nil {
		t.Fatal("a future schema version was accepted")
	}
	if !strings.Contains(applyErr.Error(), "schema_version") {
		t.Errorf("the refusal does not name the cause: %v", applyErr)
	}
}

func TestAnUnparseableVersionIsRefused(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(func(r *UpdateRequest) { r.Version = "latest" }))

	err := f.apply()

	if err == nil {
		t.Fatal("a non-semantic version was accepted")
	}
	if !strings.Contains(err.Error(), "semantic version") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

// --- Failure reporting ------------------------------------------------------

func TestAFailedInstallIsRecordedAsFailed(t *testing.T) {
	f := newHandoffFixture(t)
	f.write(f.request(nil))
	f.upgrade = errors.New("the archive contains no hcc-agent")

	if err := f.apply(); err == nil {
		t.Fatal("a failed install reported success")
	}

	outcome := f.outcome()
	if outcome == nil {
		t.Fatal("no outcome was written")
	}
	if outcome.State != StateFailed {
		t.Errorf("state = %q, want %q", outcome.State, StateFailed)
	}
	if !strings.Contains(outcome.Error, "no hcc-agent") {
		t.Errorf("the outcome does not carry the reason: %q", outcome.Error)
	}
}

func TestARolledBackInstallIsRecordedDistinctly(t *testing.T) {
	// "Failed" and "failed and the previous binary is running again" need
	// different reactions from an operator, so they are different states.
	f := newHandoffFixture(t)
	f.write(f.request(nil))
	f.upgrade = errors.New("the upgraded agent did not reconnect; rolled back to 0.1.0")

	if err := f.apply(); err == nil {
		t.Fatal("a rolled-back install reported success")
	}

	outcome := f.outcome()
	if outcome == nil || outcome.State != StateRolledBack {
		t.Fatalf("outcome = %+v, want state %q", outcome, StateRolledBack)
	}
}

func TestAnAbsentOutcomeIsNotAnError(t *testing.T) {
	// The ordinary case for a freshly installed host: nothing has ever
	// updated it.
	f := newHandoffFixture(t)

	outcome, err := ReadUpdateOutcome(f.paths.UpdateOutcome())

	if err != nil {
		t.Fatalf("a missing outcome is an error: %v", err)
	}
	if outcome != nil {
		t.Errorf("outcome = %+v, want nil", outcome)
	}
}

func TestTheRequestRoundTrips(t *testing.T) {
	f := newHandoffFixture(t)
	original := f.request(nil)
	f.write(original)

	read, err := ReadUpdateRequest(f.paths.UpdateRequest())
	if err != nil {
		t.Fatalf("read: %v", err)
	}

	if read != original {
		t.Errorf("round trip changed the request:\n got %+v\nwant %+v", read, original)
	}
}

func TestTheRequestIsWrittenAtomically(t *testing.T) {
	// The path unit triggers on PathExists, so a partially written file would
	// be a request root reads halfway through. Asserted by the mode and by
	// there being no leftover temporary file, which is what the rename leaves
	// behind if it did not happen.
	f := newHandoffFixture(t)
	f.write(f.request(nil))

	info, err := os.Stat(f.paths.UpdateRequest())
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Mode().Perm() != HandoffFileMode {
		t.Errorf("mode = %#o, want %#o", info.Mode().Perm(), HandoffFileMode)
	}
	entries, err := os.ReadDir(f.paths.StateDir())
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") {
			t.Errorf("a temporary file was left behind: %s", entry.Name())
		}
	}
}

// Ensure the fixture's writer is used rather than stdout, so a failing test's
// output is attributable.
var _ io.Writer = (*strings.Builder)(nil)
