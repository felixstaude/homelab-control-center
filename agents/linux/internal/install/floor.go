package install

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// The anti-rollback floor: the oldest version this host will accept.
//
// # What it is for
//
// Evergreen's trust model says a compromised controller cannot install a
// binary the publisher did not build, because the digest comes out of
// publisher-signed metadata. It does **not** say the controller cannot install
// an *old* binary the publisher did build — and a signed release from before a
// fix is exactly what an attacker who has taken the control plane would
// choose. Signature verification is the wrong tool for that: the signature on
// 0.1.0 is as valid today as it was the day it was made.
//
// The tool for it is a durable, monotonic, locally-owned floor. The host
// remembers the newest version it has successfully run and refuses to go
// below it, whatever the controller authorises.
//
// # Three versions, deliberately kept apart
//
//   - **The running version** is what the binary executing right now reports.
//     It is not stored here: it is [buildinfo.Info.Version], and it can be
//     *below* the floor after a rollback, which is a state an operator needs
//     to be able to see rather than one the data model should forbid.
//   - **[State.HighestInstalled]** is the newest version this host has ever
//     run to a successful, health-checked conclusion. A record, never lowered
//     automatically, and the thing that makes "we are running below our own
//     high-water mark" a visible fact.
//   - **[State.Floor]** is the oldest version an update may target. It
//     normally equals HighestInstalled, and it is a separate field because
//     lowering it is a legitimate root-authorised operation that must not
//     erase the record of what was installed.
//
// # Why the floor is not where the agent can reach it
//
// A floor an attacker can lower is not a floor. The agent's state directory
// `/var/lib/hcc-agent` is owned by the service account **by design** — the
// agent stages archives in it — so anything stored there can be rewritten by a
// compromised agent account. So the floor lives in a sibling directory owned
// by root, [DefaultUpdateStateDir], which the agent can read and cannot write.
//
// A sibling rather than a subdirectory of the state directory, and that is the
// whole point rather than tidiness: permission to rename or delete a directory
// entry comes from the **parent** directory, so a root-owned `trust/`
// subdirectory inside an agent-owned state directory could be renamed away by
// the agent and replaced with one it wrote. The only fix inside that layout is
// a sticky bit, which is a subtle property one `chmod` undoes. A sibling needs
// write access to /var/lib, which the agent does not have.
//
// # What this does not claim
//
// Nothing here resists a compromised **root** account. Root can rewrite the
// floor, replace the binary, or remove the unit. That is outside the threat
// boundary and is stated rather than glossed: the property defended is
// "a compromised *controller*, or a compromised *unprivileged agent account*,
// cannot move this host to an older release".
//
// Recorded as ADR-063.

// FloorSchemaVersion is the shape of the stored document.
const FloorSchemaVersion = 1

// MaxFloorBytes bounds the file. The real document is about 200 bytes.
const MaxFloorBytes = 64 * 1024

// State is the durable update state the privileged side owns.
//
// Every field is a string rather than a parsed version, because this is a
// document on disk that a future binary has to be able to read: a type that
// round-trips exactly is worth more here than one that is convenient in
// memory.
type State struct {
	SchemaVersion int `json:"schema_version"`
	// Floor is the oldest version an update may target. Empty means no floor
	// has been established, which is the state of every installation made
	// before this existed.
	Floor string `json:"floor"`
	// HighestInstalled is the newest version this host has run to a
	// successful conclusion.
	HighestInstalled string `json:"highest_installed"`
	// LastInstructionID is the controller authorisation last applied, for
	// replay protection: a request naming it again is refused.
	LastInstructionID string `json:"last_instruction_id,omitempty"`
	// UpdatedAt is RFC 3339, for an operator reading the file. Not used for
	// any decision.
	UpdatedAt string `json:"updated_at"`
}

// ErrFloorUnsafe reports a floor file that exists and cannot be trusted.
//
// Distinguished from absence, and the distinction decides the behaviour:
// absence is the normal state of an installation made before this existed and
// is permissive; a file that is there and wrong is a refusal, because
// "unreadable" is what an attacker would arrange if ignoring it meant
// proceeding.
var ErrFloorUnsafe = errors.New("the update floor cannot be trusted")

// LoadState reads the stored update state.
//
// Returns (nil, nil) when the file does not exist: no floor has been
// established, which is permissive and is what every existing installation
// looks like on the day it first runs a binary that has this code.
//
// Returns [ErrFloorUnsafe] for anything else that is wrong — a symlink, a
// non-regular file, a file not owned by root, a group- or world-writable file,
// a directory that is not root-owned, unparseable contents, a version that is
// not semantic. Each of those is a thing an attacker would construct if the
// alternative to a valid floor were no floor.
func LoadState(path string) (*State, error) {
	directory := filepath.Dir(path)
	if err := checkRootOwnedDirectory(directory); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil //nolint:nilnil // absence is not an error here
		}
		return nil, err
	}

	// O_NOFOLLOW so a symlink at the final component is a refusal rather than
	// a read of whatever it points at. The directory above is root-owned and
	// checked, so no intermediate component is attacker-controlled.
	handle, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return nil, nil //nolint:nilnil // absence is not an error here
	case errors.Is(err, syscall.ELOOP):
		return nil, fmt.Errorf("%w: %s is a symbolic link", ErrFloorUnsafe, path)
	case err != nil:
		return nil, fmt.Errorf("%w: cannot read %s: %v", ErrFloorUnsafe, path, err)
	}
	defer handle.Close()

	info, err := handle.Stat()
	if err != nil {
		return nil, fmt.Errorf("%w: cannot stat %s: %v", ErrFloorUnsafe, path, err)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf(
			"%w: %s is not a regular file (mode %s)", ErrFloorUnsafe, path, info.Mode(),
		)
	}
	if info.Size() > MaxFloorBytes {
		return nil, fmt.Errorf(
			"%w: %s is %d bytes, over the %d byte limit",
			ErrFloorUnsafe, path, info.Size(), MaxFloorBytes,
		)
	}
	if err := checkRootOwnedFile(path, info); err != nil {
		return nil, err
	}

	payload, err := io.ReadAll(io.LimitReader(handle, MaxFloorBytes))
	if err != nil {
		return nil, fmt.Errorf("%w: cannot read %s: %v", ErrFloorUnsafe, path, err)
	}

	decoder := json.NewDecoder(strings.NewReader(string(payload)))
	// A field this binary does not know may be the one carrying the floor in a
	// newer format. Refusing is the only safe reading of it.
	decoder.DisallowUnknownFields()
	var state State
	if err := decoder.Decode(&state); err != nil {
		return nil, fmt.Errorf("%w: %s is not a usable document: %v", ErrFloorUnsafe, path, err)
	}
	if state.SchemaVersion != FloorSchemaVersion {
		return nil, fmt.Errorf(
			"%w: %s has schema_version %d and this binary understands %d",
			ErrFloorUnsafe, path, state.SchemaVersion, FloorSchemaVersion,
		)
	}
	for _, field := range []struct{ name, value string }{
		{"floor", state.Floor},
		{"highest_installed", state.HighestInstalled},
	} {
		if field.value == "" {
			continue
		}
		if _, err := version.Parse(field.value); err != nil {
			return nil, fmt.Errorf(
				"%w: %s records %s %q, which is not a semantic version",
				ErrFloorUnsafe, path, field.name, field.value,
			)
		}
	}
	if len(state.LastInstructionID) > maxIDChars {
		return nil, fmt.Errorf(
			"%w: %s records an instruction id of %d characters, over the %d limit",
			ErrFloorUnsafe, path, len(state.LastInstructionID), maxIDChars,
		)
	}
	return &state, nil
}

// checkRootOwnedDirectory refuses a directory a non-root account could write.
//
// Returns an error wrapping [os.ErrNotExist] when it is simply absent, which
// the caller reads as "no floor established" rather than as a problem.
func checkRootOwnedDirectory(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err != nil {
		return fmt.Errorf("%w: cannot stat %s: %v", ErrFloorUnsafe, path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("%w: %s is a symbolic link", ErrFloorUnsafe, path)
	}
	if !info.IsDir() {
		return fmt.Errorf("%w: %s is not a directory", ErrFloorUnsafe, path)
	}
	if info.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf(
			"%w: %s is mode %#o, so an account other than root can write into it",
			ErrFloorUnsafe, path, info.Mode().Perm(),
		)
	}
	return ownedByRoot(path, info)
}

// checkRootOwnedFile refuses a file a non-root account could have written.
func checkRootOwnedFile(path string, info os.FileInfo) error {
	if info.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf(
			"%w: %s is mode %#o, so an account other than root can write it",
			ErrFloorUnsafe, path, info.Mode().Perm(),
		)
	}
	return ownedByRoot(path, info)
}

// ownedByRoot checks uid 0, skipping the check when the test harness asked it
// to.
//
// # Why there is an escape at all
//
// `go test` runs as an ordinary user, so every file it creates is owned by
// that user and a uid-0 requirement would make the whole floor untestable
// without root — which would mean the checks were exercised by nothing. The
// escape is a package variable rather than an exported option precisely so
// that no production code path can reach it: nothing outside this package can
// set it, and `TestTheOwnershipCheckIsOnByDefault` asserts its default.
var requireRootOwnership = true

func ownedByRoot(path string, info os.FileInfo) error {
	if !requireRootOwnership {
		return nil
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		// A platform whose FileInfo carries no uid. Not reachable on Linux,
		// and refusing is the only safe answer: the check cannot be made.
		return fmt.Errorf("%w: cannot determine the owner of %s", ErrFloorUnsafe, path)
	}
	if stat.Uid != 0 {
		return fmt.Errorf(
			"%w: %s is owned by uid %d rather than root, so it is not evidence "+
				"of anything the agent account could not have written",
			ErrFloorUnsafe, path, stat.Uid,
		)
	}
	return nil
}

// FloorDecision is why an update was or was not permitted by the floor.
type FloorDecision struct {
	Permitted bool
	// Reason is operator-facing and is set only when Permitted is false.
	Reason string
}

// PermitsVersion reports whether target may be installed.
//
// A nil state — no floor established — permits everything, which is the
// compatibility behaviour for an installation made before the floor existed.
// An unparseable target is refused here as well as upstream: this is a
// security gate and it does not assume its caller checked.
func (s *State) PermitsVersion(target string) FloorDecision {
	if s == nil || strings.TrimSpace(s.Floor) == "" {
		return FloorDecision{Permitted: true}
	}
	wanted, err := version.Parse(target)
	if err != nil {
		return FloorDecision{
			Reason: fmt.Sprintf("%q is not a semantic version, so it cannot be compared "+
				"against this host's floor of %s", target, s.Floor),
		}
	}
	floor, err := version.Parse(s.Floor)
	if err != nil {
		// Unreachable: LoadState refuses an unparseable floor. Handled anyway,
		// because a gate that panics on bad input is a gate that is bypassed
		// by bad input.
		return FloorDecision{
			Reason: fmt.Sprintf("this host's recorded floor %q is not a semantic version", s.Floor),
		}
	}
	if wanted.LessThan(floor) {
		return FloorDecision{
			Reason: fmt.Sprintf(
				"%s is older than %s, which this host has already run successfully. "+
					"Installing it would be a rollback, and no controller authorisation "+
					"overrides that. A deliberate downgrade needs root on this host: "+
					"`hcc-agent upgrade --from <archive> --sha256 <digest> --allow-downgrade`",
				wanted, floor,
			),
		}
	}
	return FloorDecision{Permitted: true}
}

// PermitsInstruction reports whether an instruction may be applied at all.
//
// Two gates: the version floor, and replay. A request naming an instruction
// already applied is refused, so a compromised agent account cannot re-run the
// last authorised install by rewriting the request file — which on a host
// whose service the installer restarts is a restart loop it can drive from an
// unprivileged account.
func (s *State) PermitsInstruction(instructionID, target string) FloorDecision {
	if s != nil && instructionID != "" && s.LastInstructionID == instructionID {
		return FloorDecision{
			Reason: fmt.Sprintf(
				"instruction %s has already been applied on this host; refusing to "+
					"apply it again", instructionID,
			),
		}
	}
	return s.PermitsVersion(target)
}

// FloorText renders the floor for an operator, or says there is none.
func (s *State) FloorText() string {
	if s == nil || strings.TrimSpace(s.Floor) == "" {
		return "none"
	}
	return s.Floor
}

// EnsureFloor establishes a floor if none exists, at seed.
//
// Called by the privileged installer **before** it evaluates a request, so a
// host that has been running since before this code existed is protected on
// its very first controller-driven update rather than on its second: the floor
// is seeded at what is running now, and the request then has to clear it.
//
// An unparseable seed — a development build reporting "dev" — establishes
// nothing and is not an error. There is no honest floor to derive from a
// version that does not order.
//
// Returns the state in effect afterwards.
func EnsureFloor(path, seed string) (*State, error) {
	existing, err := LoadState(path)
	if err != nil {
		return nil, err
	}
	if existing != nil && strings.TrimSpace(existing.Floor) != "" {
		return existing, nil
	}
	if _, err := version.Parse(seed); err != nil {
		return existing, nil
	}
	state := State{Floor: seed, HighestInstalled: seed}
	if existing != nil {
		state.LastInstructionID = existing.LastInstructionID
		// Keep whichever record is higher: a host may have installed a newer
		// version and then been rolled back, and the record is of the highest
		// ever run rather than of what is running.
		state.HighestInstalled = higherVersion(existing.HighestInstalled, seed)
	}
	if err := writeState(path, state); err != nil {
		return nil, err
	}
	return &state, nil
}

// RecordSuccessfulInstall raises the floor to installed.
//
// Called **only** after an upgrade has run to a health-checked conclusion:
// the binary was replaced, the service restarted, and the replacement
// reconnected to the controller. An attempt does not raise the floor, which is
// what keeps a failed upgrade from locking a host out of the versions between
// where it is and where it tried to go.
//
// Never lowers. An install of an older version — which only root can authorise
// — records the instruction and leaves the floor where it was, because the
// floor is the record of what has been proven to work on this host and a
// deliberate downgrade does not unprove it. [LowerFloor] is how that is
// changed, and it is a separate, explicit act.
func RecordSuccessfulInstall(path, installed, instructionID string) error {
	state, err := LoadState(path)
	if err != nil {
		return err
	}
	if state == nil {
		state = &State{}
	}
	state.LastInstructionID = instructionID
	if _, err := version.Parse(installed); err == nil {
		state.Floor = higherVersion(state.Floor, installed)
		state.HighestInstalled = higherVersion(state.HighestInstalled, installed)
	}
	return writeState(path, *state)
}

// LowerFloor sets the floor to target, which may be below the current one.
//
// The root escape hatch, and the only way down. Used by `hcc-agent upgrade
// --allow-downgrade`, which needs root to run at all. [State.HighestInstalled]
// is deliberately **not** lowered: the record of what this host has run is not
// something a downgrade should erase, and it is what tells an operator later
// that the machine is below its own high-water mark.
func LowerFloor(path, target string) error {
	if _, err := version.Parse(target); err != nil {
		return fmt.Errorf("%q is not a semantic version", target)
	}
	state, err := LoadState(path)
	if err != nil {
		return err
	}
	if state == nil {
		state = &State{}
	}
	state.Floor = target
	state.HighestInstalled = higherVersion(state.HighestInstalled, target)
	return writeState(path, *state)
}

// higherVersion returns whichever of two version strings orders higher.
//
// An empty or unparseable value loses to a parseable one, so a document
// written by a build that reported "dev" is repaired by the first real
// version rather than keeping it for ever.
func higherVersion(left, right string) string {
	leftVersion, leftErr := version.Parse(left)
	rightVersion, rightErr := version.Parse(right)
	switch {
	case leftErr != nil && rightErr != nil:
		return ""
	case leftErr != nil:
		return right
	case rightErr != nil:
		return left
	case leftVersion.LessThan(rightVersion):
		return right
	default:
		return left
	}
}

// writeState persists atomically, creating the root-owned directory if needed.
func writeState(path string, state State) error {
	state.SchemaVersion = FloorSchemaVersion
	state.UpdatedAt = time.Now().UTC().Format(time.RFC3339)

	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, UpdateStateDirMode); err != nil {
		return fmt.Errorf("cannot create %s: %w", directory, err)
	}
	// Explicitly, because MkdirAll honours the umask and a 077 umask would
	// leave the directory unreadable by the agent account — which reads the
	// floor to report it and to refuse early.
	if err := os.Chmod(directory, UpdateStateDirMode); err != nil {
		return fmt.Errorf("cannot set the mode on %s: %w", directory, err)
	}
	payload, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("cannot encode the update state: %w", err)
	}
	return writeFileAtomically(path, append(payload, '\n'), VersionFloorMode)
}
