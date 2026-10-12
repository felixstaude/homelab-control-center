package credentials_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
)

const sample = "wCJFSrCtB0EYmMqXWTyXcQbLB0OBfGNbKNfYbO3bCEE"

// secureDir returns a temporary directory with safe permissions.
//
// t.TempDir is already 0700, but it is set explicitly so these tests do not
// silently depend on a property of the testing package — and so a change there
// would not quietly make the directory check untested.
func secureDir(t *testing.T) string {
	t.Helper()
	directory := t.TempDir()
	if err := os.Chmod(directory, 0o700); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	return directory
}

func TestSaveThenLoadRoundTrips(t *testing.T) {
	t.Parallel()

	path := filepath.Join(secureDir(t), "credential")

	if err := credentials.Save(path, sample); err != nil {
		t.Fatalf("save: %v", err)
	}
	loaded, err := credentials.Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if loaded != sample {
		t.Errorf("loaded = %q, want %q", loaded, sample)
	}
}

func TestSaveCreatesTheDirectory(t *testing.T) {
	t.Parallel()

	// The ordinary case on a fresh install: /etc/hcc-agent does not exist yet.
	path := filepath.Join(secureDir(t), "nested", "credential")

	if err := credentials.Save(path, sample); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Mode().Perm() != credentials.DirMode {
		t.Errorf("directory mode = %#o, want %#o", info.Mode().Perm(), credentials.DirMode)
	}
}

func TestSaveWritesModeSixHundred(t *testing.T) {
	t.Parallel()

	// And created with it rather than chmod-ed afterwards: the window between
	// creation and chmod is a window in which the secret is readable.
	path := filepath.Join(secureDir(t), "credential")
	if err := credentials.Save(path, sample); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Mode().Perm() != credentials.FileMode {
		t.Errorf("mode = %#o, want %#o", info.Mode().Perm(), credentials.FileMode)
	}
}

func TestSaveLeavesNoTemporaryFileBehind(t *testing.T) {
	t.Parallel()

	// A crash mid-enrollment would otherwise leave a credential sitting in a
	// .tmp file nobody will think to delete — and which, unlike the target,
	// nothing checks the permissions of afterwards.
	directory := secureDir(t)
	path := filepath.Join(directory, "credential")
	if err := credentials.Save(path, sample); err != nil {
		t.Fatalf("save: %v", err)
	}

	entries, err := os.ReadDir(directory)
	if err != nil {
		t.Fatalf("readdir: %v", err)
	}
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".tmp") {
			t.Errorf("a temporary file survived: %s", entry.Name())
		}
	}
}

func TestSaveReplacesAnExistingCredential(t *testing.T) {
	t.Parallel()

	// Re-enrollment overwrites, and the rename is atomic — so the file is
	// either the old credential or the new one, never a truncated mixture.
	path := filepath.Join(secureDir(t), "credential")
	if err := credentials.Save(path, sample); err != nil {
		t.Fatalf("save: %v", err)
	}
	replacement := strings.Repeat("z", 43)
	if err := credentials.Save(path, replacement); err != nil {
		t.Fatalf("save again: %v", err)
	}

	loaded, err := credentials.Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if loaded != replacement {
		t.Errorf("loaded = %q, want the replacement", loaded)
	}
}

func TestSaveRefusesAnEmptyCredential(t *testing.T) {
	t.Parallel()

	// An empty credential file fails authentication with no indication that
	// the file is the problem, so it is refused at the point of writing.
	path := filepath.Join(secureDir(t), "credential")

	if err := credentials.Save(path, "   \n"); err == nil {
		t.Fatal("expected an empty credential to be refused")
	}
	if _, err := os.Stat(path); err == nil {
		t.Error("nothing should have been written")
	}
}

func TestSaveRefusesToWriteThroughASymlink(t *testing.T) {
	t.Parallel()

	// The classic local attack: point the credential path at a file you own,
	// so the agent writes the secret where you can read it.
	directory := secureDir(t)
	path := filepath.Join(directory, "credential")
	target := filepath.Join(directory, "attacker-owned")
	if err := os.WriteFile(target, []byte("planted"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	err := credentials.Save(path, sample)
	if err == nil {
		t.Fatal("expected a symlinked path to be refused")
	}
	if !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("the error should name the problem, got: %v", err)
	}
	// And the secret must not have reached the target.
	contents, readErr := os.ReadFile(target)
	if readErr != nil {
		t.Fatalf("read: %v", readErr)
	}
	if strings.Contains(string(contents), sample) {
		t.Fatal("the credential was written through the symlink")
	}
}

func TestLoadReportsNotEnrolledForAMissingFile(t *testing.T) {
	t.Parallel()

	// The one failure that is not a problem: a freshly installed agent is in a
	// normal state, and the right response is an instruction rather than an
	// error. Distinguished so the caller can print the right thing.
	_, err := credentials.Load(filepath.Join(secureDir(t), "absent"))

	if !errors.Is(err, credentials.ErrNotEnrolled) {
		t.Fatalf("expected ErrNotEnrolled, got: %v", err)
	}
}

func TestLoadRefusesAGroupOrWorldReadableFile(t *testing.T) {
	t.Parallel()

	// A secret readable by every local user is not a secret. Refused rather
	// than silently fixed: chmod-ing a file the agent did not create would
	// hide a misconfiguration the operator needs to see.
	for _, mode := range []os.FileMode{0o640, 0o644, 0o666, 0o604} {
		path := filepath.Join(secureDir(t), "credential")
		if err := os.WriteFile(path, []byte(sample), mode); err != nil {
			t.Fatalf("write: %v", err)
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatalf("chmod: %v", err)
		}

		_, err := credentials.Load(path)
		if err == nil {
			t.Errorf("mode %#o was accepted", mode)
			continue
		}
		if !strings.Contains(err.Error(), "chmod 600") {
			t.Errorf("mode %#o: the error should name the fix, got: %v", mode, err)
		}
	}
}

func TestLoadAcceptsModeFourHundred(t *testing.T) {
	t.Parallel()

	// Read-only for the owner is stricter than required and must not be
	// refused: an operator who locked the file down further has done nothing
	// wrong, and rejecting it would push them towards loosening it.
	path := filepath.Join(secureDir(t), "credential")
	if err := os.WriteFile(path, []byte(sample), 0o400); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatalf("chmod: %v", err)
	}

	if _, err := credentials.Load(path); err != nil {
		t.Fatalf("mode 0400 should be accepted: %v", err)
	}
}

func TestLoadRefusesToReadThroughASymlink(t *testing.T) {
	t.Parallel()

	// Lstat rather than Stat is what makes this work: Stat would report the
	// mode and owner of the *target*, so the check would pass for a link
	// pointing at an attacker's own 0600 file.
	directory := secureDir(t)
	target := filepath.Join(directory, "elsewhere")
	if err := os.WriteFile(target, []byte(sample), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	link := filepath.Join(directory, "credential")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	_, err := credentials.Load(link)
	if err == nil {
		t.Fatal("expected a symlinked credential file to be refused")
	}
	if !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("the error should name the problem, got: %v", err)
	}
}

func TestLoadRefusesADirectory(t *testing.T) {
	t.Parallel()

	path := filepath.Join(secureDir(t), "credential")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	if _, err := credentials.Load(path); err == nil {
		t.Fatal("expected a directory to be refused")
	}
}

func TestLoadRefusesAWorldWritableDirectory(t *testing.T) {
	t.Parallel()

	// Mode 0600 on the file is no defence if another user can unlink it and
	// put their own file there.
	directory := t.TempDir()
	path := filepath.Join(directory, "credential")
	if err := os.WriteFile(path, []byte(sample), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.Chmod(directory, 0o777); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o700) })

	_, err := credentials.Load(path)
	if err == nil {
		t.Fatal("expected a world-writable credential directory to be refused")
	}
	if !strings.Contains(err.Error(), "chmod 700") {
		t.Errorf("the error should name the fix, got: %v", err)
	}
}

func TestLoadAcceptsAStickyDirectory(t *testing.T) {
	t.Parallel()

	// /tmp is group- and world-writable and still prevents one user from
	// removing another's files, so refusing a sticky directory would reject a
	// legitimate arrangement.
	directory := t.TempDir()
	path := filepath.Join(directory, "credential")
	if err := os.WriteFile(path, []byte(sample), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	// os.ModeSticky, not 0o1000: Go's FileMode spells the sticky bit as a
	// named bit of its own, and the raw octal form is silently ignored — so a
	// chmod written the C way would set 0777 and test the opposite of this.
	if err := os.Chmod(directory, 0o777|os.ModeSticky); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o700) })

	if _, err := credentials.Load(path); err != nil {
		t.Fatalf("a sticky directory should be accepted: %v", err)
	}
}

func TestLoadRefusesAnEmptyFile(t *testing.T) {
	t.Parallel()

	// Distinguished from "missing": an empty file means something went wrong
	// during enrollment, and the remedy is to enroll again rather than to
	// enroll for the first time.
	path := filepath.Join(secureDir(t), "credential")
	if err := os.WriteFile(path, []byte("  \n\t "), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	_, err := credentials.Load(path)
	if err == nil {
		t.Fatal("expected an empty credential file to be refused")
	}
	if errors.Is(err, credentials.ErrNotEnrolled) {
		t.Error("an empty file is a broken enrollment, not an absent one")
	}
	if !strings.Contains(err.Error(), "re-enroll") {
		t.Errorf("the error should name the remedy, got: %v", err)
	}
}

func TestLoadRefusesAnImplausiblyLargeFile(t *testing.T) {
	t.Parallel()

	// A credential path pointed at /dev/zero, or at a multi-gigabyte log file,
	// would otherwise be an out-of-memory condition at startup.
	path := filepath.Join(secureDir(t), "credential")
	if err := os.WriteFile(path, []byte(strings.Repeat("x", credentials.MaxFileBytes+1)), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	_, err := credentials.Load(path)
	if err == nil {
		t.Fatal("expected an oversized credential file to be refused")
	}
	if !strings.Contains(err.Error(), "not a credential") {
		t.Errorf("the error should say what it concluded, got: %v", err)
	}
}

func TestLoadTrimsTrailingWhitespace(t *testing.T) {
	t.Parallel()

	// An operator who edits the file by hand leaves a newline, and `echo`
	// adds one. Tolerating that is not laxity: a credential that failed
	// because of an invisible character is the least diagnosable failure this
	// file can have.
	path := filepath.Join(secureDir(t), "credential")
	if err := os.WriteFile(path, []byte("  "+sample+"  \n\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	loaded, err := credentials.Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if loaded != sample {
		t.Errorf("loaded = %q, want it trimmed to %q", loaded, sample)
	}
}

func TestTheErrorNamesThePathAndTheFix(t *testing.T) {
	t.Parallel()

	// An error that said only "insecure" would leave an operator guessing
	// between four different chmod commands.
	path := filepath.Join(secureDir(t), "credential")
	if err := os.WriteFile(path, []byte(sample), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatalf("chmod: %v", err)
	}

	_, err := credentials.Load(path)
	if err == nil {
		t.Fatal("expected a refusal")
	}
	var insecure *credentials.InsecureFileError
	if !errors.As(err, &insecure) {
		t.Fatalf("expected InsecureFileError, got %T", err)
	}
	if insecure.Path != path {
		t.Errorf("path = %q, want %q", insecure.Path, path)
	}
	if insecure.Fix == "" {
		t.Error("the error should carry a fix")
	}
}
