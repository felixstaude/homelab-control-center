package monitoring

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

// FileMode and DirMode are the permissions the stored configuration gets.
//
// 0600 even though a configuration document holds no secret. Two reasons: it
// lives in the same directory as the credential, which is 0700, so a laxer
// file mode would buy nobody read access it did not already have; and a single
// mode for everything the agent writes is one fewer thing to get wrong when a
// future document *does* hold something sensitive.
const (
	FileMode fs.FileMode = 0o600
	DirMode  fs.FileMode = 0o700
)

// MaxStoredBytes bounds what Load will read.
//
// A configuration for the five modules in this phase is about a kilobyte. The
// bound exists because the file is read at startup, before anything has
// validated it, and a 2 GiB file — however it got there — should be a clear
// refusal rather than the agent's memory.
const MaxStoredBytes = 256 * 1024

// ErrNoStoredConfig reports that this agent has no local configuration.
//
// A normal state, not a failure: a freshly enrolled agent has never been given
// one. The caller connects and asks, rather than treating it as an error —
// which is why this is a sentinel to compare against and not a wrapped
// filesystem error whose cause a caller would have to inspect.
var ErrNoStoredConfig = errors.New("this agent has no stored configuration")

// Stored is a configuration document on disk, with the bytes it arrived as.
//
// Raw is kept because the checksum covers *bytes*, not a re-serialisation
// (see [Checksum]). Without it, an agent restarting could report which version
// it holds but not prove the file still matches the digest it acknowledged —
// so a half-written or edited file would be reported as applied.
type Stored struct {
	Raw      []byte
	Checksum string
	Document Document
}

// Load reads the stored configuration.
//
// Returns [ErrNoStoredConfig] when there is none. A file that exists but does
// not parse, or whose content fails validation, is returned as an error: this
// runs at startup, and an agent that silently discarded a corrupt
// configuration would come up collecting nothing while reporting itself
// healthy. The operator needs to know, and the controller will send a fresh
// document on the next connection anyway.
func Load(path string) (Stored, error) {
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return Stored{}, ErrNoStoredConfig
		}
		return Stored{}, fmt.Errorf("cannot read configuration %q: %w", path, err)
	}
	if info.Mode()&fs.ModeSymlink != 0 {
		// Refused rather than followed. The same local attack the credential
		// loader refuses: point the path at a file you control and the agent
		// reads a configuration you chose — which, since a configuration says
		// what to collect and how often, is a way to make somebody else's
		// agent do work you specified.
		return Stored{}, fmt.Errorf(
			"refusing to read configuration %q: it is a symbolic link", path,
		)
	}
	if info.IsDir() {
		return Stored{}, fmt.Errorf("refusing to read configuration %q: it is a directory", path)
	}
	if info.Size() > MaxStoredBytes {
		return Stored{}, fmt.Errorf(
			"configuration %q is %d bytes, over the %d byte limit",
			path, info.Size(), MaxStoredBytes,
		)
	}

	raw, err := os.ReadFile(path) //nolint:gosec // path is the agent's own configured location
	if err != nil {
		return Stored{}, fmt.Errorf("cannot read configuration %q: %w", path, err)
	}

	document, problems, err := Parse(raw)
	if err != nil {
		return Stored{}, fmt.Errorf("stored configuration %q is unusable: %w", path, err)
	}
	if len(problems) > 0 {
		return Stored{}, fmt.Errorf(
			"stored configuration %q is invalid: %s", path, problems[0],
		)
	}

	return Stored{Raw: raw, Checksum: Checksum(raw), Document: document}, nil
}

// Save writes a configuration atomically.
//
// Temporary file in the same directory, fsync, rename — so a crash mid-write
// leaves the previous configuration intact rather than a half-written one
// (AGENT_ARCHITECTURE.md §6.3). The same discipline as the credential writer,
// and for a sharper reason here: this file is read at startup to decide what
// to collect, so a truncated one would be a host that comes up collecting
// nothing after a power cut.
//
// Takes the raw bytes rather than a [Document], because those are the bytes
// the checksum covers. Writing a re-serialised document would store something
// whose digest no longer matches what was acknowledged.
func Save(path string, raw []byte) error {
	if len(raw) == 0 {
		return errors.New("refusing to write an empty configuration")
	}
	if len(raw) > MaxStoredBytes {
		return fmt.Errorf("refusing to write %d bytes of configuration", len(raw))
	}

	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, DirMode); err != nil {
		return fmt.Errorf("cannot create configuration directory %q: %w", directory, err)
	}

	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&fs.ModeSymlink != 0 {
			return fmt.Errorf(
				"refusing to write configuration %q: it is a symbolic link", path,
			)
		}
		if info.IsDir() {
			return fmt.Errorf("refusing to write configuration %q: it is a directory", path)
		}
	}

	// In the target's own directory, so the rename is on one filesystem — a
	// cross-filesystem rename is not atomic and falls back to a copy, which
	// reintroduces the partial write this avoids.
	//
	// O_EXCL and O_NOFOLLOW so a pre-created temporary file or a symlink
	// planted at that path is a refusal rather than a write somewhere else.
	temporary := path + ".tmp"
	file, err := os.OpenFile(
		temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, FileMode,
	)
	if err != nil {
		return fmt.Errorf("cannot create temporary configuration %q: %w", temporary, err)
	}
	defer os.Remove(temporary)

	if _, err := file.Write(raw); err != nil {
		file.Close()
		return fmt.Errorf("cannot write configuration: %w", err)
	}
	// Flushed before the rename: without it a crash can leave the rename
	// durable and the contents not, which is an empty configuration file
	// presented as a valid one.
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("cannot flush configuration to disk: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("cannot close configuration file: %w", err)
	}

	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("cannot install configuration %q: %w", path, err)
	}
	return nil
}
