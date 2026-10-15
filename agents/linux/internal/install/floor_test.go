package install

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// withoutRootOwnership relaxes the uid-0 requirement for the duration of a
// test.
//
// `go test` runs as an ordinary user, so every file it creates is owned by
// that user. Keeping the requirement on would make the whole floor untestable
// without root, which would mean none of it was exercised by anything. The
// requirement itself is covered by [TestTheOwnershipRequirementIsOnByDefault]
// and [TestAFloorOwnedByAnotherAccountIsRefused], which turns it back on.
func withoutRootOwnership(t *testing.T) {
	t.Helper()
	previous := requireRootOwnership
	requireRootOwnership = false
	t.Cleanup(func() { requireRootOwnership = previous })
}

// floorPath builds a root-owned-shaped update state directory in a temp root.
func floorPath(t *testing.T) string {
	t.Helper()
	withoutRootOwnership(t)
	paths := Paths{Root: t.TempDir()}
	if err := os.MkdirAll(paths.UpdateStateDir(), UpdateStateDirMode); err != nil {
		t.Fatalf("cannot create the update state directory: %v", err)
	}
	return paths.VersionFloor()
}

// --- Absence is permissive, which is the compatibility property -------------

func TestAnAbsentFloorPermitsEverything(t *testing.T) {
	// Every installation made before this code existed has no floor. If
	// absence refused, this change would stop updates on the entire existing
	// fleet, which is a worse outcome than the attack it defends against.
	withoutRootOwnership(t)
	path := Paths{Root: t.TempDir()}.VersionFloor()

	state, err := LoadState(path)

	if err != nil {
		t.Fatalf("an absent floor is an error: %v", err)
	}
	if state != nil {
		t.Fatalf("an absent floor produced a state: %+v", state)
	}
	if decision := state.PermitsVersion("0.0.1"); !decision.Permitted {
		t.Errorf("an absent floor refused a version: %s", decision.Reason)
	}
	if state.FloorText() != "none" {
		t.Errorf("FloorText() = %q, want none", state.FloorText())
	}
}

func TestAnEmptyDirectoryWithNoFileIsAbsentRatherThanUnsafe(t *testing.T) {
	path := floorPath(t)

	state, err := LoadState(path)

	if err != nil {
		t.Fatalf("a missing file in an existing directory is an error: %v", err)
	}
	if state != nil {
		t.Fatalf("a missing file produced a state: %+v", state)
	}
}

// --- The floor refuses what it is for ---------------------------------------

func TestAVersionBelowTheFloorIsRefused(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", "instruction-a"); err != nil {
		t.Fatalf("cannot record the install: %v", err)
	}

	state, err := LoadState(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}

	decision := state.PermitsVersion("0.3.9")
	if decision.Permitted {
		t.Fatal("a version below the floor was permitted")
	}
	// The message has to say what to do about it, because the operator
	// reading it in the update history is being told their controller cannot
	// fix this.
	for _, fragment := range []string{"0.3.9", "0.4.0", "rollback", "--allow-downgrade"} {
		if !strings.Contains(decision.Reason, fragment) {
			t.Errorf("the refusal does not mention %q: %s", fragment, decision.Reason)
		}
	}
}

func TestTheFloorItselfIsPermitted(t *testing.T) {
	// Equal is not below. Reinstalling the current version is a repair, not a
	// rollback, and the gate above this one is what refuses it as pointless.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", "instruction-a"); err != nil {
		t.Fatalf("cannot record the install: %v", err)
	}
	state, _ := LoadState(path)

	if decision := state.PermitsVersion("0.4.0"); !decision.Permitted {
		t.Errorf("the floor version itself was refused: %s", decision.Reason)
	}
	if decision := state.PermitsVersion("0.4.1"); !decision.Permitted {
		t.Errorf("a version above the floor was refused: %s", decision.Reason)
	}
}

func TestAPrereleaseBelowTheFloorIsRefused(t *testing.T) {
	// 1.0.0-rc.1 precedes 1.0.0 in semver, so a controller offering the
	// release candidate of a version already installed is offering a
	// downgrade. A string comparison would accept it.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "1.0.0", ""); err != nil {
		t.Fatalf("cannot record the install: %v", err)
	}
	state, _ := LoadState(path)

	if decision := state.PermitsVersion("1.0.0-rc.1"); decision.Permitted {
		t.Error("a pre-release of the installed version was permitted")
	}
}

func TestAnUnparseableTargetIsRefusedByTheFloor(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", ""); err != nil {
		t.Fatalf("cannot record the install: %v", err)
	}
	state, _ := LoadState(path)

	if decision := state.PermitsVersion("latest"); decision.Permitted {
		t.Error("a target that is not a version was permitted")
	}
}

// --- Monotonicity -----------------------------------------------------------

func TestTheFloorNeverFallsOnItsOwn(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.5.0", "a"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}
	// Root installed something older by hand. The record of what this host
	// has run is not something that unproves.
	if err := RecordSuccessfulInstall(path, "0.4.0", "b"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}

	state, err := LoadState(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}
	if state.Floor != "0.5.0" {
		t.Errorf("floor = %q, want 0.5.0", state.Floor)
	}
	if state.HighestInstalled != "0.5.0" {
		t.Errorf("highest_installed = %q, want 0.5.0", state.HighestInstalled)
	}
}

func TestOnlyLowerFloorLowersIt(t *testing.T) {
	// The root escape hatch. `hcc-agent upgrade --allow-downgrade` needs root
	// to run at all, and root is outside the threat boundary — so there is a
	// way down, it is explicit, and the controller cannot reach it.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.5.0", "a"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}

	if err := LowerFloor(path, "0.3.0"); err != nil {
		t.Fatalf("cannot lower the floor: %v", err)
	}

	state, _ := LoadState(path)
	if state.Floor != "0.3.0" {
		t.Errorf("floor = %q, want 0.3.0", state.Floor)
	}
	// The high-water mark survives, so an operator can still see that this
	// host has run something newer than it is running.
	if state.HighestInstalled != "0.5.0" {
		t.Errorf("highest_installed = %q, want 0.5.0 — lowering the floor erased the record",
			state.HighestInstalled)
	}
	if decision := state.PermitsVersion("0.3.0"); !decision.Permitted {
		t.Errorf("the lowered floor still refuses: %s", decision.Reason)
	}
}

func TestLowerFloorRefusesSomethingThatIsNotAVersion(t *testing.T) {
	path := floorPath(t)

	if err := LowerFloor(path, "0.3"); err == nil {
		t.Fatal("an incomplete version was accepted as a floor")
	}
}

// --- Seeding ----------------------------------------------------------------

func TestEnsureFloorSeedsFromTheRunningVersion(t *testing.T) {
	// The compatibility path that matters: a host installed months ago, with
	// no floor, running 0.3.0. The first privileged update seeds the floor at
	// 0.3.0 **before** evaluating the request, so that very update is already
	// protected rather than the one after it.
	path := floorPath(t)

	state, err := EnsureFloor(path, "0.3.0")

	if err != nil {
		t.Fatalf("cannot seed: %v", err)
	}
	if state.Floor != "0.3.0" {
		t.Errorf("floor = %q, want 0.3.0", state.Floor)
	}
	if decision := state.PermitsVersion("0.2.0"); decision.Permitted {
		t.Error("the seeded floor permitted a rollback")
	}
	// And it is on disk, not just in memory.
	reloaded, err := LoadState(path)
	if err != nil {
		t.Fatalf("cannot reload: %v", err)
	}
	if reloaded == nil || reloaded.Floor != "0.3.0" {
		t.Errorf("the seeded floor did not persist: %+v", reloaded)
	}
}

func TestEnsureFloorDoesNotMoveAnExistingFloor(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.5.0", "a"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}

	// A binary that reports something older — which is what a rollback leaves
	// running — must not drag the floor down with it.
	state, err := EnsureFloor(path, "0.2.0")

	if err != nil {
		t.Fatalf("cannot seed: %v", err)
	}
	if state.Floor != "0.5.0" {
		t.Errorf("floor = %q, want 0.5.0 — a rolled-back binary lowered the floor", state.Floor)
	}
}

func TestADevelopmentVersionSeedsNothing(t *testing.T) {
	// A build with no version stamp reports "dev", which does not order. The
	// honest floor to derive from it is none at all.
	path := floorPath(t)

	state, err := EnsureFloor(path, "dev")

	if err != nil {
		t.Fatalf("seeding from an unstamped build is an error: %v", err)
	}
	if state != nil && state.Floor != "" {
		t.Errorf("an unstamped build produced a floor: %+v", state)
	}
	if _, err := os.Stat(path); err == nil {
		t.Error("an unstamped build wrote a floor file")
	}
}

// --- Replay -----------------------------------------------------------------

func TestAnInstructionIsNotAppliedTwice(t *testing.T) {
	// A compromised agent account can rewrite the request file whenever it
	// likes. Without this, replaying the last authorised request is a restart
	// loop of a root-owned service, driven from an unprivileged account.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", "instruction-a"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}
	state, _ := LoadState(path)

	decision := state.PermitsInstruction("instruction-a", "0.4.0")

	if decision.Permitted {
		t.Fatal("the last applied instruction was accepted again")
	}
	if !strings.Contains(decision.Reason, "already been applied") {
		t.Errorf("the refusal does not name the cause: %s", decision.Reason)
	}
	// A different instruction for the same version is fine: that is a new
	// authorisation, not a replay.
	if decision := state.PermitsInstruction("instruction-b", "0.4.0"); !decision.Permitted {
		t.Errorf("a new instruction was refused: %s", decision.Reason)
	}
}

func TestAnEmptyInstructionIDIsNotTreatedAsAReplay(t *testing.T) {
	// The operator path carries no instruction. Two of those in a row must
	// not look like a replay of each other.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", ""); err != nil {
		t.Fatalf("cannot record: %v", err)
	}
	state, _ := LoadState(path)

	if decision := state.PermitsInstruction("", "0.4.1"); !decision.Permitted {
		t.Errorf("an unauthenticated install was refused as a replay: %s", decision.Reason)
	}
}

// --- A file that exists and is wrong is a refusal, not an absence -----------

func TestACorruptFloorIsRefusedRatherThanIgnored(t *testing.T) {
	// The whole mechanism turns on this. If unparseable meant "no floor",
	// then `echo garbage > version-floor.json` would be the bypass — and the
	// one account that cannot do that is the one this defends against, so
	// making it work would be pointless anyway.
	path := floorPath(t)
	if err := os.WriteFile(path, []byte("{not json"), VersionFloorMode); err != nil {
		t.Fatalf("cannot write the corrupt file: %v", err)
	}

	_, err := LoadState(path)

	if !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a corrupt floor was not refused: %v", err)
	}
}

func TestAFloorFromAFutureSchemaIsRefused(t *testing.T) {
	path := floorPath(t)
	if err := os.WriteFile(path,
		[]byte(`{"schema_version":2,"floor":"1.0.0","highest_installed":"1.0.0","updated_at":""}`),
		VersionFloorMode,
	); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	_, err := LoadState(path)

	if !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a future schema was accepted: %v", err)
	}
}

func TestAFloorWithAnUnknownFieldIsRefused(t *testing.T) {
	// An unknown field may be the one carrying the floor in a newer format.
	path := floorPath(t)
	if err := os.WriteFile(path,
		[]byte(`{"schema_version":1,"floor":"1.0.0","highest_installed":"1.0.0",`+
			`"updated_at":"","ceiling":"0.1.0"}`),
		VersionFloorMode,
	); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	if _, err := LoadState(path); !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("an unknown field was ignored: %v", err)
	}
}

func TestAFloorThatIsNotAVersionIsRefused(t *testing.T) {
	path := floorPath(t)
	if err := os.WriteFile(path,
		[]byte(`{"schema_version":1,"floor":"latest","highest_installed":"","updated_at":""}`),
		VersionFloorMode,
	); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	if _, err := LoadState(path); !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a floor that is not a version was accepted: %v", err)
	}
}

func TestAWorldWritableFloorIsRefused(t *testing.T) {
	// A world-writable floor is one any local account can lower, which is
	// exactly as useful as no floor while looking like protection.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", ""); err != nil {
		t.Fatalf("cannot record: %v", err)
	}
	if err := os.Chmod(path, 0o666); err != nil {
		t.Fatalf("cannot chmod: %v", err)
	}

	if _, err := LoadState(path); !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a world-writable floor was accepted: %v", err)
	}
}

func TestAWorldWritableFloorDirectoryIsRefused(t *testing.T) {
	// A directory anyone can write is a directory in which anyone can
	// replace the floor by rename, whatever the file's own mode says.
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", ""); err != nil {
		t.Fatalf("cannot record: %v", err)
	}
	if err := os.Chmod(filepath.Dir(path), 0o777); err != nil {
		t.Fatalf("cannot chmod: %v", err)
	}

	if _, err := LoadState(path); !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a world-writable floor directory was accepted: %v", err)
	}
}

func TestASymlinkedFloorIsRefusedRatherThanFollowed(t *testing.T) {
	path := floorPath(t)
	elsewhere := filepath.Join(t.TempDir(), "forged.json")
	if err := os.WriteFile(elsewhere,
		[]byte(`{"schema_version":1,"floor":"0.0.1","highest_installed":"0.0.1","updated_at":""}`),
		0o644,
	); err != nil {
		t.Fatalf("cannot write the forgery: %v", err)
	}
	if err := os.Symlink(elsewhere, path); err != nil {
		t.Skipf("cannot create a symlink here: %v", err)
	}

	_, err := LoadState(path)

	if !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a symlinked floor was followed: %v", err)
	}
	if !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestASymlinkedFloorDirectoryIsRefused(t *testing.T) {
	withoutRootOwnership(t)
	paths := Paths{Root: t.TempDir()}
	elsewhere := t.TempDir()
	if err := os.MkdirAll(filepath.Dir(paths.UpdateStateDir()), 0o755); err != nil {
		t.Fatalf("cannot create the parent: %v", err)
	}
	if err := os.Symlink(elsewhere, paths.UpdateStateDir()); err != nil {
		t.Skipf("cannot create a symlink here: %v", err)
	}

	if _, err := LoadState(paths.VersionFloor()); !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a symlinked floor directory was accepted: %v", err)
	}
}

func TestAFloorOwnedByAnotherAccountIsRefused(t *testing.T) {
	// The check that makes the whole thing mean something, exercised with the
	// requirement **on**: this test does not call withoutRootOwnership, so
	// the file it writes — owned by whoever runs `go test` — is refused for
	// exactly the reason a forged one would be.
	if os.Geteuid() == 0 {
		t.Skip("running as root, so a file this test writes is legitimately root-owned")
	}
	paths := Paths{Root: t.TempDir()}
	if err := os.MkdirAll(paths.UpdateStateDir(), UpdateStateDirMode); err != nil {
		t.Fatalf("cannot create the directory: %v", err)
	}
	if err := os.WriteFile(paths.VersionFloor(),
		[]byte(`{"schema_version":1,"floor":"0.0.1","highest_installed":"0.0.1","updated_at":""}`),
		VersionFloorMode,
	); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	_, err := LoadState(paths.VersionFloor())

	if !errors.Is(err, ErrFloorUnsafe) {
		t.Fatalf("a floor owned by a non-root account was accepted: %v", err)
	}
	if !strings.Contains(err.Error(), "rather than root") {
		t.Errorf("the refusal does not name the cause: %v", err)
	}
}

func TestTheOwnershipRequirementIsOnByDefault(t *testing.T) {
	// The test escape is a package variable. If it ever defaulted to false —
	// a stray assignment, a copy-paste into non-test code — every check above
	// would still pass and the mechanism would be off in production.
	if !requireRootOwnership {
		t.Fatal("requireRootOwnership defaults to false; the floor is unenforced in production")
	}
}

// --- Round-tripping ---------------------------------------------------------

func TestTheStoredDocumentRoundTrips(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "1.2.3-beta.1+build.9", "instruction-x"); err != nil {
		t.Fatalf("cannot record: %v", err)
	}

	state, err := LoadState(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}
	if state.Floor != "1.2.3-beta.1+build.9" {
		t.Errorf("floor = %q, want the exact string that was written", state.Floor)
	}
	if state.LastInstructionID != "instruction-x" {
		t.Errorf("last_instruction_id = %q", state.LastInstructionID)
	}
	if state.UpdatedAt == "" {
		t.Error("updated_at was not stamped")
	}
}

func TestTheFloorFileIsNotWorldWritableWhenWritten(t *testing.T) {
	path := floorPath(t)
	if err := RecordSuccessfulInstall(path, "0.4.0", ""); err != nil {
		t.Fatalf("cannot record: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("cannot stat: %v", err)
	}
	if info.Mode().Perm()&0o022 != 0 {
		t.Errorf("mode = %#o; the floor is writable by something other than root",
			info.Mode().Perm())
	}
	// Readable by the agent account, which needs it to refuse early and to
	// report the floor it is under.
	if info.Mode().Perm()&0o004 == 0 {
		t.Errorf("mode = %#o; the agent account cannot read the floor", info.Mode().Perm())
	}
}
