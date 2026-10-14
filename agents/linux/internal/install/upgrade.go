package install

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// UpgradeOptions is what the operator asked for.
type UpgradeOptions struct {
	// ArchivePath is a release archive already on this machine.
	ArchivePath string
	// ExpectedSHA256 is the digest the control plane showed for that archive.
	// Required, and not optional by design — see [Upgrader.Run].
	ExpectedSHA256 string
	// DryRun verifies and reports, changing nothing.
	DryRun bool
	// ConnectTimeout bounds how long the new binary has to reconnect before
	// the previous one is put back.
	ConnectTimeout time.Duration
	// Out receives the output.
	Out io.Writer
}

// Upgrader replaces the installed binary with a verified one.
type Upgrader struct {
	System  System
	Paths   Paths
	Options UpgradeOptions
	out     io.Writer
}

// NewUpgrader builds an Upgrader against the real machine.
func NewUpgrader(options UpgradeOptions) *Upgrader {
	if options.Out == nil {
		options.Out = os.Stdout
	}
	if options.ConnectTimeout == 0 {
		options.ConnectTimeout = DefaultConnectTimeout
	}
	return &Upgrader{
		System:  LinuxSystem{},
		Paths:   Paths{},
		Options: options,
		out:     options.Out,
	}
}

func (u *Upgrader) printf(format string, arguments ...any) {
	fmt.Fprintf(u.out, format, arguments...)
}

func (u *Upgrader) step(label, detail string) {
	u.printf("  ✓ %-22s %s\n", label, detail)
}

// MaxArchiveBytes bounds what the upgrader will read from an archive.
//
// The agent is about 7 MB and the archive about 3 MB. The bound exists
// because a gzip stream can decompress to an arbitrary size from a small
// file, and an upgrader that read until it ran out of memory would be a
// denial of service triggered by a corrupt download.
const MaxArchiveBytes = 128 << 20

// Run performs the upgrade.
//
// # The digest is required
//
// There is no `--skip-verify` and no mode that takes the archive on trust. The
// digest comes from the controller — which the administrator is already
// authenticated to over TLS, and which ships the manifest of the releases it
// was built alongside — so it arrives inside a session an attacker on the
// download path cannot touch. That is the whole trust model: the controller,
// not the download origin, is what vouches for the binary. A flag that let
// somebody skip it would make the model optional, and an optional model is the
// one that is off when it matters.
//
// # Rollback is the point
//
//  1. Verify the archive against the digest, before anything is unpacked.
//  2. Extract the binary into the target's own directory, under a temporary
//     name, and check that it is an ELF object for this architecture and that
//     it can report its own version.
//  3. Move the running binary aside to <path>.previous.
//  4. Rename the new one into place and restart the service.
//  5. Wait for it to reconnect — proved by the controller's configuration
//     being written, exactly as the installer does.
//  6. If it does not, put the previous binary back and restart again.
//
// The agent's credential and configuration are never touched. An upgrade that
// required re-enrolment would not be an upgrade path.
func (u *Upgrader) Run(ctx context.Context) error {
	u.printf("hcc-agent — upgrade\n")
	if u.Options.DryRun {
		u.printf("This is a dry run. Nothing will be changed.\n")
	}
	u.printf("\n")

	if euid := u.System.Geteuid(); euid != 0 && !u.Options.DryRun {
		return fmt.Errorf("upgrading needs root and this process is uid %d; re-run with sudo", euid)
	}

	installed, err := os.Stat(u.Paths.Binary())
	if err != nil {
		return fmt.Errorf(
			"there is no installation to upgrade: %s is missing.\n"+
				"Install first:  sudo ./hcc-agent install --controller https://…",
			u.Paths.Binary(),
		)
	}

	// --- 1. Verify --------------------------------------------------------
	u.printf("Verifying\n")
	expected := strings.ToLower(strings.TrimSpace(u.Options.ExpectedSHA256))
	if len(expected) != 64 {
		return fmt.Errorf(
			"--sha256 must be a 64-character hexadecimal digest, got %d characters",
			len(expected),
		)
	}
	if _, err := hex.DecodeString(expected); err != nil {
		return fmt.Errorf("--sha256 is not hexadecimal: %w", err)
	}

	actual, size, err := digestFile(u.Options.ArchivePath)
	if err != nil {
		return err
	}
	if actual != expected {
		// The one message in this program that has to be unambiguous. A
		// mismatch is either a corrupt download or a substituted one, and
		// the operator cannot tell which — so neither can this.
		return fmt.Errorf(
			"the archive does not match the digest the control plane gave.\n"+
				"  expected  %s\n"+
				"  actual    %s\n\n"+
				"Nothing has been changed. Delete the download and fetch it again. If it\n"+
				"still does not match, the bytes you are being served are not the bytes\n"+
				"the controller expects, and that is worth investigating rather than\n"+
				"working around",
			expected, actual,
		)
	}
	u.step("archive", fmt.Sprintf("%s (%d bytes)", u.Options.ArchivePath, size))
	u.step("digest", "matches the one the control plane gave")

	// --- 2. Extract and inspect -------------------------------------------
	staged := u.Paths.Binary() + ".new"
	// A previous attempt's staging file would make the create below fail with
	// O_EXCL, which is the correct behaviour for a *concurrent* upgrade and
	// the wrong one for a retry after a crash. Removed here, where it is a
	// deliberate act, rather than by opening without O_EXCL.
	if err := removeIfExists(staged); err != nil {
		return fmt.Errorf("cannot clear the staging path %s: %w", staged, err)
	}

	payload, err := extractAgentBinary(u.Options.ArchivePath)
	if err != nil {
		return err
	}
	if err := verifyELF(payload); err != nil {
		return fmt.Errorf("the archive's binary is not usable on this machine: %w", err)
	}

	if u.Options.DryRun {
		u.printf("\nWould do, in order\n")
		u.printf("  1. Write the new binary to %s\n", staged)
		u.printf("  2. Move %s to %s\n", u.Paths.Binary(), u.Paths.PreviousBinary())
		u.printf("  3. Rename %s to %s\n", staged, u.Paths.Binary())
		u.printf("  4. systemctl restart %s\n", ServiceName)
		u.printf("  5. Wait up to %s for it to reconnect\n", u.Options.ConnectTimeout)
		u.printf("  6. Put the previous binary back if it does not\n")
		u.printf("\nThe credential and the stored configuration are not touched.\n")
		u.printf("Nothing above has been done.\n")
		return nil
	}

	if err := writeFileAtomically(staged, payload, BinaryMode); err != nil {
		return err
	}
	defer func() { _ = removeIfExists(staged) }()

	// The staged binary is asked for its own version before anything is
	// swapped. An archive that unpacks to something which cannot execute on
	// this kernel — a static binary built for a newer glibc is not the risk
	// here, but a truncated one is — fails now rather than after the service
	// has been stopped.
	newVersion, err := reportedVersion(ctx, staged)
	if err != nil {
		return fmt.Errorf(
			"the new binary does not run on this machine, so nothing was changed: %w", err,
		)
	}
	currentVersion, _ := reportedVersion(ctx, u.Paths.Binary())
	u.step("new binary", fmt.Sprintf("version %s (replacing %s)", newVersion, orUnknown(currentVersion)))

	if newVersion == currentVersion {
		u.printf("\n  ! %-22s %s\n", "same version",
			"the installed binary already reports "+newVersion+"; continuing anyway")
	}

	// --- 3 & 4. Swap and restart ------------------------------------------
	u.printf("\nInstalling\n")
	if err := removeIfExists(u.Paths.PreviousBinary()); err != nil {
		return fmt.Errorf("cannot clear %s: %w", u.Paths.PreviousBinary(), err)
	}
	if err := os.Rename(u.Paths.Binary(), u.Paths.PreviousBinary()); err != nil {
		return fmt.Errorf("cannot move the running binary aside: %w", err)
	}
	u.step("previous binary", u.Paths.PreviousBinary())

	if err := os.Rename(staged, u.Paths.Binary()); err != nil {
		// Put it back immediately: a rename failure here leaves no binary at
		// all, and the service would fail to restart for a reason that has
		// nothing to do with the upgrade.
		_ = os.Rename(u.Paths.PreviousBinary(), u.Paths.Binary())
		return fmt.Errorf("cannot install the new binary: %w", err)
	}
	if err := os.Chmod(u.Paths.Binary(), installed.Mode().Perm()); err != nil {
		u.printf("  ! could not preserve the previous mode: %v\n", err)
	}
	u.step("agent binary", fmt.Sprintf("%s is now version %s", u.Paths.Binary(), newVersion))

	// The configuration is remembered before the restart, so the wait below
	// can tell a fresh connection from the file that was already there.
	previousConfigVersion := int64(-1)
	if stored, err := monitoring.Load(u.Paths.Configuration()); err == nil {
		previousConfigVersion = stored.Document.Version
	}
	configModifiedBefore := modificationTime(u.Paths.Configuration())

	if _, err := u.System.Systemctl(ctx, "restart", ServiceName); err != nil {
		u.printf("  ✗ %v\n", err)
		return u.rollback(ctx, fmt.Sprintf("the service would not restart: %v", err))
	}
	u.step("service", "restarted")

	// --- 5. Wait for it to come back --------------------------------------
	if err := u.waitForReconnect(ctx, previousConfigVersion, configModifiedBefore); err != nil {
		return u.rollback(ctx, err.Error())
	}

	u.printf("\nUpgraded to %s.\n", newVersion)
	u.printf("\nThe previous binary is kept at %s. Remove it when you are satisfied:\n",
		u.Paths.PreviousBinary())
	u.printf("  sudo rm %s\n", u.Paths.PreviousBinary())
	return nil
}

// waitForReconnect waits for evidence that the new binary connected.
//
// The evidence is the stored configuration being rewritten: the controller
// sends a snapshot on every accepted connection and the agent persists it
// before acknowledging. Both the version and the file's modification time are
// compared, because an unchanged configuration is rewritten with the same
// version — so the version alone would never change on a restart, and the
// mtime alone would be fooled by a filesystem with coarse timestamps.
func (u *Upgrader) waitForReconnect(
	ctx context.Context, previousVersion int64, modifiedBefore time.Time,
) error {
	deadline := time.Now().Add(u.Options.ConnectTimeout)
	for {
		stored, err := monitoring.Load(u.Paths.Configuration())
		if err == nil {
			rewritten := modificationTime(u.Paths.Configuration()).After(modifiedBefore)
			if stored.Document.Version != previousVersion || rewritten {
				u.step("reconnected", fmt.Sprintf(
					"the controller sent configuration v%d", stored.Document.Version,
				))
				return nil
			}
		}

		state, _ := u.System.Systemctl(ctx, "is-active", ServiceName)
		switch strings.TrimSpace(state) {
		case "failed", "inactive":
			return fmt.Errorf("the service is %s after the restart", strings.TrimSpace(state))
		}

		if time.Now().After(deadline) {
			return fmt.Errorf(
				"the upgraded agent did not reconnect within %s", u.Options.ConnectTimeout,
			)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(connectionPollInterval):
		}
	}
}

// rollback puts the previous binary back and restarts.
//
// Returns an error either way: a successful rollback still means the upgrade
// failed, and reporting success because the *recovery* worked would be the
// kind of green that hides a problem.
func (u *Upgrader) rollback(ctx context.Context, reason string) error {
	u.printf("\nRolling back\n")

	if _, err := os.Stat(u.Paths.PreviousBinary()); err != nil {
		return fmt.Errorf(
			"%s, and there is no previous binary at %s to restore.\n"+
				"The installation is left as it is; diagnose it with `hcc-agent doctor`",
			reason, u.Paths.PreviousBinary(),
		)
	}
	if err := os.Rename(u.Paths.PreviousBinary(), u.Paths.Binary()); err != nil {
		return fmt.Errorf(
			"%s, and the previous binary could not be restored: %v.\n"+
				"The agent is not running. Reinstall from a known-good archive",
			reason, err,
		)
	}
	u.printf("  ← restored the previous binary\n")

	if _, err := u.System.Systemctl(ctx, "restart", ServiceName); err != nil {
		return fmt.Errorf(
			"%s. The previous binary was restored and the service would not restart: %v",
			reason, err,
		)
	}
	u.printf("  ← restarted the service with it\n")

	if journal, err := u.System.JournalTail(ctx, ServiceName, 20); err == nil {
		if trimmed := strings.TrimSpace(journal); trimmed != "" {
			u.printf("\nThe last lines the service logged:\n")
			for _, line := range strings.Split(trimmed, "\n") {
				u.printf("  %s\n", line)
			}
		}
	}

	return fmt.Errorf(
		"the upgrade failed and was rolled back: %s.\n"+
			"The previous version is running again and the credential was never touched",
		reason,
	)
}

// --- archive handling -------------------------------------------------------

func digestFile(path string) (digest string, size int64, err error) {
	file, err := os.Open(path) //nolint:gosec // an operator-supplied path, by design
	if err != nil {
		return "", 0, fmt.Errorf("cannot read the archive %q: %w", path, err)
	}
	defer file.Close()

	hash := sha256.New()
	written, err := io.Copy(hash, io.LimitReader(file, MaxArchiveBytes+1))
	if err != nil {
		return "", 0, fmt.Errorf("cannot read the archive %q: %w", path, err)
	}
	if written > MaxArchiveBytes {
		return "", 0, fmt.Errorf(
			"%q is larger than %d bytes, so it is not a release archive", path, MaxArchiveBytes,
		)
	}
	return hex.EncodeToString(hash.Sum(nil)), written, nil
}

// extractAgentBinary pulls hcc-agent out of a release archive.
//
// # Extraction safety
//
// The archive has already been checked against a digest the controller
// supplied, so the bytes are the right bytes. These checks are for the case
// where that is not enough — an operator pointing --from at some other
// tarball, or a controller manifest built from a compromised release — and
// they are cheap:
//
//   - Only a member named exactly "hcc-agent" is read. Nothing is written to
//     a path taken from the archive, which is what makes a `../../etc/cron.d`
//     member harmless here rather than a root write.
//   - Links are refused outright.
//   - The read is bounded, so a gzip bomb is a refusal rather than the
//     machine's memory.
func extractAgentBinary(path string) ([]byte, error) {
	file, err := os.Open(path) //nolint:gosec // an operator-supplied path, by design
	if err != nil {
		return nil, fmt.Errorf("cannot read the archive %q: %w", path, err)
	}
	defer file.Close()

	decompressed, err := gzip.NewReader(file)
	if err != nil {
		return nil, fmt.Errorf("%q is not a gzip archive: %w", path, err)
	}
	defer decompressed.Close()

	archive := tar.NewReader(io.LimitReader(decompressed, MaxArchiveBytes+1))
	for {
		header, err := archive.Next()
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf(
				"%q contains no member named hcc-agent, so it is not a release archive", path,
			)
		}
		if err != nil {
			return nil, fmt.Errorf("cannot read %q: %w", path, err)
		}

		// Compared against the base name, and then required to *be* the base
		// name. A member called "evil/../hcc-agent" has a base name of
		// hcc-agent and is refused, because nothing in a flat release archive
		// has a directory component.
		if filepath.Base(header.Name) != "hcc-agent" {
			continue
		}
		if header.Name != "hcc-agent" {
			return nil, fmt.Errorf(
				"%q contains a member %q; a release archive is flat and this is not one",
				path, header.Name,
			)
		}
		if header.Typeflag == tar.TypeSymlink || header.Typeflag == tar.TypeLink {
			return nil, fmt.Errorf("%q contains hcc-agent as a link, which is refused", path)
		}
		if header.Typeflag != tar.TypeReg {
			return nil, fmt.Errorf("%q contains hcc-agent as a non-regular file", path)
		}

		payload, err := io.ReadAll(io.LimitReader(archive, MaxArchiveBytes+1))
		if err != nil {
			return nil, fmt.Errorf("cannot read hcc-agent out of %q: %w", path, err)
		}
		if len(payload) > MaxArchiveBytes {
			return nil, fmt.Errorf(
				"hcc-agent inside %q is larger than %d bytes", path, MaxArchiveBytes,
			)
		}
		return payload, nil
	}
}

// verifyELF checks that a payload is a Linux executable for this machine.
//
// Checked here rather than left to execve, because a binary for the wrong
// architecture fails with "Exec format error" from systemd after the swap —
// at which point the service is down and the message is three layers from the
// cause.
func verifyELF(payload []byte) error {
	if len(payload) < 20 || string(payload[:4]) != "\x7fELF" {
		return errors.New("it is not an ELF object")
	}
	expected, ok := elfMachineFor(GOARCH())
	if !ok {
		// A new architecture this function has not been taught about. Not an
		// error: refusing an upgrade because the checker is out of date would
		// be worse than performing one the kernel will validate anyway.
		return nil
	}
	machine := int(payload[18]) | int(payload[19])<<8
	if machine != expected {
		return fmt.Errorf(
			"it is for ELF machine %#x and this agent runs on %s (%#x)",
			machine, GOARCH(), expected,
		)
	}
	return nil
}

// elfMachineFor maps a Go architecture onto the ELF e_machine value.
func elfMachineFor(architecture string) (int, bool) {
	switch architecture {
	case "amd64":
		return 0x3E, true
	case "arm64":
		return 0xB7, true
	case "arm":
		return 0x28, true
	default:
		return 0, false
	}
}

// reportedVersion asks a binary for its own version string.
func reportedVersion(ctx context.Context, path string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, path, "version").Output() //nolint:gosec // an agent binary's own path
	if err != nil {
		return "", fmt.Errorf("`%s version` failed: %w", path, err)
	}
	fields := strings.Fields(string(output))
	if len(fields) < 2 || fields[0] != "hcc-agent" {
		return "", fmt.Errorf("`%s version` did not identify itself as hcc-agent", path)
	}
	return fields[1], nil
}

func modificationTime(path string) time.Time {
	info, err := os.Stat(path)
	if err != nil {
		return time.Time{}
	}
	return info.ModTime()
}
