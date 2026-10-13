// Package install owns the agent's installation lifecycle: install, upgrade,
// uninstall, and the systemd unit all three agree about.
//
// # Why the binary installs itself
//
// There is no installation shell script, and that is a decision rather than an
// omission. The artifact a person verifies with `sha256sum -c` is the artifact
// that performs the installation and then *is* the installed program, so there
// is no second thing to trust. A script's whole job — create a user, place a
// binary, write a unit, set modes — is work this agent can do for itself, in a
// language with types, tests and no quoting rules.
//
// The alternative everyone reaches for, `curl … | sudo bash`, fails three ways
// at once: unaudited code runs as root, the credential arrives in argv where
// /proc makes it world-readable, and the whole command lands in shell history.
// `install --dry-run` is the audit step a pipe into a shell cannot offer, and
// it costs one flag.
//
// # Everything privileged goes through System
//
// The installer creates users, runs systemctl and writes into /etc. None of
// that is testable as a direct call, so all of it is behind [System] and every
// path is rooted at [Paths.Root] — which is "" in production and a temporary
// directory in a test. The result is that the ordering, the refusals and the
// rollback are unit-tested without root, and the parts that genuinely need a
// real machine (does the unit start? does the service connect?) are tested by
// installing into a real container with real systemd rather than by mocking
// systemd and calling it proof.
package install

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
)

// Service and account names. One constant each, because these appear in the
// unit file, in the journal identifier, in the user lookup and in the
// uninstaller, and a typo in one of four copies is a service that installs and
// cannot be removed.
const (
	// ServiceName is the systemd unit's name, without the .service suffix.
	ServiceName = "hcc-agent"
	// ServiceUser is the dedicated unprivileged account the service runs as.
	// Also used as the group name: a service account whose group is shared
	// with anything else is a service account whose files are readable by
	// whatever else is in that group.
	ServiceUser = "hcc-agent"
)

// Default locations. Deliberately not configurable by a flag.
//
// A --prefix would multiply the installer's test surface by every path it
// touches, and would mean the committed unit file is one of many possible
// renderings rather than *the* rendering. An operator who needs different
// paths has a packaging problem, not an installer problem, and
// agents/linux/deploy/README.md still documents the manual procedure.
const (
	DefaultBinaryPath = "/usr/local/bin/hcc-agent"
	DefaultUnitPath   = "/etc/systemd/system/" + ServiceName + ".service"
	DefaultConfigDir  = "/etc/hcc-agent"
	DefaultEnvFile    = DefaultConfigDir + "/agent.env"
	DefaultCAPath     = DefaultConfigDir + "/ca.pem"
	// PreviousBinarySuffix is appended to the installed binary's path to keep
	// the version being replaced. Upgrade restores it when the new binary does
	// not come up; install restores it when a rollback is needed.
	PreviousBinarySuffix = ".previous"
)

// Modes for everything the installer creates.
//
// The configuration directory is 0750 and owned by the service account rather
// than by root, because the agent **writes** into it: it persists the
// monitoring configuration the controller sends so that a restart resumes
// collecting what it was last told to. A root-owned directory leaves the agent
// connected and permanently unable to apply a configuration, which is a defect
// this project has already shipped once.
const (
	BinaryMode    os.FileMode = 0o755
	UnitMode      os.FileMode = 0o644
	ConfigDirMode os.FileMode = 0o750
	// The environment file holds no secret — the credential is in its own
	// 0600 file — which is what lets an administrator read the controller URL
	// without sudo and still not be able to read the credential.
	EnvFileMode os.FileMode = 0o644
	// A CA certificate is public by construction.
	CAFileMode os.FileMode = 0o644
)

// ErrNoSuchUser reports that an account does not exist.
//
// Its own sentinel rather than os/user.UnknownUserError, so a [System] fake
// does not have to construct a type from the standard library to say "absent".
var ErrNoSuchUser = errors.New("no such user")

// UserInfo is what the installer needs to know about an account.
type UserInfo struct {
	Name string
	UID  int
	GID  int
	// Shell is the login shell, used to tell a system account apart from a
	// person's account. An empty string means the platform did not report one.
	Shell string
}

// IsSystemAccount reports whether this looks like a service account rather
// than a person.
//
// Two tests, and both have to pass. A uid below 1000 is what `useradd
// --system` allocates and what login.defs treats as outside the human range.
// A nologin shell is the other half: a uid-500 account with /bin/bash is
// somebody's legacy login, and adopting it would hand them read access to this
// agent's credential.
func (u UserInfo) IsSystemAccount() bool {
	if u.UID >= 1000 {
		return false
	}
	// An unknown shell counts as a system account. The shell is read from
	// /etc/passwd as a best effort, and an unreadable passwd file must not
	// turn an ordinary service account into a refusal — the uid check has
	// already excluded the human range. Checked before filepath.Base, which
	// maps "" to "." and would otherwise fall through to the default.
	if strings.TrimSpace(u.Shell) == "" {
		return true
	}
	switch filepath.Base(u.Shell) {
	case "nologin", "false":
		return true
	default:
		return false
	}
}

// System is every operation the installer cannot perform with the standard
// library alone, or cannot perform in a test.
//
// Narrow on purpose. Each method is one privileged effect, so a fake
// implementation is a handful of recorded calls rather than a simulated
// operating system.
type System interface {
	// Geteuid reports the effective user id of this process.
	Geteuid() int
	// SystemdPresent reports whether this machine is running systemd as pid 1.
	SystemdPresent() bool
	// LookupUser resolves an account by name, returning [ErrNoSuchUser] when
	// it does not exist.
	LookupUser(name string) (UserInfo, error)
	// CreateServiceAccount creates a system group and a system user of the
	// same name, with no home directory and no login shell. Idempotent: an
	// existing account of the right shape is left alone.
	CreateServiceAccount(ctx context.Context, name string) error
	// DeleteServiceAccount removes an account and its group.
	DeleteServiceAccount(ctx context.Context, name string) error
	// Systemctl runs one systemctl invocation, returning its combined output.
	Systemctl(ctx context.Context, args ...string) (string, error)
	// JournalTail returns the last lines the unit logged, for the one case
	// where the installer has to explain a failure it did not cause.
	JournalTail(ctx context.Context, unit string, lines int) (string, error)
	// MachineArchitecture reports `uname -m`, for the architecture advisory.
	MachineArchitecture() string
	// Chown changes a path's ownership. Behind the interface because a test
	// running as an ordinary user cannot chown to another uid.
	Chown(path string, uid, gid int) error
}

// Paths is where an installation lives, rooted so tests can point the whole
// layout at a temporary directory.
//
// Root is "" in production. A test sets it to a temp directory and every path
// below moves with it, which is what makes the step ordering and the rollback
// testable without root and without touching the machine running the test.
type Paths struct {
	Root string
}

func (p Paths) join(absolute string) string {
	if p.Root == "" {
		return absolute
	}
	return filepath.Join(p.Root, absolute)
}

func (p Paths) Binary() string         { return p.join(DefaultBinaryPath) }
func (p Paths) PreviousBinary() string { return p.join(DefaultBinaryPath + PreviousBinarySuffix) }
func (p Paths) Unit() string           { return p.join(DefaultUnitPath) }
func (p Paths) ConfigDir() string      { return p.join(DefaultConfigDir) }
func (p Paths) EnvFile() string        { return p.join(DefaultEnvFile) }
func (p Paths) CAFile() string         { return p.join(DefaultCAPath) }
func (p Paths) Credential() string     { return p.join(config.DefaultCredentialFile) }
func (p Paths) Configuration() string  { return p.join(config.DefaultConfigFile) }

// SystemdRuntimeDir is the directory whose existence means systemd is pid 1.
//
// Checked by path rather than by running `systemctl is-system-running`, which
// answers "degraded" on a perfectly usable machine and "offline" inside a
// container where systemd is present but not booted. The directory is what
// systemd itself documents as the test.
const SystemdRuntimeDir = "/run/systemd/system"

// LinuxSystem is the production [System].
type LinuxSystem struct {
	// Root mirrors [Paths.Root] for the one check that reads a fixed absolute
	// path. Set by the installer so a rooted test does not consult the real
	// /run/systemd/system.
	Root string
}

func (s LinuxSystem) Geteuid() int { return os.Geteuid() }

func (s LinuxSystem) SystemdPresent() bool {
	path := SystemdRuntimeDir
	if s.Root != "" {
		path = filepath.Join(s.Root, SystemdRuntimeDir)
	}
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func (s LinuxSystem) LookupUser(name string) (UserInfo, error) {
	entry, err := user.Lookup(name)
	if err != nil {
		var unknown user.UnknownUserError
		if errors.As(err, &unknown) {
			return UserInfo{}, ErrNoSuchUser
		}
		return UserInfo{}, fmt.Errorf("cannot look up user %q: %w", name, err)
	}
	uid, err := strconv.Atoi(entry.Uid)
	if err != nil {
		return UserInfo{}, fmt.Errorf("user %q has a non-numeric uid %q", name, entry.Uid)
	}
	gid, err := strconv.Atoi(entry.Gid)
	if err != nil {
		return UserInfo{}, fmt.Errorf("user %q has a non-numeric gid %q", name, entry.Gid)
	}
	// os/user's pure-Go reader does not expose the shell, so it is read from
	// /etc/passwd directly. A missing shell is reported as empty rather than
	// as an error: the account exists either way, and the shell only sharpens
	// the system-account test.
	return UserInfo{Name: name, UID: uid, GID: gid, Shell: shellOf(name)}, nil
}

// shellOf reads one field out of /etc/passwd.
//
// os/user.User has no Shell field, and cgo-free builds — which this agent
// always is — cannot ask libc. Best effort: an unreadable passwd file means
// the shell is unknown, not that the account is unsafe.
func shellOf(name string) string {
	contents, err := os.ReadFile("/etc/passwd")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(contents), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) >= 7 && fields[0] == name {
			return fields[6]
		}
	}
	return ""
}

// nologinShells are the paths a service account's shell may be, in the order
// they are tried.
//
// Three because the distributions disagree: Debian and Ubuntu put nologin in
// /usr/sbin, Fedora and RHEL in /sbin, and a minimal image may have neither —
// at which point /bin/false is the honest fallback, since the property being
// bought is "this account cannot log in" rather than any particular binary.
var nologinShells = []string{"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}

func nologinShell() string {
	for _, candidate := range nologinShells {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
	}
	return "/bin/false"
}

// CreateServiceAccount creates the group and the user.
//
// Two tools are tried because the distributions ship different ones:
// shadow-utils' useradd/groupadd on Debian, Ubuntu, Fedora, Arch and openSUSE,
// and busybox's adduser/addgroup on Alpine. An "already exists" exit is
// success, which is what makes a re-run of a partly completed install
// idempotent rather than a failure.
func (s LinuxSystem) CreateServiceAccount(ctx context.Context, name string) error {
	shell := nologinShell()

	if _, err := exec.LookPath("groupadd"); err == nil {
		if err := runAllowingExists(ctx, "groupadd", "--system", name); err != nil {
			return err
		}
		return runAllowingExists(ctx, "useradd",
			"--system",
			"--gid", name,
			"--no-create-home",
			// Not /home/hcc-agent: the account has no home and saying so
			// explicitly stops useradd's default from creating one later.
			"--home-dir", "/nonexistent",
			"--shell", shell,
			"--comment", "Homelab Control Center agent",
			name,
		)
	}

	if _, err := exec.LookPath("addgroup"); err == nil {
		// busybox: -S system, -D no password, -H no home directory,
		// -G primary group, -g GECOS.
		if err := runAllowingExists(ctx, "addgroup", "-S", name); err != nil {
			return err
		}
		return runAllowingExists(ctx, "adduser",
			"-S", "-D", "-H", "-s", shell, "-G", name, "-g", "Homelab Control Center agent", name,
		)
	}

	return fmt.Errorf(
		"no user-creation tool found: neither useradd nor adduser is on PATH. "+
			"Create the %q system account by hand and re-run",
		name,
	)
}

func (s LinuxSystem) DeleteServiceAccount(ctx context.Context, name string) error {
	if _, err := exec.LookPath("userdel"); err == nil {
		// userdel removes the user's own group on most distributions; the
		// groupdel afterwards covers the ones where it does not, and a missing
		// group is not an error.
		if err := runAllowingMissing(ctx, "userdel", name); err != nil {
			return err
		}
		return runAllowingMissing(ctx, "groupdel", name)
	}
	if _, err := exec.LookPath("deluser"); err == nil {
		if err := runAllowingMissing(ctx, "deluser", name); err != nil {
			return err
		}
		return runAllowingMissing(ctx, "delgroup", name)
	}
	return errors.New("no user-removal tool found: neither userdel nor deluser is on PATH")
}

// exitCodeUserExists is shadow-utils' "the name is already in use". Treated as
// success so an interrupted install can be re-run.
const exitCodeUserExists = 9

func runAllowingExists(ctx context.Context, name string, args ...string) error {
	output, err := runCommand(ctx, name, args...)
	if err == nil {
		return nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == exitCodeUserExists {
		return nil
	}
	// busybox reports an existing account with exit 1 and a message, so the
	// text is checked too. Ugly, and better than failing an idempotent
	// operation on Alpine.
	if strings.Contains(strings.ToLower(output), "already exists") ||
		strings.Contains(strings.ToLower(output), "in use") {
		return nil
	}
	return fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(output))
}

func runAllowingMissing(ctx context.Context, name string, args ...string) error {
	output, err := runCommand(ctx, name, args...)
	if err == nil {
		return nil
	}
	lowered := strings.ToLower(output)
	if strings.Contains(lowered, "does not exist") || strings.Contains(lowered, "unknown") {
		return nil
	}
	return fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(output))
}

func (s LinuxSystem) Systemctl(ctx context.Context, args ...string) (string, error) {
	output, err := runCommand(ctx, "systemctl", args...)
	if err != nil {
		return output, fmt.Errorf(
			"systemctl %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(output),
		)
	}
	return output, nil
}

func (s LinuxSystem) JournalTail(ctx context.Context, unit string, lines int) (string, error) {
	output, err := runCommand(ctx, "journalctl",
		"-u", unit, "-n", strconv.Itoa(lines), "--no-pager", "--output", "short-iso",
	)
	if err != nil {
		return "", fmt.Errorf("journalctl -u %s: %w", unit, err)
	}
	return output, nil
}

func (s LinuxSystem) MachineArchitecture() string {
	var buffer syscall.Utsname
	if err := syscall.Uname(&buffer); err != nil {
		return ""
	}
	machine := make([]byte, 0, len(buffer.Machine))
	for _, character := range buffer.Machine {
		if character == 0 {
			break
		}
		machine = append(machine, byte(character))
	}
	return string(machine)
}

func (s LinuxSystem) Chown(path string, uid, gid int) error {
	// Lchown, not Chown: following a symlink here would change the ownership
	// of whatever it points at, which is the whole shape of the attack the
	// credential loader already refuses.
	if err := os.Lchown(path, uid, gid); err != nil {
		return fmt.Errorf("cannot change ownership of %q: %w", path, err)
	}
	return nil
}

// commandTimeout bounds one privileged command.
//
// useradd and systemctl are fast or stuck; neither has a legitimate reason to
// take half a minute, and an installer that hangs for ever on `systemctl
// daemon-reload` against a wedged systemd is worse than one that reports the
// timeout.
const commandTimeout = 30 * time.Second

func runCommand(ctx context.Context, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, commandTimeout)
	defer cancel()
	command := exec.CommandContext(ctx, name, args...) //nolint:gosec // fixed names, no shell
	// An empty environment except for PATH and LANG. These tools read
	// configuration from the environment in places, and inheriting an
	// operator's locale and editor settings into useradd is noise at best.
	command.Env = []string{
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"LC_ALL=C",
	}
	output, err := command.CombinedOutput()
	return string(output), err
}

// GOARCH exposes the architecture this binary was built for, so the preflight
// advisory can compare it against the machine's.
func GOARCH() string { return runtime.GOARCH }

// GOOS exposes the operating system this binary was built for.
func GOOS() string { return runtime.GOOS }
