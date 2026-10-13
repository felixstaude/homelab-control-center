package install

import (
	"context"
	"embed"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

//go:embed hcc-agent.service
var unitFiles embed.FS

// UnitFile returns the exact bytes the installer writes to
// /etc/systemd/system/hcc-agent.service.
//
// Embedded rather than templated, and there is deliberately only one copy in
// the repository. The file in this package *is* the installed unit; `install
// --dry-run` prints these bytes verbatim, which is what makes the dry run an
// audit rather than a summary. A second copy under deploy/ existed until the
// installer took ownership of it, and a second copy is a copy that drifts.
func UnitFile() []byte {
	contents, err := unitFiles.ReadFile("hcc-agent.service")
	if err != nil {
		// Unreachable: the file is embedded at compile time, so a failure
		// here means the binary is corrupt.
		panic("the systemd unit is missing from this binary: " + err.Error())
	}
	return contents
}

// Enrollment is the outcome of spending a one-time credential.
type Enrollment struct {
	HostID   string
	HostName string
	// Credential is the long-lived agent credential. Held only long enough to
	// write it to a file.
	Credential         string
	SupersededPrevious bool
}

// EnrollFunc exchanges a one-time credential for a durable one.
//
// A function rather than an interface so a test supplies a closure, and so
// this package does not have to construct a controller client to be testable.
// [DefaultEnroll] is the production implementation.
type EnrollFunc func(
	ctx context.Context, controllerURL, caFile, enrollmentCredential string,
) (*Enrollment, error)

// ProbeFunc checks that a controller is reachable and its certificate
// verifies. [DefaultProbe] is the production implementation.
type ProbeFunc func(ctx context.Context, controllerURL, caFile string) (string, error)

// Options is what the operator asked for.
type Options struct {
	// ControllerURL is the base URL of the control plane. Required.
	ControllerURL string
	// CAFile is a PEM bundle to trust in addition to the system roots, as the
	// operator named it. Copied into the configuration directory so the
	// service does not depend on a path in somebody's home.
	CAFile string
	// TokenSource is where the enrollment credential comes from.
	TokenSource TokenSource
	// Force re-enrolls a host that is already enrolled, which revokes the
	// credential the running agent is using.
	Force bool
	// DryRun prints the plan and changes nothing.
	DryRun bool
	// SourceBinary is the executable to install. Empty means this process's
	// own executable, read through /proc/self/exe so that a binary which has
	// since been moved or deleted still installs.
	SourceBinary string
	// ConnectTimeout bounds how long the installer waits for the started
	// service to connect and receive a configuration.
	ConnectTimeout time.Duration
	// ProcRoot is where /proc is mounted. Tests point it at a fixture.
	ProcRoot string
	// Enroll and Probe are injected for tests.
	Enroll EnrollFunc
	Probe  ProbeFunc
	// Out receives the installer's progress output.
	Out io.Writer
}

// DefaultConnectTimeout is how long "installed" waits to mean "connected".
//
// Thirty seconds. The agent's first connection attempt is immediate and a
// controller on a LAN answers in milliseconds, so this is almost entirely
// headroom for a slow link and a cold reverse proxy. Long enough that a
// working installation is never reported as a failure; short enough that a
// genuinely broken one does not look like a hang.
const DefaultConnectTimeout = 30 * time.Second

// Mode is what an installation run has found already on the machine.
type Mode int

const (
	// ModeFresh: nothing of an installation is present. The full sequence
	// runs, including enrollment.
	ModeFresh Mode = iota
	// ModeIncomplete: a credential exists but the service does not. A
	// previous run got part-way — interrupted, or stopped by a failure after
	// enrollment — and this run finishes it **without** enrolling again.
	//
	// This is what makes the installer idempotent in the case that matters.
	// Without it, the remedy for a half-finished install would be --force,
	// which spends a second one-time credential to fix a problem that has
	// nothing to do with credentials.
	ModeIncomplete
	// ModeInstalled: binary, credential and unit are all present. Refused
	// unless --force, because re-enrolling disconnects a working agent.
	ModeInstalled
)

func (m Mode) String() string {
	switch m {
	case ModeFresh:
		return "fresh"
	case ModeIncomplete:
		return "incomplete"
	case ModeInstalled:
		return "already installed"
	default:
		return "unknown"
	}
}

// Detect reports what is already present.
func Detect(paths Paths) Mode {
	_, credentialErr := credentials.Load(paths.Credential())
	enrolled := credentialErr == nil

	_, unitErr := os.Stat(paths.Unit())
	_, binaryErr := os.Stat(paths.Binary())
	serviced := unitErr == nil && binaryErr == nil

	switch {
	case enrolled && serviced:
		return ModeInstalled
	case enrolled:
		return ModeIncomplete
	default:
		return ModeFresh
	}
}

// Installer performs an installation.
type Installer struct {
	System  System
	Paths   Paths
	Options Options

	out io.Writer
	// undo is a stack of reversals for resources *this run* created, unwound
	// in reverse on failure. Nothing a previous run created is ever in it,
	// which is the property that stops a failed install from destroying a
	// working one.
	undo []undoStep
	// enrolled records that a one-time credential was spent in this run. The
	// credential file is never rolled back — see rollback.
	enrolled bool
	account  UserInfo
}

type undoStep struct {
	what string
	run  func(ctx context.Context) error
}

// New builds an Installer with the production system and sensible defaults.
func New(options Options) *Installer {
	if options.Out == nil {
		options.Out = os.Stdout
	}
	if options.ConnectTimeout == 0 {
		options.ConnectTimeout = DefaultConnectTimeout
	}
	if options.Enroll == nil {
		options.Enroll = DefaultEnroll
	}
	if options.Probe == nil {
		options.Probe = DefaultProbe
	}
	return &Installer{
		System:  LinuxSystem{},
		Paths:   Paths{},
		Options: options,
		out:     options.Out,
	}
}

func (i *Installer) printf(format string, arguments ...any) {
	fmt.Fprintf(i.out, format, arguments...)
}

func (i *Installer) step(label, detail string) {
	i.printf("  ✓ %-24s %s\n", label, detail)
}

// Run installs the agent.
//
// The order is not arbitrary, and each step is a refusal rather than a warning
// when it cannot be done safely:
//
//  1. Preflight. Everything knowable without a side effect, in one pass.
//  2. Reach the controller and verify its certificate — before creating a
//     user or writing a file, because a typo in a hostname should not leave
//     debris on the machine.
//  3. The service account.
//  4. The configuration directory, owned by that account because the agent
//     writes into it.
//  5. The binary, placed atomically, with any previous one kept aside.
//  6. The environment file.
//  7. Enrollment: the credential is read, exchanged for a durable one, and
//     written at 0600 owned by the service account. The one-time credential
//     is never written to disk by the installer at all — it goes from the
//     terminal to one HTTPS request.
//  8. The unit, then daemon-reload and enable --now.
//  9. Wait for the service to connect and receive a configuration, which is
//     what makes "installed" mean "connected" rather than "files placed".
func (i *Installer) Run(ctx context.Context) error {
	info := buildinfo.Get()
	if i.Options.DryRun {
		i.printf("hcc-agent %s — installation plan\n", info.Version)
		i.printf("This is a dry run. Nothing below will be changed.\n\n")
	} else {
		i.printf("hcc-agent %s — installing\n\n", info.Version)
	}

	mode := Detect(i.Paths)

	// --- 1. Preflight -----------------------------------------------------
	i.printf("Preflight\n")
	preflight := RunPreflight(ctx, i.System, i.Paths, i.Options)
	for _, check := range preflight.Passed {
		i.step(check.Name, check.Detail)
	}
	for _, check := range preflight.Advisories {
		i.printf("  ! %-24s %s\n", check.Name, check.Detail)
		if check.Fix != "" {
			i.printf("    %s\n", check.Fix)
		}
	}
	for _, check := range preflight.Problems {
		i.printf("  ✗ %-24s %s\n", check.Name, check.Detail)
		if check.Fix != "" {
			i.printf("    fix: %s\n", check.Fix)
		}
	}
	// A dry run reports the problems and carries on, deliberately. The point
	// of --dry-run is to let somebody read what an installation would do
	// *before* running it as root — so requiring root in order to see the
	// plan would defeat it, and so would stopping at the first refusal. The
	// problems are printed above and counted again at the end.
	if !preflight.OK() && !i.Options.DryRun {
		i.printf("\n")
		return preflight.Error()
	}

	if mode == ModeInstalled && !i.Options.Force && !i.Options.DryRun {
		return fmt.Errorf(
			"this host already has a complete installation.\n"+
				"  To replace the binary:            hcc-agent upgrade\n"+
				"  To diagnose a problem:            hcc-agent doctor\n"+
				"  To re-enroll (disconnects the running agent): hcc-agent install --force "+
				"--controller %s",
			i.Options.ControllerURL,
		)
	}
	if mode == ModeIncomplete && !i.Options.Force {
		i.printf("\n  ! %-24s %s\n", "existing credential",
			"this host is enrolled but has no service; completing the installation "+
				"without enrolling again")
	}

	// --- 2. The controller ------------------------------------------------
	i.printf("\nController\n")
	description, err := i.Options.Probe(ctx, i.Options.ControllerURL, i.Options.CAFile)
	if err != nil {
		i.printf("  ✗ %-24s %v\n", i.Options.ControllerURL, err)
		return fmt.Errorf("the controller is not usable from this host: %w", err)
	}
	i.step(i.Options.ControllerURL, description)

	// --- Dry run ----------------------------------------------------------
	if i.Options.DryRun {
		i.describePlan(mode)
		if !preflight.OK() {
			i.printf(
				"\n%d problem(s) above would stop this installation. "+
					"The plan is shown anyway so it can be read.\n", len(preflight.Problems),
			)
			return preflight.Error()
		}
		return nil
	}

	// --- Everything below changes the machine -----------------------------
	i.printf("\nInstalling\n")

	if err := i.apply(ctx, mode); err != nil {
		// One failure is deliberately not rolled back: the files are correct,
		// the credential is spent, and the agent has not connected. Removing a
		// correct installation would be the wrong response to somebody's
		// firewall, and would cost them a second one-time credential.
		if !InstalledNotConnected(err) {
			i.rollback(ctx)
		}
		return err
	}

	i.reportSuccess()
	return nil
}

// apply runs the mutating steps, leaving rollback to the caller.
func (i *Installer) apply(ctx context.Context, mode Mode) error {
	if err := i.ensureAccount(ctx); err != nil {
		return err
	}
	if err := i.ensureConfigDirectory(ctx); err != nil {
		return err
	}
	if err := i.installCA(ctx); err != nil {
		return err
	}
	if err := i.placeBinary(ctx); err != nil {
		return err
	}
	if err := i.writeEnvironmentFile(ctx); err != nil {
		return err
	}
	if mode == ModeFresh || i.Options.Force {
		if err := i.enroll(ctx); err != nil {
			return err
		}
	} else {
		i.step("credential", i.Paths.Credential()+" (kept; this host is already enrolled)")
	}
	if err := i.writeUnit(ctx); err != nil {
		return err
	}
	if err := i.startService(ctx); err != nil {
		return err
	}
	return i.waitForConnection(ctx)
}

// ensureAccount creates the service account, or adopts an existing one.
func (i *Installer) ensureAccount(ctx context.Context) error {
	existing, err := i.System.LookupUser(ServiceUser)
	switch {
	case err == nil:
		if !existing.IsSystemAccount() {
			// Refused rather than adopted. A login account called hcc-agent
			// belongs to somebody, and running the service as them would give
			// them read access to this agent's credential — and give the
			// agent whatever they can reach.
			return fmt.Errorf(
				"a user account %q already exists with uid %d and shell %q, "+
					"which is a login account rather than a service account.\n"+
					"The installer will not run the agent as somebody's user. "+
					"Remove or rename that account, or install by hand",
				ServiceUser, existing.UID, existing.Shell,
			)
		}
		i.account = existing
		i.step("service account", fmt.Sprintf(
			"%s (uid %d, already present)", ServiceUser, existing.UID,
		))
		return nil
	case !errors.Is(err, ErrNoSuchUser):
		return fmt.Errorf("cannot check for the %q account: %w", ServiceUser, err)
	}

	if err := i.System.CreateServiceAccount(ctx, ServiceUser); err != nil {
		return fmt.Errorf("cannot create the %q service account: %w", ServiceUser, err)
	}
	created, err := i.System.LookupUser(ServiceUser)
	if err != nil {
		return fmt.Errorf(
			"the %q account was created and cannot be looked up: %w", ServiceUser, err,
		)
	}
	i.account = created
	i.pushUndo("remove the "+ServiceUser+" account", func(ctx context.Context) error {
		// Not removed if the configuration directory survived: the credential
		// inside it is owned by this uid, and recreating the account later
		// may allocate a different one — which would leave a credential the
		// service cannot read.
		if _, err := os.Stat(i.Paths.ConfigDir()); err == nil {
			return nil
		}
		return i.System.DeleteServiceAccount(ctx, ServiceUser)
	})
	i.step("service account", fmt.Sprintf("%s (uid %d, created)", ServiceUser, created.UID))
	return nil
}

func (i *Installer) ensureConfigDirectory(ctx context.Context) error {
	path := i.Paths.ConfigDir()
	_, err := os.Stat(path)
	existed := err == nil

	if err := os.MkdirAll(path, ConfigDirMode); err != nil {
		return fmt.Errorf("cannot create %s: %w", path, err)
	}
	// MkdirAll honours the umask, so the mode is set explicitly afterwards.
	// A umask of 022 would otherwise produce 0750 & ~022 = 0750 — correct by
	// accident — while 027 would produce 0750 and 077 would produce 0700, and
	// relying on the operator's umask for a permission that matters is how
	// directories end up group-readable.
	if err := os.Chmod(path, ConfigDirMode); err != nil {
		return fmt.Errorf("cannot set the mode on %s: %w", path, err)
	}
	if err := i.System.Chown(path, i.account.UID, i.account.GID); err != nil {
		return err
	}
	if !existed {
		i.pushUndo("remove "+path, func(context.Context) error {
			// os.Remove, not RemoveAll. A credential file inside makes this
			// fail, which is the intended outcome: rollback must never
			// destroy a spent one-time credential's replacement.
			err := os.Remove(path)
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				var pathError *os.PathError
				if errors.As(err, &pathError) && errors.Is(pathError.Err, syscall.ENOTEMPTY) {
					return nil
				}
				return err
			}
			return nil
		})
	}
	i.step("configuration directory", fmt.Sprintf(
		"%s (mode %#o, owned by %s)", path, ConfigDirMode, ServiceUser,
	))
	return nil
}

// installCA copies the operator's CA bundle into the configuration directory.
//
// Copied rather than referenced: a path in somebody's home directory is a path
// the service may not be able to read, and one that disappears when they tidy
// up. Mode 0644 root-owned, because a CA certificate is public — the secret is
// the private key, which is not this file.
func (i *Installer) installCA(_ context.Context) error {
	if i.Options.CAFile == "" {
		return nil
	}
	source, err := os.ReadFile(i.Options.CAFile)
	if err != nil {
		return fmt.Errorf("cannot read the CA bundle %q: %w", i.Options.CAFile, err)
	}
	target := i.Paths.CAFile()
	_, statErr := os.Stat(target)
	existed := statErr == nil

	if err := writeFileAtomically(target, source, CAFileMode); err != nil {
		return err
	}
	if !existed {
		i.pushUndo("remove "+target, func(context.Context) error { return removeIfExists(target) })
	}
	i.step("CA certificate", fmt.Sprintf("%s (mode %#o, from %s)", target, CAFileMode, i.Options.CAFile))
	return nil
}

func (i *Installer) placeBinary(_ context.Context) error {
	target := i.Paths.Binary()
	source := i.Options.SourceBinary

	payload, describedSource, err := readExecutable(source)
	if err != nil {
		return err
	}

	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return fmt.Errorf("cannot create %s: %w", filepath.Dir(target), err)
	}

	// An existing binary is moved aside rather than overwritten, so a
	// rollback can put it back and so `upgrade` has something to revert to.
	movedAside := false
	if _, err := os.Lstat(target); err == nil {
		if err := os.Rename(target, i.Paths.PreviousBinary()); err != nil {
			return fmt.Errorf("cannot move the existing binary aside: %w", err)
		}
		movedAside = true
	}

	if err := writeFileAtomically(target, payload, BinaryMode); err != nil {
		if movedAside {
			_ = os.Rename(i.Paths.PreviousBinary(), target)
		}
		return err
	}

	i.pushUndo("restore the previous "+target, func(context.Context) error {
		if movedAside {
			return os.Rename(i.Paths.PreviousBinary(), target)
		}
		return removeIfExists(target)
	})

	detail := fmt.Sprintf("%s (mode %#o, root-owned, from %s)", target, BinaryMode, describedSource)
	if movedAside {
		detail += fmt.Sprintf("; previous kept at %s", i.Paths.PreviousBinary())
	}
	i.step("agent binary", detail)
	return nil
}

// EnvironmentFile renders the contents of /etc/hcc-agent/agent.env.
//
// Exported so --dry-run prints exactly what would be written, and so a test
// can assert on the rendering rather than on a file.
//
// Holds no secret, deliberately. The credential is in its own 0600 file, which
// is what lets an administrator read the controller URL without sudo and still
// not be able to read the credential.
func EnvironmentFile(controllerURL, caPath string) []byte {
	var builder strings.Builder
	builder.WriteString("# Homelab Control Center agent — service environment\n")
	builder.WriteString("#\n")
	builder.WriteString("# Written by `hcc-agent install`. Safe to edit; run\n")
	builder.WriteString("#   systemctl restart " + ServiceName + "\n")
	builder.WriteString("# afterwards. This file holds NO secret: the agent credential is in\n")
	builder.WriteString("# " + config.DefaultCredentialFile + " at mode 0600, which is why this\n")
	builder.WriteString("# one can be 0644 and readable without sudo.\n")
	builder.WriteString("#\n")
	builder.WriteString("# There is no setting that disables TLS verification. An internal CA\n")
	builder.WriteString("# is trusted by pointing " + config.EnvCAFile + " at it.\n")
	builder.WriteString("\n")
	builder.WriteString(config.EnvControllerURL + "=" + controllerURL + "\n")
	if caPath != "" {
		builder.WriteString(config.EnvCAFile + "=" + caPath + "\n")
	}
	builder.WriteString("\n")
	builder.WriteString("# debug | info | warn | error\n")
	builder.WriteString(config.EnvLogLevel + "=info\n")
	builder.WriteString("# text | json — journald captures either.\n")
	builder.WriteString(config.EnvLogFormat + "=text\n")
	return []byte(builder.String())
}

func (i *Installer) environmentFile() []byte {
	caPath := ""
	if i.Options.CAFile != "" {
		caPath = DefaultCAPath
	}
	return EnvironmentFile(i.Options.ControllerURL, caPath)
}

func (i *Installer) writeEnvironmentFile(_ context.Context) error {
	target := i.Paths.EnvFile()
	_, err := os.Stat(target)
	existed := err == nil

	if err := writeFileAtomically(target, i.environmentFile(), EnvFileMode); err != nil {
		return err
	}
	if !existed {
		i.pushUndo("remove "+target, func(context.Context) error { return removeIfExists(target) })
	}
	i.step("environment file", fmt.Sprintf("%s (mode %#o, root-owned)", target, EnvFileMode))
	return nil
}

// enroll reads the one-time credential and exchanges it.
//
// The one-time credential is never written to disk by the installer. It is
// read from the terminal or from a file the operator already controls, held in
// memory for one HTTPS request, and dropped.
func (i *Installer) enroll(ctx context.Context) error {
	i.printf("\n")
	enrollmentCredential, err := i.Options.TokenSource.Read()
	if err != nil {
		return fmt.Errorf("cannot read the enrollment credential: %w", err)
	}

	result, err := i.Options.Enroll(ctx, i.Options.ControllerURL, i.Options.CAFile, enrollmentCredential)
	if err != nil {
		return fmt.Errorf("enrollment failed: %w", err)
	}
	i.enrolled = true

	if err := credentials.Save(i.Paths.Credential(), result.Credential); err != nil {
		return fmt.Errorf(
			"enrollment succeeded but the credential could not be stored: %w\n\n"+
				"The one-time credential has been spent. Fix the problem above, then "+
				"issue a new enrollment credential and run this command again", err,
		)
	}
	// Written by root at 0600 and then handed to the service account. The
	// mode never changes, so there is no window in which it is readable by
	// anybody else — and the agent's own loader accepts a file owned by the
	// uid it runs as.
	if err := i.System.Chown(i.Paths.Credential(), i.account.UID, i.account.GID); err != nil {
		return err
	}

	i.step("enrolled", fmt.Sprintf("as %q (host %s)", result.HostName, result.HostID))
	i.step("credential", fmt.Sprintf(
		"%s (mode %#o, owned by %s)", i.Paths.Credential(), credentials.FileMode, ServiceUser,
	))
	if result.SupersededPrevious {
		i.printf("  ! %-24s %s\n", "previous credential",
			"revoked; any agent still using it has been disconnected")
	}
	return nil
}

func (i *Installer) writeUnit(_ context.Context) error {
	target := i.Paths.Unit()
	_, err := os.Stat(target)
	existed := err == nil

	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return fmt.Errorf("cannot create %s: %w", filepath.Dir(target), err)
	}
	if err := writeFileAtomically(target, UnitFile(), UnitMode); err != nil {
		return err
	}
	if !existed {
		i.pushUndo("remove "+target, func(context.Context) error { return removeIfExists(target) })
	}
	i.step("systemd unit", fmt.Sprintf("%s (mode %#o)", target, UnitMode))
	return nil
}

func (i *Installer) startService(ctx context.Context) error {
	if _, err := i.System.Systemctl(ctx, "daemon-reload"); err != nil {
		return err
	}
	i.pushUndo("stop and disable "+ServiceName, func(ctx context.Context) error {
		_, err := i.System.Systemctl(ctx, "disable", "--now", ServiceName)
		return err
	})
	if _, err := i.System.Systemctl(ctx, "enable", "--now", ServiceName); err != nil {
		return err
	}
	i.step("service", "enabled and started")
	return nil
}

// connectionPollInterval is how often the installer looks for evidence that
// the service connected.
const connectionPollInterval = 500 * time.Millisecond

// waitForConnection makes "installed" mean "connected".
//
// The evidence is local and structural rather than a log line: on every
// accepted connection the controller immediately sends a configuration
// snapshot, and the agent persists it to /etc/hcc-agent/config.json before
// acknowledging. So a loadable configuration file is proof that the socket
// opened, the credential was accepted, and the controller answered — none of
// which can be inferred from `systemctl is-active`.
//
// Deliberately not a second WebSocket of the installer's own: opening one with
// the credential just minted would supersede the service's connection, so the
// check would break the thing it was checking.
func (i *Installer) waitForConnection(ctx context.Context) error {
	deadline := time.Now().Add(i.Options.ConnectTimeout)
	for {
		stored, err := monitoring.Load(i.Paths.Configuration())
		if err == nil {
			enabled := stored.Document.EnabledModules()
			detail := fmt.Sprintf("the controller sent configuration v%d", stored.Document.Version)
			if len(enabled) > 0 {
				detail += " enabling " + strings.Join(enabled, ", ")
			}
			i.step("connected", detail)
			return nil
		}

		// A service that has already given up will never produce the file, so
		// the wait ends early with the journal rather than at the timeout.
		state, _ := i.System.Systemctl(ctx, "is-active", ServiceName)
		switch strings.TrimSpace(state) {
		case "failed", "inactive":
			return i.connectionFailure(ctx, fmt.Sprintf(
				"the service is %s", strings.TrimSpace(state),
			))
		}

		if time.Now().After(deadline) {
			return i.connectionFailure(ctx, fmt.Sprintf(
				"no configuration arrived within %s", i.Options.ConnectTimeout,
			))
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(connectionPollInterval):
		}
	}
}

// connectionFailure explains a service that started and did not connect.
//
// The installation is **not** rolled back here, and the error says so. The
// credential has been spent, the files are correct, and the fault is almost
// always on the network or in the controller's reverse proxy — so removing a
// working installation and making the operator issue a second one-time
// credential would be the wrong response to somebody's firewall.
func (i *Installer) connectionFailure(ctx context.Context, reason string) error {
	var builder strings.Builder
	fmt.Fprintf(&builder, "the agent was installed but has not connected: %s", reason)

	if journal, err := i.System.JournalTail(ctx, ServiceName, 20); err == nil {
		trimmed := strings.TrimSpace(journal)
		if trimmed != "" {
			builder.WriteString("\n\nThe last lines it logged:\n")
			for _, line := range strings.Split(trimmed, "\n") {
				builder.WriteString("  ")
				builder.WriteString(line)
				builder.WriteString("\n")
			}
		}
	}
	builder.WriteString(
		"\nThe installation has been left in place: the credential has been spent, " +
			"and the files are correct.\nDiagnose it with:\n" +
			"  hcc-agent doctor\n" +
			"  journalctl -u " + ServiceName + " -n 50\n",
	)
	return errInstalledNotConnected{message: builder.String()}
}

// errInstalledNotConnected marks the one failure that must not roll back.
type errInstalledNotConnected struct{ message string }

func (e errInstalledNotConnected) Error() string { return e.message }

// InstalledNotConnected reports whether an error means the files are in place
// and the agent has not connected — a state the operator can fix without
// re-enrolling.
func InstalledNotConnected(err error) bool {
	var target errInstalledNotConnected
	return errors.As(err, &target)
}

func (i *Installer) reportSuccess() {
	i.printf("\nInstalled.\n\n")
	i.printf(
		"Nothing is being collected yet, and that is correct: every monitoring module\n" +
			"is off until an administrator enables it for this host. Open the host's page\n" +
			"in the control plane to turn one on.\n\n",
	)
	i.printf("  systemctl status %s\n", ServiceName)
	i.printf("  journalctl -u %s -f\n", ServiceName)
	i.printf("  hcc-agent doctor\n")
}

// rollback unwinds what this run created, most recent first.
//
// Two things are never undone, and both are deliberate:
//
//   - **The credential file.** A one-time enrollment credential has been spent
//     to obtain it; deleting it would mean the operator has to issue another to
//     recover from a problem that has nothing to do with credentials. A
//     surviving credential also puts the next `install` run into
//     [ModeIncomplete], which completes the installation without enrolling.
//   - **Anything a previous run created.** Only this run's own effects are on
//     the undo stack, so a failed install cannot remove a working one's files.
func (i *Installer) rollback(ctx context.Context) {
	if len(i.undo) == 0 {
		return
	}
	i.printf("\nRolling back what this run created\n")
	for index := len(i.undo) - 1; index >= 0; index-- {
		step := i.undo[index]
		if err := step.run(ctx); err != nil {
			i.printf("  ! could not %s: %v\n", step.what, err)
			continue
		}
		i.printf("  ← %s\n", step.what)
	}
	if i.enrolled {
		i.printf(
			"\nThe enrollment credential was spent before this failed, and the\n"+
				"credential it produced has been kept at %s.\n"+
				"Re-running this command will complete the installation without\n"+
				"enrolling again.\n", i.Paths.Credential(),
		)
	}
}

func (i *Installer) pushUndo(what string, run func(context.Context) error) {
	i.undo = append(i.undo, undoStep{what: what, run: run})
}

// describePlan prints what a non-dry run would do, in order, with the exact
// contents of every file it would write.
//
// This is the audit step `curl … | sudo bash` cannot offer, and the reason
// there is no installation shell script. It costs one flag.
func (i *Installer) describePlan(mode Mode) {
	caPath := ""
	if i.Options.CAFile != "" {
		caPath = DefaultCAPath
	}

	if mode == ModeInstalled && !i.Options.Force {
		i.printf(
			"\nThis host already has a complete installation, so a real run would refuse\n" +
				"and tell you to use `hcc-agent upgrade`. The plan below is what\n" +
				"`install --force` would do — which re-enrolls, and disconnects the\n" +
				"agent currently running.\n",
		)
	}

	i.printf("\nWould do, in order\n")
	number := 0
	say := func(format string, arguments ...any) {
		number++
		i.printf("  %d. %s\n", number, fmt.Sprintf(format, arguments...))
	}

	if _, err := i.System.LookupUser(ServiceUser); errors.Is(err, ErrNoSuchUser) {
		say("Create the system account %s (group %s, no home directory, shell %s)",
			ServiceUser, ServiceUser, nologinShell())
	} else {
		say("Use the existing %s system account", ServiceUser)
	}
	say("Create %s   mode %#o   owner %s:%s", i.Paths.ConfigDir(), ConfigDirMode, ServiceUser, ServiceUser)
	if caPath != "" {
		say("Copy %s to %s   mode %#o   owner root:root", i.Options.CAFile, caPath, CAFileMode)
	}
	say("Install the agent binary to %s   mode %#o   owner root:root", i.Paths.Binary(), BinaryMode)
	say("Write %s   mode %#o   owner root:root", i.Paths.EnvFile(), EnvFileMode)
	// A dry run against a complete installation shows what --force would do,
	// because the note above has just said that is what it is showing.
	if mode == ModeFresh || i.Options.Force || mode == ModeInstalled {
		say("Read the enrollment credential from %s and exchange it at %s/api/v1/agents/enroll",
			i.Options.TokenSource.Describe(), i.Options.ControllerURL)
		say("Write %s   mode %#o   owner %s:%s",
			i.Paths.Credential(), credentials.FileMode, ServiceUser, ServiceUser)
	} else {
		say("Keep the existing credential at %s — this host is already enrolled",
			i.Paths.Credential())
	}
	say("Write %s   mode %#o   owner root:root", i.Paths.Unit(), UnitMode)
	say("systemctl daemon-reload, then systemctl enable --now %s", ServiceName)
	say("Wait up to %s for the agent to connect and receive a configuration",
		i.Options.ConnectTimeout)

	i.printf("\n%s would contain\n\n", i.Paths.Unit())
	i.printIndented(string(UnitFile()))

	i.printf("\n%s would contain\n\n", i.Paths.EnvFile())
	i.printIndented(string(i.environmentFile()))

	i.printf("\nNothing above has been done.\n")
}

func (i *Installer) printIndented(text string) {
	for _, line := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
		if line == "" {
			i.printf("\n")
			continue
		}
		i.printf("    %s\n", line)
	}
}

// --- shared helpers ---------------------------------------------------------

// writeFileAtomically writes to a temporary file in the target's own directory
// and renames it over the target.
//
// The same discipline the credential and configuration writers already use,
// and for the same reason: an interrupted write leaves a half-written file,
// and a half-written binary or unit is a service that fails in a way nobody
// will connect back to a power cut.
//
// The temporary file is created with the final mode from the start rather than
// chmod-ed afterwards, because the window between the two is a window in which
// the file has the wrong permissions.
func writeFileAtomically(path string, contents []byte, mode os.FileMode) error {
	directory := filepath.Dir(path)
	temporary := path + ".tmp"
	// O_EXCL and O_NOFOLLOW: a pre-created temporary file or a symlink
	// planted at that path is a refusal rather than a write somewhere else.
	file, err := os.OpenFile(
		temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, mode,
	)
	if err != nil {
		return fmt.Errorf("cannot create %s: %w", temporary, err)
	}
	defer os.Remove(temporary)

	if _, err := file.Write(contents); err != nil {
		file.Close()
		return fmt.Errorf("cannot write %s: %w", temporary, err)
	}
	// An explicit chmod as well as the open mode: the open honours the
	// umask, so a umask of 022 turns a requested 0600 into 0600 but a
	// requested 0755 into 0755 and a requested 0644 into 0644 — correct —
	// while a umask of 077 would turn 0755 into 0700 and leave an installed
	// binary nobody but root can run.
	if err := file.Chmod(mode); err != nil {
		file.Close()
		return fmt.Errorf("cannot set the mode on %s: %w", temporary, err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("cannot flush %s: %w", temporary, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("cannot close %s: %w", temporary, err)
	}
	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("cannot install %s: %w", path, err)
	}
	// The directory entry is flushed too. Without it the rename can be lost
	// to a power cut that the file's own fsync survived, which leaves the
	// temporary file and no target.
	if handle, err := os.Open(directory); err == nil {
		_ = handle.Sync()
		handle.Close()
	}
	return nil
}

func removeIfExists(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// maxBinaryBytes bounds what the installer will read as an executable.
//
// The agent is about 7 MB. The bound is what stops a --source pointed at a
// filesystem image from being the installer's memory profile.
const maxBinaryBytes = 128 << 20

// readExecutable loads the binary to install.
//
// An empty source means this process's own executable, read through
// /proc/self/exe rather than through os.Executable's path: the path can be
// stale — the file may have been renamed or deleted since exec — while
// /proc/self/exe is a handle on the running image and always correct.
func readExecutable(source string) (contents []byte, described string, err error) {
	path := source
	described = source
	if path == "" {
		path = "/proc/self/exe"
		if self, selfErr := os.Executable(); selfErr == nil {
			described = self
		} else {
			described = path
		}
	}

	file, err := os.Open(path) //nolint:gosec // an operator-supplied path, by design
	if err != nil {
		return nil, "", fmt.Errorf("cannot read the agent binary %q: %w", path, err)
	}
	defer file.Close()

	payload, err := io.ReadAll(io.LimitReader(file, maxBinaryBytes+1))
	if err != nil {
		return nil, "", fmt.Errorf("cannot read the agent binary %q: %w", path, err)
	}
	if len(payload) > maxBinaryBytes {
		return nil, "", fmt.Errorf(
			"%q is larger than %d bytes, so it is not an agent binary", path, maxBinaryBytes,
		)
	}
	if len(payload) < 4 || string(payload[:4]) != "\x7fELF" {
		return nil, "", fmt.Errorf("%q is not an ELF executable", path)
	}
	return payload, described, nil
}
