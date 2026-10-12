package monitoring_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

func configPath(t *testing.T) string {
	t.Helper()
	// t.TempDir is 0700 and removed afterwards, so these never touch a real
	// agent's configuration — which is the same discipline the credential
	// tests follow.
	return filepath.Join(t.TempDir(), "config.json")
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

func TestLoadingWithNoFileIsNotAnError(t *testing.T) {
	// A normal state, not a failure: a freshly enrolled agent has never been
	// given a configuration. A sentinel so the caller can tell it apart
	// without inspecting a wrapped filesystem error.
	_, err := monitoring.Load(configPath(t))
	if !errors.Is(err, monitoring.ErrNoStoredConfig) {
		t.Fatalf("expected ErrNoStoredConfig, got %v", err)
	}
}

func TestASavedConfigurationRoundTrips(t *testing.T) {
	path := configPath(t)
	raw := encode(t, validDocument())

	if err := monitoring.Save(path, raw); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	stored, err := monitoring.Load(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}

	if string(stored.Raw) != string(raw) {
		t.Error("the stored bytes differ from what was written")
	}
	if stored.Checksum != monitoring.Checksum(raw) {
		t.Error("the stored checksum does not match the stored bytes")
	}
	if stored.Document.Version != 1 {
		t.Errorf("version is %d, expected 1", stored.Document.Version)
	}
}

func TestTheStoredBytesStillVerifyAfterAReload(t *testing.T) {
	// The property a restart depends on. Without keeping the raw bytes, a
	// restarted agent could report which version it holds but not prove the
	// file still matches the digest it acknowledged — so a half-written or
	// hand-edited file would be reported as applied.
	path := configPath(t)
	raw := encode(t, validDocument())
	checksum := monitoring.Checksum(raw)

	if err := monitoring.Save(path, raw); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	stored, err := monitoring.Load(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}
	if !monitoring.VerifyChecksum(stored.Raw, checksum) {
		t.Fatal("the reloaded configuration does not verify against its original checksum")
	}
}

func TestLoadingACorruptFileIsAnError(t *testing.T) {
	// Not silently discarded. An agent that discarded it would come up
	// collecting nothing while reporting itself healthy, and the operator
	// needs to know.
	path := configPath(t)
	if err := os.WriteFile(path, []byte("not a document"), monitoring.FileMode); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	if _, err := monitoring.Load(path); err == nil {
		t.Fatal("expected an error for a corrupt configuration")
	}
}

func TestLoadingAnInvalidFileIsAnError(t *testing.T) {
	path := configPath(t)
	document := validDocument()
	document["version"] = 0
	if err := monitoring.Save(path, encode(t, document)); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	_, err := monitoring.Load(path)
	if err == nil {
		t.Fatal("expected an error for an invalid configuration")
	}
	if !strings.Contains(err.Error(), "version must be at least 1") {
		t.Errorf("the error should name the problem, got %v", err)
	}
}

func TestLoadingRefusesASymlink(t *testing.T) {
	// The same local attack the credential loader refuses: point the path at a
	// file you control and the agent reads a configuration you chose — which,
	// since a configuration says what to collect and how often, is a way to
	// make somebody else's agent do work you specified.
	directory := t.TempDir()
	target := filepath.Join(directory, "attacker.json")
	if err := os.WriteFile(target, encode(t, validDocument()), 0o600); err != nil {
		t.Fatalf("cannot write the target: %v", err)
	}
	link := filepath.Join(directory, "config.json")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("cannot create the symlink: %v", err)
	}

	_, err := monitoring.Load(link)
	if err == nil {
		t.Fatal("expected a symlink to be refused")
	}
	if !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("the refusal should say why, got %v", err)
	}
}

func TestLoadingRefusesAnOversizedFile(t *testing.T) {
	path := configPath(t)
	oversized := make([]byte, monitoring.MaxStoredBytes+1)
	for index := range oversized {
		oversized[index] = ' '
	}
	if err := os.WriteFile(path, oversized, monitoring.FileMode); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}
	_, err := monitoring.Load(path)
	if err == nil || !strings.Contains(err.Error(), "limit") {
		t.Fatalf("expected an oversized file to be refused, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// Saving
// ---------------------------------------------------------------------------

func TestSavingUsesRestrictivePermissions(t *testing.T) {
	path := configPath(t)
	if err := monitoring.Save(path, encode(t, validDocument())); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("cannot stat: %v", err)
	}
	if info.Mode().Perm() != monitoring.FileMode.Perm() {
		t.Errorf("mode is %v, expected %v", info.Mode().Perm(), monitoring.FileMode.Perm())
	}
}

func TestSavingCreatesTheDirectory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "deeper", "config.json")
	if err := monitoring.Save(path, encode(t, validDocument())); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	info, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatalf("the directory was not created: %v", err)
	}
	if info.Mode().Perm() != monitoring.DirMode.Perm() {
		t.Errorf("directory mode is %v, expected %v", info.Mode().Perm(), monitoring.DirMode.Perm())
	}
}

func TestSavingLeavesNoTemporaryFileBehind(t *testing.T) {
	// A crash mid-write would otherwise leave a .tmp nobody thinks to delete,
	// and on the next attempt the O_EXCL open would fail — so the agent would
	// be permanently unable to store a configuration.
	path := configPath(t)
	if err := monitoring.Save(path, encode(t, validDocument())); err != nil {
		t.Fatalf("cannot save: %v", err)
	}
	if _, err := os.Stat(path + ".tmp"); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("a temporary file survived: %v", err)
	}
}

func TestSavingTwiceReplacesTheFile(t *testing.T) {
	// The second save has to succeed even though the target exists, which is
	// the rename's job — and a .tmp left by the first would break it.
	path := configPath(t)
	for version := 1; version <= 3; version++ {
		document := validDocument()
		document["version"] = version
		if err := monitoring.Save(path, encode(t, document)); err != nil {
			t.Fatalf("save %d failed: %v", version, err)
		}
	}
	stored, err := monitoring.Load(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}
	if stored.Document.Version != 3 {
		t.Errorf("version is %d, expected 3", stored.Document.Version)
	}
}

func TestSavingRefusesAnEmptyConfiguration(t *testing.T) {
	if err := monitoring.Save(configPath(t), nil); err == nil {
		t.Fatal("expected an empty configuration to be refused")
	}
}

func TestSavingRefusesASymlink(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "elsewhere.json")
	if err := os.WriteFile(target, []byte("{}"), 0o600); err != nil {
		t.Fatalf("cannot write the target: %v", err)
	}
	link := filepath.Join(directory, "config.json")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("cannot create the symlink: %v", err)
	}

	if err := monitoring.Save(link, encode(t, validDocument())); err == nil {
		t.Fatal("expected a symlink to be refused")
	}
	// And the target is untouched, which is the actual property: writing
	// through the link would have put the configuration where the link points.
	content, err := os.ReadFile(target) //nolint:gosec // a test fixture path
	if err != nil {
		t.Fatalf("cannot read the target: %v", err)
	}
	if string(content) != "{}" {
		t.Error("the symlink target was written through")
	}
}

// ---------------------------------------------------------------------------
// Applying
// ---------------------------------------------------------------------------

func TestApplyingAValidConfigurationStoresAndAcceptsIt(t *testing.T) {
	path := configPath(t)
	raw := encode(t, validDocument())

	outcome, err := monitoring.Apply(path, 1, monitoring.Checksum(raw), raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !outcome.Accepted() {
		t.Fatalf("expected acceptance, got problems %v", outcome.Problems)
	}
	if outcome.Version != 1 {
		t.Errorf("version is %d, expected 1", outcome.Version)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("the configuration was not stored: %v", err)
	}
}

func TestApplyingRefusesAChecksumMismatchBeforeParsing(t *testing.T) {
	// Verified first, so content nobody vouched for is never validated — and
	// so a corrupted document is reported as corrupted rather than as a list
	// of parse problems that are artefacts of the corruption.
	path := configPath(t)
	raw := encode(t, validDocument())

	outcome, err := monitoring.Apply(path, 1, monitoring.ChecksumPrefix+strings.Repeat("a", 64), raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if outcome.Accepted() {
		t.Fatal("a checksum mismatch was accepted")
	}
	if len(outcome.Problems) != 1 || !strings.Contains(outcome.Problems[0], "checksum") {
		t.Fatalf("expected one checksum problem, got %v", outcome.Problems)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Error("a rejected configuration was written to disk")
	}
}

func TestARejectedConfigurationNeverReplacesTheLastGoodOne(t *testing.T) {
	// The rule that matters most: a monitoring agent that stops monitoring
	// because somebody typed a bad interval has failed at its job.
	path := configPath(t)

	good := encode(t, validDocument())
	if _, err := monitoring.Apply(path, 1, monitoring.Checksum(good), good); err != nil {
		t.Fatalf("cannot apply the first configuration: %v", err)
	}

	broken := validDocument()
	broken["version"] = 2
	setModule(broken, "load", enabledInterval(1))
	brokenRaw := encode(t, broken)

	outcome, err := monitoring.Apply(path, 2, monitoring.Checksum(brokenRaw), brokenRaw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if outcome.Accepted() {
		t.Fatal("an invalid configuration was accepted")
	}

	stored, err := monitoring.Load(path)
	if err != nil {
		t.Fatalf("cannot load: %v", err)
	}
	if stored.Document.Version != 1 {
		t.Errorf("the stored version is %d; the rejected one replaced the good one", stored.Document.Version)
	}
}

func TestARejectionReportsEveryProblem(t *testing.T) {
	path := configPath(t)
	document := validDocument()
	setModule(document, "load", enabledInterval(1))
	setModule(document, "memory", enabledInterval(0))
	raw := encode(t, document)

	outcome, err := monitoring.Apply(path, 1, monitoring.Checksum(raw), raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(outcome.Problems) < 2 {
		t.Fatalf("expected at least two problems, got %v", outcome.Problems)
	}
}

func TestAWriteFailureIsAnErrorRatherThanARejection(t *testing.T) {
	// The distinction matters to the operator: "this configuration is wrong"
	// sends them to the schedule, "this host's disk is read-only" sends them
	// to the host. A write failure reported as a rejection would send somebody
	// to look at a schedule when they should look at a disk.
	directory := t.TempDir()
	// A directory where the file should be, so the atomic write cannot proceed.
	path := filepath.Join(directory, "config.json")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatalf("cannot create the obstruction: %v", err)
	}
	raw := encode(t, validDocument())

	outcome, err := monitoring.Apply(path, 1, monitoring.Checksum(raw), raw)
	if err == nil {
		t.Fatal("expected a write failure to be an error")
	}
	if len(outcome.Problems) != 0 {
		t.Errorf("a write failure must not be reported as a rejection, got %v", outcome.Problems)
	}
}

func TestAnUnparseableDocumentIsOneProblemNotAnError(t *testing.T) {
	path := configPath(t)
	raw := []byte("{not json")
	outcome, err := monitoring.Apply(path, 1, monitoring.Checksum(raw), raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(outcome.Problems) != 1 {
		t.Fatalf("expected one problem, got %v", outcome.Problems)
	}
}
