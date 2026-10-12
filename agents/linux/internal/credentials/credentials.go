// Package credentials stores and loads the agent's long-lived secret.
//
// One file, one secret, and a set of refusals. The whole package exists
// because "write a string to a file" is the operation that in practice leaks
// agent credentials — through a world-readable mode, a symlink an attacker
// planted, a partial write after a crash, or a secret that survived in a
// temporary file nobody cleaned up.
//
// # What it refuses, and why each one has happened to somebody
//
//   - A file any other local user can read. A secret readable by the
//     unprivileged service account somebody runs their media server under is
//     not a secret, and silently accepting it would give a false sense of
//     security. Refused rather than fixed: silently chmod-ing a file the agent
//     did not create would hide a misconfiguration the operator needs to see.
//   - A symlink. The classic attack is a local user pointing /etc/hcc-agent's
//     credential path at a file they own, so the agent writes the secret where
//     they can read it, or reads a secret they chose. [Save] refuses to follow
//     one and [Load] refuses to read through one.
//   - A file owned by somebody else. Same shape of attack as the symlink, one
//     step cruder.
//   - A directory any other user can write to. Mode 0600 on the file is no
//     defence if another user can replace the file wholesale.
//
// # Writes are atomic
//
// [Save] writes a temporary file in the same directory, fsyncs it, and renames
// it over the target. A plain write that was interrupted — a power cut during
// enrollment is exactly the moment this matters — leaves a truncated
// credential, and a truncated credential fails authentication with no
// indication that the file is the problem. The rename is atomic on any POSIX
// filesystem, so the file is either the old credential or the new one.
//
// The temporary file is created with 0600 from the start rather than chmod-ed
// afterwards, because the window between creation and chmod is a window in
// which the secret is readable.
package credentials

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// FileMode is the only mode a credential file may have: readable and writable
// by its owner, invisible to everyone else.
const FileMode fs.FileMode = 0o600

// DirMode is the mode [Save] creates a missing credential directory with.
//
// 0700 rather than 0755: the directory listing itself is not secret, but a
// directory only the owner may traverse means a mode mistake on a file inside
// it is not immediately exploitable.
const DirMode fs.FileMode = 0o700

// MaxFileBytes bounds how much [Load] will read.
//
// A credential is 43 characters. The allowance is for a trailing newline and
// an operator's stray whitespace; the bound is what stops a credential path
// pointed at /dev/zero — or at a multi-gigabyte log file — from being an
// out-of-memory condition at startup.
const MaxFileBytes = 4096

// ErrNotEnrolled reports that no credential file exists.
//
// Distinguished from every other failure because it is the one that is not a
// problem: a freshly installed agent that has not been enrolled yet is in a
// normal state, and the right response is an instruction rather than an error.
var ErrNotEnrolled = errors.New("this agent is not enrolled")

// InsecureFileError reports a credential file the agent will not use.
//
// Carries the path and the specific problem so the message can name the fix.
// An error that said only "insecure" would leave an operator guessing between
// four different chmod commands.
type InsecureFileError struct {
	Path   string
	Reason string
	Fix    string
}

func (e *InsecureFileError) Error() string {
	if e.Fix == "" {
		return fmt.Sprintf("credential file %q is not safe to use: %s", e.Path, e.Reason)
	}
	return fmt.Sprintf(
		"credential file %q is not safe to use: %s\n  fix: %s",
		e.Path, e.Reason, e.Fix,
	)
}

// Load reads the agent credential from path.
//
// Returns [ErrNotEnrolled] when the file does not exist, so a caller can tell
// "not set up yet" from "set up wrongly" and print the right thing.
//
// Every other failure is a refusal to use a credential whose confidentiality
// the agent cannot vouch for. The checks run before the read, not after: a
// secret that has already been read into memory and then rejected has still
// been read out of a file an attacker may have substituted.
func Load(path string) (string, error) {
	// Lstat, not Stat: Stat follows symlinks, so it would report the mode and
	// owner of the *target* and the check would pass for a link an attacker
	// planted pointing at their own 0600 file.
	info, err := os.Lstat(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return "", ErrNotEnrolled
	case err != nil:
		return "", fmt.Errorf("cannot inspect credential file %q: %w", path, err)
	}

	if err := checkFile(path, info); err != nil {
		return "", err
	}

	// O_NOFOLLOW as well as the Lstat above. The Lstat result could be stale
	// by the time the open happens, and this is the one place where a
	// time-of-check-to-time-of-use race would hand the secret to whoever won
	// it. Belt and braces, cheaply.
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return "", fmt.Errorf("cannot open credential file %q: %w", path, err)
	}
	defer file.Close()

	// One byte over the limit, so a file at exactly the limit reads fully and
	// an oversized one is detectable rather than silently truncated.
	buffer := make([]byte, MaxFileBytes+1)
	read, err := file.Read(buffer)
	if err != nil && read == 0 {
		return "", fmt.Errorf("cannot read credential file %q: %w", path, err)
	}
	if read > MaxFileBytes {
		return "", &InsecureFileError{
			Path:   path,
			Reason: fmt.Sprintf("it is larger than %d bytes, so it is not a credential", MaxFileBytes),
			Fix:    "check the path; it may be pointing at the wrong file",
		}
	}

	credential := strings.TrimSpace(string(buffer[:read]))
	if credential == "" {
		return "", &InsecureFileError{
			Path:   path,
			Reason: "it is empty",
			Fix:    "re-enroll this host to obtain a credential",
		}
	}
	return credential, nil
}

// Save writes the agent credential to path, atomically and privately.
//
// Creates the parent directory with [DirMode] if it is missing, which is the
// ordinary case on a fresh install.
//
// Refuses to overwrite a symlink, for the reason in the package documentation:
// following one would write the secret wherever its target points.
func Save(path, credential string) error {
	if strings.TrimSpace(credential) == "" {
		return errors.New("refusing to write an empty credential")
	}

	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, DirMode); err != nil {
		return fmt.Errorf("cannot create credential directory %q: %w", directory, err)
	}

	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&fs.ModeSymlink != 0 {
			return &InsecureFileError{
				Path:   path,
				Reason: "it is a symbolic link, and writing through it would put the credential wherever it points",
				Fix:    "remove the link and re-run enrollment",
			}
		}
		if info.IsDir() {
			return &InsecureFileError{Path: path, Reason: "it is a directory"}
		}
	}

	// Created in the target's own directory so the rename below is on one
	// filesystem — a rename across filesystems is not atomic and falls back to
	// a copy, which reintroduces the partial-write failure this avoids.
	//
	// O_EXCL, so an attacker cannot pre-create the temporary file and have the
	// agent write the secret into something they already hold open.
	temporary := path + ".tmp"
	file, err := os.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, FileMode)
	if err != nil {
		return fmt.Errorf("cannot create temporary credential file %q: %w", temporary, err)
	}
	// Removed on every failure path below. Without this a crash mid-enrollment
	// leaves a credential sitting in a .tmp file nobody will think to delete.
	defer os.Remove(temporary)

	if _, err := file.WriteString(credential + "\n"); err != nil {
		file.Close()
		return fmt.Errorf("cannot write credential: %w", err)
	}
	// Flushed to disk before the rename. Without the sync, a crash immediately
	// after enrollment can leave the rename durable and the contents not —
	// which is an empty credential file and a spent enrollment token, the
	// worst of both.
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("cannot flush credential to disk: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("cannot close credential file: %w", err)
	}

	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("cannot install credential file %q: %w", path, err)
	}
	return nil
}

// checkFile applies the confidentiality refusals to an already-stat-ed path.
func checkFile(path string, info fs.FileInfo) error {
	if info.Mode()&fs.ModeSymlink != 0 {
		return &InsecureFileError{
			Path:   path,
			Reason: "it is a symbolic link, so the agent cannot tell whose file it would actually read",
			Fix:    "replace the link with the credential file itself",
		}
	}
	if !info.Mode().IsRegular() {
		return &InsecureFileError{
			Path:   path,
			Reason: "it is not a regular file",
			Fix:    "check the path",
		}
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		return &InsecureFileError{
			Path:   path,
			Reason: fmt.Sprintf("its permissions are %#o, so other local users can read it", mode),
			Fix:    fmt.Sprintf("chmod 600 %s", path),
		}
	}
	if err := checkOwner(path, info); err != nil {
		return err
	}
	return checkDirectory(filepath.Dir(path))
}

// checkOwner refuses a credential file owned by another user.
//
// The attack is the same shape as the symlink one and one step cruder: a local
// user creates the file first, with a credential they chose or with a mode they
// can read, and the agent uses it. Owner equality is the cheapest complete
// answer.
//
// Root is permitted to own a file the agent reads as another user, because
// that is the normal arrangement: root installs the credential during
// enrollment and the service runs as a dedicated unprivileged account.
func checkOwner(path string, info fs.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		// Not reachable on Linux, which is the only platform this agent
		// targets. Returning nil rather than an error is deliberate: refusing
		// to start because an ownership check could not be performed would be
		// worse than not performing it, on a platform where the attack it
		// prevents does not apply.
		return nil
	}
	uid := os.Getuid()
	if int(stat.Uid) != uid && stat.Uid != 0 {
		return &InsecureFileError{
			Path:   path,
			Reason: fmt.Sprintf("it is owned by uid %d, not by this process (uid %d) or root", stat.Uid, uid),
			Fix:    fmt.Sprintf("chown %d %s", uid, path),
		}
	}
	return nil
}

// checkDirectory refuses a credential directory other users may write to.
//
// Mode 0600 on the file is no defence if another user can unlink it and put
// their own file there, so the containing directory has to be checked too.
// The sticky bit is honoured: /tmp is group- and world-writable and still
// prevents one user from removing another's files, so refusing a sticky
// directory would reject a legitimate arrangement.
func checkDirectory(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("cannot inspect credential directory %q: %w", path, err)
	}
	mode := info.Mode()
	if mode&fs.ModeSticky != 0 {
		return nil
	}
	if mode.Perm()&0o022 != 0 {
		return &InsecureFileError{
			Path:   path,
			Reason: fmt.Sprintf("the directory's permissions are %#o, so other local users can replace the credential file", mode.Perm()),
			Fix:    fmt.Sprintf("chmod 700 %s", path),
		}
	}
	return nil
}
