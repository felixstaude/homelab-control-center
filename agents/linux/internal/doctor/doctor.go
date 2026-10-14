// Package doctor diagnoses an agent installation without changing it.
//
// # Why this exists
//
// The deployment documentation used to spend a whole troubleshooting section
// on questions a program can answer: is this host enrolled, is the credential
// file a mode the agent will accept, does the controller's name resolve, does
// its certificate verify, is the service running, is anything being collected.
// Every one of those was a paragraph telling somebody which command to run and
// how to read its output. This is that section, executable.
//
// # It touches nothing
//
// Read-only, by construction. It opens files, resolves a name, opens a socket
// and asks systemd for state. It does not start, stop, write, chmod or chown
// anything, so it is safe to run on a production host at any time and safe to
// run as an unprivileged user — where it reports what it could not check
// rather than guessing.
//
// # It never prints the credential
//
// Not the agent credential and not an enrollment credential. The output is
// meant to be pasted into an issue report, which means "contains no secret"
// has to be a property rather than a habit — so it is a test
// (TestOutputNeverContainsTheCredential) rather than a convention. The checks
// report a credential's *presence, mode and owner*; they never read its value
// into the report.
package doctor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// DefaultTimeout bounds the network checks.
//
// Ten seconds total, not per check: a diagnostic that takes a minute to tell
// an operator their DNS is wrong is a diagnostic they will stop running.
const DefaultTimeout = 10 * time.Second

// MaxClockSkew is how far the local clock may be from the controller's before
// it is reported.
//
// Two minutes. TLS certificate validity is checked against the local clock, so
// a host minutes out is fine and a host days out fails handshakes with errors
// that read as certificate problems — which sends people to their CA instead
// of to NTP.
const MaxClockSkew = 2 * time.Minute

// Status is a finding's outcome.
type Status string

const (
	// StatusOK: checked, and correct.
	StatusOK Status = "ok"
	// StatusWarning: checked, and worth saying, but not broken. A host with
	// no modules enabled collects nothing, which is the *default* state and
	// not a fault.
	StatusWarning Status = "warning"
	// StatusFailed: checked, and wrong. Any of these makes the command exit
	// non-zero.
	StatusFailed Status = "failed"
	// StatusSkipped: could not be checked. Distinguished from failed on
	// purpose — "cannot read the credential file as this user" is not the
	// same finding as "the credential file is world-readable", and reporting
	// the first as the second would send an operator chasing a problem that
	// does not exist.
	StatusSkipped Status = "skipped"
)

// Finding is one diagnostic result.
type Finding struct {
	// Section groups findings in the text output.
	Section string `json:"section"`
	// Check is the short label.
	Check string `json:"check"`
	// Status is the outcome.
	Status Status `json:"status"`
	// Detail is what was found. Never a secret.
	Detail string `json:"detail"`
	// Fix is what to do about a failure, when there is something to do.
	Fix string `json:"fix,omitempty"`
}

// Report is the whole diagnosis.
type Report struct {
	GeneratedAt string    `json:"generated_at"`
	Agent       AgentInfo `json:"agent"`
	Findings    []Finding `json:"findings"`
}

// AgentInfo is the running binary's own identity.
type AgentInfo struct {
	Version   string `json:"version"`
	Commit    string `json:"commit"`
	BuildDate string `json:"build_date"`
	GoVersion string `json:"go_version"`
	Platform  string `json:"platform"`
}

// Options configures a run.
type Options struct {
	// Timeout bounds the network checks. Zero means [DefaultTimeout].
	Timeout time.Duration
	// Paths locates the installation. Tests root it at a temporary directory.
	Paths install.Paths
	// LookupEnv is injected so a test can supply an environment.
	LookupEnv func(string) (string, bool)
	// Now is injected so the clock-skew check is testable.
	Now func() time.Time
	// ProcRoot is where /proc is mounted.
	ProcRoot string
	// ServiceAccount is the account the service is expected to run as.
	// Defaults to [install.ServiceUser]; a test sets it to whoever is running
	// the test, because a test cannot create a file owned by hcc-agent.
	ServiceAccount string
	// Probe is the controller reachability check. Injected for tests; nil
	// means the real one.
	Probe func(ctx context.Context, controllerURL, caFile string) (*controller.ProbeResult, error)
	// Resolve and Dial are the first two stages of the connectivity check.
	// Injected alongside Probe rather than left real, so that the *staging* —
	// which failure is reported, and that the later stages are skipped once
	// an earlier one fails — is what the tests exercise. A test against the
	// real resolver would be a test of whoever owns example.com's DNS.
	Resolve func(ctx context.Context, host string) ([]string, error)
	Dial    func(ctx context.Context, address string) error
	// Systemctl runs systemctl. Injected for tests; nil means the real one.
	Systemctl func(ctx context.Context, args ...string) (string, error)
}

// Failed reports whether anything was found wrong.
func (r Report) Failed() bool {
	for _, finding := range r.Findings {
		if finding.Status == StatusFailed {
			return true
		}
	}
	return false
}

// Counts returns how many findings there are of each status.
func (r Report) Counts() map[Status]int {
	counts := make(map[Status]int, 4)
	for _, finding := range r.Findings {
		counts[finding.Status]++
	}
	return counts
}

type collector struct {
	options  Options
	findings []Finding
	// controllerURL is resolved by the configuration section and used by the
	// connectivity one.
	controllerURL string
	caFile        string
}

func (c *collector) add(section, check string, status Status, detail, fix string) {
	c.findings = append(c.findings, Finding{
		Section: section,
		Check:   check,
		Status:  status,
		Detail:  detail,
		Fix:     fix,
	})
}

func (c *collector) ok(section, check, detail string) {
	c.add(section, check, StatusOK, detail, "")
}

func (c *collector) fail(section, check, detail, fix string) {
	c.add(section, check, StatusFailed, detail, fix)
}

func (c *collector) warn(section, check, detail, fix string) {
	c.add(section, check, StatusWarning, detail, fix)
}

func (c *collector) skip(section, check, detail string) {
	c.add(section, check, StatusSkipped, detail, "")
}

// Run performs every check.
func Run(ctx context.Context, options Options) Report {
	if options.Timeout <= 0 {
		options.Timeout = DefaultTimeout
	}
	if options.LookupEnv == nil {
		options.LookupEnv = os.LookupEnv
	}
	if options.Now == nil {
		options.Now = time.Now
	}
	if options.ProcRoot == "" {
		options.ProcRoot = collect.DefaultProcRoot
	}
	if options.ServiceAccount == "" {
		options.ServiceAccount = install.ServiceUser
	}
	if options.Systemctl == nil {
		options.Systemctl = install.LinuxSystem{}.Systemctl
	}
	if options.Probe == nil {
		options.Probe = realProbe
	}
	if options.Resolve == nil {
		options.Resolve = net.DefaultResolver.LookupHost
	}
	if options.Dial == nil {
		options.Dial = func(ctx context.Context, address string) error {
			dialer := net.Dialer{Timeout: options.Timeout}
			connection, err := dialer.DialContext(ctx, "tcp", address)
			if err != nil {
				return err
			}
			return connection.Close()
		}
	}

	information := buildinfo.Get()
	runner := &collector{options: options}

	runner.checkHost(information)
	runner.checkInstallation(information)
	runner.checkConfiguration()
	runner.checkCredential()
	runner.checkService(ctx)
	runner.checkConnectivity(ctx)
	runner.checkCollection()

	return Report{
		GeneratedAt: options.Now().UTC().Format(time.RFC3339),
		Agent: AgentInfo{
			Version:   information.Version,
			Commit:    information.Commit,
			BuildDate: information.BuildDate,
			GoVersion: information.GoVersion,
			Platform:  information.Platform,
		},
		Findings: runner.findings,
	}
}

const (
	sectionHost         = "Host"
	sectionInstallation = "Installation"
	sectionConfig       = "Configuration"
	sectionCredential   = "Credential"
	sectionService      = "Service"
	sectionController   = "Controller"
	sectionCollection   = "Collection"
)

// --- Host -------------------------------------------------------------------

func (c *collector) checkHost(information buildinfo.Info) {
	c.ok(sectionHost, "agent", fmt.Sprintf(
		"%s (commit %s, built %s)", information.Version, information.Commit, information.BuildDate,
	))
	c.ok(sectionHost, "built for", information.Platform)

	machine := install.LinuxSystem{}.MachineArchitecture()
	if machine == "" {
		c.skip(sectionHost, "machine", "the kernel did not report a machine type")
	} else {
		c.ok(sectionHost, "machine", fmt.Sprintf("%s, kernel %s", machine, kernelRelease()))
	}

	if release := osRelease(); release != "" {
		c.ok(sectionHost, "distribution", release)
	}

	// Who is running this. Read-only checks that need privilege are reported
	// as skipped rather than failed, so the distinction has to be visible.
	euid := os.Geteuid()
	switch euid {
	case 0:
		c.ok(sectionHost, "running as", "root — every check can be performed")
	default:
		c.warn(sectionHost, "running as", fmt.Sprintf(
			"uid %d; checks that need to read root-owned files will be skipped", euid,
		), "re-run with sudo for the complete picture")
	}

	// The four kernel files that are the entire data source for every module
	// that exists. Read, not stat-ed: /proc mounted with subset=pid is
	// present, listable, and missing exactly these.
	var unreadable []string
	for _, name := range []string{"stat", "meminfo", "loadavg", "uptime"} {
		path := filepath.Join(c.options.ProcRoot, name)
		file, err := os.Open(path) //nolint:gosec // a fixed set of names under /proc
		if err != nil {
			unreadable = append(unreadable, name)
			continue
		}
		buffer := make([]byte, 1)
		read, err := file.Read(buffer)
		file.Close()
		if err != nil || read == 0 {
			unreadable = append(unreadable, name)
		}
	}
	if len(unreadable) > 0 {
		c.fail(sectionHost, "/proc", fmt.Sprintf(
			"cannot read %s under %s", strings.Join(unreadable, ", "), c.options.ProcRoot,
		), "the agent has nothing to measure without these; check whether /proc is "+
			"mounted with subset=pid, which hides exactly these files")
	} else {
		c.ok(sectionHost, "/proc", "stat, meminfo, loadavg and uptime all readable")
	}
}

// --- Installation -----------------------------------------------------------

func (c *collector) checkInstallation(information buildinfo.Info) {
	paths := c.options.Paths

	switch info, err := os.Lstat(paths.Binary()); {
	case errors.Is(err, fs.ErrNotExist):
		c.warn(sectionInstallation, "binary", fmt.Sprintf(
			"no agent binary at %s", paths.Binary(),
		), "this agent is running from somewhere else; `hcc-agent install` places it there")
	case err != nil:
		c.skip(sectionInstallation, "binary", fmt.Sprintf("cannot inspect %s: %v", paths.Binary(), err))
	case info.Mode()&fs.ModeSymlink != 0:
		c.warn(sectionInstallation, "binary", fmt.Sprintf(
			"%s is a symbolic link", paths.Binary(),
		), "the installer writes a regular file there; a link is somebody else's arrangement")
	default:
		mode := info.Mode().Perm()
		detail := fmt.Sprintf("%s (mode %#o)", paths.Binary(), mode)
		if mode&0o022 != 0 {
			c.fail(sectionInstallation, "binary", detail+" is writable by other users",
				fmt.Sprintf("chmod 755 %s — a writable service binary is a root shell for "+
					"whoever can write it", paths.Binary()))
		} else {
			// The installed binary's version, which is not necessarily this
			// process's: an operator running the freshly downloaded binary's
			// doctor sees both numbers, and a mismatch is the single most
			// confusing state an upgrade can leave behind.
			installedVersion := versionOf(paths.Binary())
			if installedVersion != "" && installedVersion != information.Version {
				c.warn(sectionInstallation, "binary", fmt.Sprintf(
					"%s reports version %s; this command is version %s",
					paths.Binary(), installedVersion, information.Version,
				), "restart the service if you have just upgraded: systemctl restart "+
					install.ServiceName)
			} else {
				c.ok(sectionInstallation, "binary", detail)
			}
		}
	}

	if _, err := os.Stat(paths.PreviousBinary()); err == nil {
		c.ok(sectionInstallation, "previous binary", fmt.Sprintf(
			"%s is kept for rollback", paths.PreviousBinary(),
		))
	}

	// The account the service runs as. An installation whose account was
	// removed — by a tidy-up script, or by `uninstall --purge` followed by a
	// reinstall of the binary alone — leaves files owned by an orphaned uid
	// and a unit systemd refuses to start, and the message systemd gives for
	// that names the user rather than the cause.
	// The composite literal needs parentheses: `install.LinuxSystem{}.Method()`
	// inside an `if` initialiser is parsed as the start of the if's block.
	_, accountErr := (install.LinuxSystem{}).LookupUser(c.options.ServiceAccount)
	if accountErr == nil {
		c.ok(sectionInstallation, "service account", c.options.ServiceAccount+" exists")
	} else if errors.Is(accountErr, install.ErrNoSuchUser) {
		c.fail(sectionInstallation, "service account",
			"the "+c.options.ServiceAccount+" account does not exist",
			"the unit runs as that account and systemd will refuse to start it; "+
				"`hcc-agent install` creates it")
	} else {
		c.skip(sectionInstallation, "service account",
			fmt.Sprintf("cannot look up %s: %v", c.options.ServiceAccount, accountErr))
	}

	// The configuration directory. The agent writes into it, so the mode and
	// the owner both matter — this is the defect that shipped once already.
	switch info, err := os.Stat(paths.ConfigDir()); {
	case errors.Is(err, fs.ErrNotExist):
		c.fail(sectionInstallation, "configuration directory", fmt.Sprintf(
			"%s does not exist", paths.ConfigDir(),
		), "run `hcc-agent install`, or create it owned by "+c.options.ServiceAccount+
			" at mode 0750")
	case err != nil:
		c.skip(sectionInstallation, "configuration directory", fmt.Sprintf("cannot inspect: %v", err))
	default:
		mode := info.Mode().Perm()
		owner := ownerName(info)
		detail := fmt.Sprintf("%s (mode %#o, owner %s)", paths.ConfigDir(), mode, owner)
		switch {
		case mode&0o022 != 0 && info.Mode()&fs.ModeSticky == 0:
			c.fail(sectionInstallation, "configuration directory",
				detail+" is writable by other users",
				fmt.Sprintf("chmod 750 %s — mode 0600 on the credential inside is no "+
					"defence if another user can replace it", paths.ConfigDir()))
		case owner != c.options.ServiceAccount && owner != "root":
			c.warn(sectionInstallation, "configuration directory",
				detail+"; the service runs as "+c.options.ServiceAccount,
				fmt.Sprintf("chown %s %s", c.options.ServiceAccount, paths.ConfigDir()))
		case owner == "root":
			// The real defect, stated as what it causes rather than as a
			// permission note: the agent connects, is sent a configuration,
			// cannot store it, correctly never acknowledges, and the
			// interface shows the host stuck on "saved, not yet confirmed"
			// for ever while nothing is collected.
			c.fail(sectionInstallation, "configuration directory",
				detail+", so the agent cannot write the configuration it is sent",
				fmt.Sprintf("chown -R %s:%s %s — the agent persists the controller's "+
					"configuration here, and a root-owned directory leaves it "+
					"connected and permanently unconfigured",
					c.options.ServiceAccount, c.options.ServiceAccount, paths.ConfigDir()))
		default:
			c.ok(sectionInstallation, "configuration directory", detail)
		}
	}

	if _, err := os.Stat(paths.Unit()); err == nil {
		c.ok(sectionInstallation, "systemd unit", paths.Unit())
	} else {
		c.warn(sectionInstallation, "systemd unit", fmt.Sprintf(
			"no unit at %s", paths.Unit(),
		), "the agent is not installed as a service; `hcc-agent install` writes one")
	}
}

// --- Configuration ----------------------------------------------------------

func (c *collector) checkConfiguration() {
	paths := c.options.Paths

	// The environment file the unit reads, which is where the controller URL
	// lives on an installed host. Read here rather than trusting this
	// process's own environment: `doctor` is usually run from a shell that
	// has none of the service's settings.
	fromFile := map[string]string{}
	if contents, err := os.ReadFile(paths.EnvFile()); err == nil {
		fromFile = parseEnvironmentFile(string(contents))
		c.ok(sectionConfig, "environment file", fmt.Sprintf(
			"%s (%d settings)", paths.EnvFile(), len(fromFile),
		))
	} else if errors.Is(err, fs.ErrNotExist) {
		c.warn(sectionConfig, "environment file", fmt.Sprintf(
			"no %s", paths.EnvFile(),
		), "an installed service reads its controller URL from there")
	} else {
		c.skip(sectionConfig, "environment file", fmt.Sprintf("cannot read: %v", err))
	}

	// This process's environment wins, because that is what an operator
	// running `HCC_AGENT_CONTROLLER_URL=… hcc-agent doctor` means.
	lookup := func(name string) (string, bool) {
		if value, ok := c.options.LookupEnv(name); ok && strings.TrimSpace(value) != "" {
			return value, true
		}
		value, ok := fromFile[name]
		return value, ok
	}

	settings, err := config.Load(lookup)
	if err != nil {
		c.fail(sectionConfig, "settings", err.Error(),
			"fix "+paths.EnvFile()+" and run: systemctl restart "+install.ServiceName)
		return
	}

	if settings.ControllerURL == "" {
		c.fail(sectionConfig, config.EnvControllerURL, "not set",
			"set it in "+paths.EnvFile())
	} else if parsed, parseErr := url.Parse(settings.ControllerURL); parseErr != nil {
		c.fail(sectionConfig, config.EnvControllerURL, fmt.Sprintf(
			"%q is not a valid URL: %v", settings.ControllerURL, parseErr,
		), "set it to something like https://hcc.example.com")
	} else {
		c.controllerURL = settings.ControllerURL
		switch {
		case parsed.Scheme == "https":
			c.ok(sectionConfig, "controller URL", settings.ControllerURL)
		case parsed.Scheme == "http" && isLoopbackHost(parsed.Hostname()):
			c.warn(sectionConfig, "controller URL",
				settings.ControllerURL+" — plain HTTP to loopback, which is the development case",
				"a remote controller must be https")
		default:
			c.fail(sectionConfig, "controller URL", fmt.Sprintf(
				"%s is plain HTTP to a non-loopback host, which the agent refuses",
				settings.ControllerURL,
			), "use https; the agent's credential crosses this connection and there is "+
				"no option to skip verification")
			// Left unset, so the connectivity section does not try to use a
			// URL the agent itself would refuse.
			c.controllerURL = ""
		}
	}

	if err := settings.Validate(); err != nil && settings.ControllerURL != "" {
		// Validate also complains about a missing controller URL, which is
		// already reported above with a better message.
		c.fail(sectionConfig, "settings", err.Error(), "fix "+paths.EnvFile())
	}

	if settings.CAFile == "" {
		c.ok(sectionConfig, "CA bundle", "none configured; the system roots are used")
	} else {
		c.caFile = settings.CAFile
		switch info, statErr := os.Stat(settings.CAFile); {
		case statErr != nil:
			c.fail(sectionConfig, "CA bundle", fmt.Sprintf(
				"%s is configured and cannot be read: %v", settings.CAFile, statErr,
			), "every connection will fail until this file exists and is readable by "+
				install.ServiceUser)
		case info.Mode().Perm()&0o004 == 0:
			c.warn(sectionConfig, "CA bundle", fmt.Sprintf(
				"%s has mode %#o, so %s may not be able to read it",
				settings.CAFile, info.Mode().Perm(), install.ServiceUser,
			), fmt.Sprintf("chmod 644 %s — a CA certificate is public", settings.CAFile))
		default:
			c.ok(sectionConfig, "CA bundle", fmt.Sprintf(
				"%s (mode %#o), trusted in addition to the system roots",
				settings.CAFile, info.Mode().Perm(),
			))
		}
	}
}

// --- Credential -------------------------------------------------------------

// checkCredential reports the credential's presence, mode and owner.
//
// It never reports its value, and the only thing it does with the bytes is
// discard them. [credentials.Load] applies the agent's own refusals, so what
// is reported here is exactly what the agent itself would accept — a
// diagnostic that used laxer rules than the program it diagnoses would say
// "fine" about a file the service refuses.
func (c *collector) checkCredential() {
	path := c.options.Paths.Credential()

	info, statErr := os.Lstat(path)
	switch {
	case errors.Is(statErr, fs.ErrNotExist):
		c.warn(sectionCredential, "enrolled", "no — there is no credential file",
			"issue an enrollment credential from this host's page, then run "+
				"`hcc-agent install --controller …`")
		return
	case statErr != nil:
		c.skip(sectionCredential, "credential file", fmt.Sprintf("cannot inspect %s: %v", path, statErr))
		return
	}

	c.ok(sectionCredential, "credential file", fmt.Sprintf(
		"%s (mode %#o, owner %s)", path, info.Mode().Perm(), ownerName(info),
	))

	_, loadErr := credentials.Load(path)
	switch {
	case loadErr == nil:
		c.ok(sectionCredential, "enrolled", "yes — the agent will accept this credential file")
	case errors.Is(loadErr, credentials.ErrNotEnrolled):
		c.warn(sectionCredential, "enrolled", "no", "run `hcc-agent install`")
	case errors.Is(loadErr, fs.ErrPermission):
		// Not a failure. Running doctor as an ordinary user cannot read a
		// 0600 root-owned file, and reporting that as a broken credential
		// would be a diagnostic inventing a problem.
		c.skip(sectionCredential, "enrolled", fmt.Sprintf(
			"cannot read it as uid %d; re-run with sudo to check it", os.Geteuid(),
		))
	default:
		var insecure *credentials.InsecureFileError
		if errors.As(loadErr, &insecure) {
			c.fail(sectionCredential, "enrolled",
				"the agent will refuse this credential file: "+insecure.Reason, insecure.Fix)
		} else {
			c.fail(sectionCredential, "enrolled", loadErr.Error(), "")
		}
	}
}

// --- Service ----------------------------------------------------------------

func (c *collector) checkService(ctx context.Context) {
	if _, err := os.Stat(c.options.Paths.Unit()); errors.Is(err, fs.ErrNotExist) {
		c.skip(sectionService, "systemd", "no unit is installed, so there is no service to check")
		return
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		c.skip(sectionService, "systemd", "systemctl is not on PATH")
		return
	}

	// One call for everything, because each systemctl invocation is a process
	// and a diagnostic should not spawn six.
	output, err := c.options.Systemctl(ctx, "show", install.ServiceName,
		"--property=LoadState,ActiveState,SubState,UnitFileState,NRestarts,ExecMainStartTimestamp,Result")
	if err != nil {
		c.skip(sectionService, "systemd", fmt.Sprintf("cannot query the unit: %v", err))
		return
	}
	properties := parseEnvironmentFile(output)

	switch properties["ActiveState"] {
	case "active":
		detail := "active (" + properties["SubState"] + ")"
		if since := properties["ExecMainStartTimestamp"]; since != "" {
			detail += ", since " + since
		}
		c.ok(sectionService, "state", detail)
	case "activating", "reloading":
		c.warn(sectionService, "state", properties["ActiveState"]+" — still coming up", "")
	case "":
		c.skip(sectionService, "state", "systemd reported no state for the unit")
	default:
		c.fail(sectionService, "state", fmt.Sprintf(
			"%s (%s), result %s",
			properties["ActiveState"], properties["SubState"], properties["Result"],
		), "journalctl -u "+install.ServiceName+" -n 50")
	}

	switch properties["UnitFileState"] {
	case "enabled", "enabled-runtime":
		c.ok(sectionService, "at boot", "enabled")
	case "":
		c.skip(sectionService, "at boot", "systemd reported no enablement state")
	default:
		c.warn(sectionService, "at boot", properties["UnitFileState"]+
			" — this host will stop reporting after a reboot",
			"systemctl enable "+install.ServiceName)
	}

	if restarts := properties["NRestarts"]; restarts != "" && restarts != "0" {
		// A warning rather than a failure: a restart is how the agent
		// recovers from a controller outage, and a handful is normal on a
		// machine that has been up for months. The number is the finding.
		c.warn(sectionService, "restarts", restarts+" since systemd last loaded the unit",
			"journalctl -u "+install.ServiceName+" | grep -i error")
	}
}

// --- Connectivity -----------------------------------------------------------

func realProbe(
	ctx context.Context, controllerURL, caFile string,
) (*controller.ProbeResult, error) {
	information := buildinfo.Get()
	client, err := controller.New(controller.Config{
		BaseURL:      controllerURL,
		CAFile:       caFile,
		AgentVersion: information.Version,
		Platform:     information.Platform,
	})
	if err != nil {
		return nil, err
	}
	return client.Probe(ctx)
}

// checkConnectivity separates DNS, TCP and TLS.
//
// Three checks rather than one, because one combined failure is the thing that
// makes enrolment hard to diagnose. "Cannot connect" could be a typo in a
// hostname, a firewall, a proxy that is not listening, or a certificate
// nobody trusts, and the remedies have nothing in common. Resolving the name
// and opening the socket before the HTTPS request costs two syscalls and
// turns one useless message into the right one.
func (c *collector) checkConnectivity(ctx context.Context) {
	if c.controllerURL == "" {
		c.skip(sectionController, "reachability", "no usable controller URL to test")
		return
	}
	parsed, err := url.Parse(c.controllerURL)
	if err != nil {
		c.skip(sectionController, "reachability", "the controller URL did not parse")
		return
	}

	host := parsed.Hostname()
	port := parsed.Port()
	if port == "" {
		port = map[string]string{"https": "443", "http": "80"}[parsed.Scheme]
	}

	ctx, cancel := context.WithTimeout(ctx, c.options.Timeout)
	defer cancel()

	// --- DNS -----------------------------------------------------------
	if net.ParseIP(host) != nil {
		c.ok(sectionController, "name resolution", host+" is a literal address")
	} else {
		addresses, err := c.options.Resolve(ctx, host)
		if err != nil {
			c.fail(sectionController, "name resolution", fmt.Sprintf(
				"%s does not resolve from this host: %v", host, err,
			), "check the spelling, and check that this host's resolver can see that name")
			return
		}
		c.ok(sectionController, "name resolution", fmt.Sprintf(
			"%s → %s", host, strings.Join(addresses, ", "),
		))
	}

	// --- TCP -----------------------------------------------------------
	if err := c.options.Dial(ctx, net.JoinHostPort(host, port)); err != nil {
		c.fail(sectionController, "tcp", fmt.Sprintf(
			"cannot open a connection to %s: %v", net.JoinHostPort(host, port), err,
		), "a firewall between this host and the controller is the usual cause; "+
			"the agent only ever dials out, so no inbound rule is needed here")
		return
	}
	c.ok(sectionController, "tcp", "port "+port+" accepts connections")

	// --- TLS and the controller itself ---------------------------------
	result, err := c.options.Probe(ctx, c.controllerURL, c.caFile)
	if err != nil {
		message := err.Error()
		fix := "journalctl -u " + install.ServiceName + " -n 50"
		switch {
		case strings.Contains(message, "certificate signed by unknown authority"):
			fix = "this host does not trust the CA that issued the controller's " +
				"certificate. Install your CA on this host, or set " +
				config.EnvCAFile + " in " + c.options.Paths.EnvFile() +
				". There is no option to skip the check."
		case strings.Contains(message, "certificate is valid for"):
			fix = "the certificate does not cover the name in " +
				config.EnvControllerURL + "; use the name the certificate was issued for"
		case strings.Contains(message, "certificate has expired") ||
			strings.Contains(message, "not yet valid"):
			fix = "check the controller's certificate, and check this host's clock — " +
				"a clock days out produces exactly this error"
		}
		c.fail(sectionController, "tls", message, fix)
		return
	}

	if result.TLS {
		c.ok(sectionController, "tls", "the certificate chain verifies")
	} else {
		c.warn(sectionController, "tls", "not used — plain HTTP to loopback", "")
	}
	c.ok(sectionController, "health", fmt.Sprintf(
		"the controller answered %d in %s", result.Status, result.RoundTrip.Round(time.Millisecond),
	))

	// --- Clock ---------------------------------------------------------
	if result.ServerTime.IsZero() {
		c.skip(sectionController, "clock", "the controller sent no Date header to compare against")
		return
	}
	skew := c.options.Now().Sub(result.ServerTime)
	if skew < 0 {
		skew = -skew
	}
	// The round trip bounds how precisely this can be measured, so it is
	// subtracted rather than ignored: a 400 ms link should not look like 400
	// ms of skew.
	if skew > result.RoundTrip {
		skew -= result.RoundTrip
	} else {
		skew = 0
	}
	if skew > MaxClockSkew {
		c.fail(sectionController, "clock", fmt.Sprintf(
			"this host's clock is %s from the controller's", skew.Round(time.Second),
		), "TLS validity is checked against the local clock, so a clock this far out "+
			"produces certificate errors that look like CA problems. Install and "+
			"enable NTP: timedatectl set-ntp true")
	} else {
		c.ok(sectionController, "clock", fmt.Sprintf(
			"within %s of the controller's", skew.Round(time.Second)+time.Second,
		))
	}
}

// --- Collection -------------------------------------------------------------

// checkCollection answers "and is it actually collecting anything?".
//
// The second question an operator asks after "is it connected", and the one
// the interface cannot answer from the host's side. The common case —
// enrolled, connected, collecting nothing because no module has been enabled
// — is the *default* and is reported as a warning with the reason, not as a
// fault, because treating it as a fault would train people to ignore it.
func (c *collector) checkCollection() {
	path := c.options.Paths.Configuration()

	stored, err := monitoring.Load(path)
	switch {
	case errors.Is(err, monitoring.ErrNoStoredConfig):
		c.warn(sectionCollection, "configuration", fmt.Sprintf(
			"none stored at %s", path,
		), "the controller sends one on every connection, so this means the agent has "+
			"not connected since it was installed, or cannot write to "+
			filepath.Dir(path))
		return
	case errors.Is(err, fs.ErrPermission):
		c.skip(sectionCollection, "configuration", fmt.Sprintf(
			"cannot read %s as uid %d; re-run with sudo", path, os.Geteuid(),
		))
		return
	case err != nil:
		c.fail(sectionCollection, "configuration", err.Error(),
			"the controller will send a fresh document on the next connection; "+
				"restart the service to ask for one now")
		return
	}

	c.ok(sectionCollection, "configuration", fmt.Sprintf(
		"version %d, checksum %s…", stored.Document.Version, shortChecksum(stored.Checksum),
	))

	enabled := stored.Document.EnabledModules()
	if len(enabled) == 0 {
		c.warn(sectionCollection, "modules", "none enabled, so nothing is being collected",
			"this is the default: every module is off until an administrator enables "+
				"it for this host in the control plane")
		return
	}
	c.ok(sectionCollection, "modules", strings.Join(enabled, ", "))
}

// --- Rendering --------------------------------------------------------------

// WriteText renders the report for a person.
func (r Report) WriteText(out io.Writer) {
	fmt.Fprintf(out, "hcc-agent doctor — %s\n", r.GeneratedAt)
	fmt.Fprintf(out, "%s\n\n", strings.Repeat("─", 72))

	section := ""
	for _, finding := range r.Findings {
		if finding.Section != section {
			if section != "" {
				fmt.Fprintln(out)
			}
			section = finding.Section
			fmt.Fprintf(out, "%s\n", section)
		}
		fmt.Fprintf(out, "  %s %-26s %s\n", marker(finding.Status), finding.Check, finding.Detail)
		if finding.Fix != "" {
			for _, line := range wrap(finding.Fix, 66) {
				fmt.Fprintf(out, "      %s\n", line)
			}
		}
	}

	counts := r.Counts()
	fmt.Fprintf(out, "\n%s\n", strings.Repeat("─", 72))
	fmt.Fprintf(out, "%d ok · %d warning · %d failed · %d not checked\n",
		counts[StatusOK], counts[StatusWarning], counts[StatusFailed], counts[StatusSkipped])
	if r.Failed() {
		fmt.Fprintf(out, "\nSomething is wrong. The lines marked ✗ above say what and how to fix it.\n")
	} else if counts[StatusWarning] > 0 {
		fmt.Fprintf(out, "\nNothing is broken. The lines marked ! are worth reading.\n")
	}
	// Said explicitly, because the whole point of the command is that its
	// output can be handed to somebody else.
	fmt.Fprintf(out,
		"\nThis output contains no credential and is safe to paste into an issue report.\n")
}

// WriteJSON renders the report for a machine.
func (r Report) WriteJSON(out io.Writer) error {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(r); err != nil {
		return fmt.Errorf("cannot encode the report: %w", err)
	}
	return nil
}

func marker(status Status) string {
	switch status {
	case StatusOK:
		return "✓"
	case StatusWarning:
		return "!"
	case StatusFailed:
		return "✗"
	default:
		return "·"
	}
}

func wrap(text string, width int) []string {
	words := strings.Fields(text)
	if len(words) == 0 {
		return nil
	}
	var lines []string
	current := words[0]
	for _, word := range words[1:] {
		if len(current)+1+len(word) > width {
			lines = append(lines, current)
			current = word
			continue
		}
		current += " " + word
	}
	return append(lines, current)
}

// --- Small helpers ----------------------------------------------------------

// parseEnvironmentFile reads KEY=VALUE lines.
//
// Used for both /etc/hcc-agent/agent.env and `systemctl show`, which emit the
// same shape. Comments and blank lines are skipped; a value's surrounding
// quotes are stripped, because systemd accepts them in an EnvironmentFile and
// an operator will write them.
func parseEnvironmentFile(contents string) map[string]string {
	values := make(map[string]string)
	for _, line := range strings.Split(contents, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, found := strings.Cut(line, "=")
		if !found {
			continue
		}
		name = strings.TrimSpace(name)
		value = strings.TrimSpace(value)
		if len(value) >= 2 {
			if (value[0] == '"' && value[len(value)-1] == '"') ||
				(value[0] == '\'' && value[len(value)-1] == '\'') {
				value = value[1 : len(value)-1]
			}
		}
		values[name] = value
	}
	return values
}

// ownerName resolves a file's owning uid to a name, falling back to the number.
func ownerName(info fs.FileInfo) string {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "unknown"
	}
	if name := userNameOf(int(stat.Uid)); name != "" {
		return name
	}
	return fmt.Sprintf("uid %d", stat.Uid)
}

// userNameOf reads /etc/passwd rather than calling os/user.LookupId.
//
// LookupId works, and on a cgo-free build it reads the same file — doing it
// directly keeps one code path instead of two and avoids an error type that
// would have to be unwrapped to tell "absent" from "unreadable".
func userNameOf(uid int) string {
	contents, err := os.ReadFile("/etc/passwd")
	if err != nil {
		return ""
	}
	target := fmt.Sprint(uid)
	for _, line := range strings.Split(string(contents), "\n") {
		fields := strings.Split(line, ":")
		if len(fields) >= 3 && fields[2] == target {
			return fields[0]
		}
	}
	return ""
}

func kernelRelease() string {
	var buffer syscall.Utsname
	if err := syscall.Uname(&buffer); err != nil {
		return "unknown"
	}
	release := make([]byte, 0, len(buffer.Release))
	for _, character := range buffer.Release {
		if character == 0 {
			break
		}
		release = append(release, byte(character))
	}
	return string(release)
}

// osRelease reads PRETTY_NAME out of /etc/os-release.
//
// Included because the first question on a support thread is which
// distribution, and the answer is one file away.
func osRelease() string {
	contents, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return ""
	}
	return parseEnvironmentFile(string(contents))["PRETTY_NAME"]
}

// versionOf asks a binary for its own version.
//
// Runs `<path> version`, which this program's own `version` subcommand
// answers without reading configuration, touching the network or needing
// privilege. Bounded, because a path that is not an agent could be anything.
func versionOf(path string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, path, "version").Output() //nolint:gosec // the installed agent's own path
	if err != nil {
		return ""
	}
	// "hcc-agent 0.1.0 (commit …)" → "0.1.0"
	fields := strings.Fields(string(output))
	if len(fields) >= 2 && fields[0] == "hcc-agent" {
		return fields[1]
	}
	return ""
}

// shortChecksum renders enough of a checksum to compare against the
// interface's, without filling the line.
//
// The stored value carries a "sha256:" prefix, which is the algorithm rather
// than the digest; dropping it leaves room for enough hex to be worth reading.
func shortChecksum(value string) string {
	digest := value
	if _, rest, found := strings.Cut(value, ":"); found {
		digest = rest
	}
	if len(digest) > 16 {
		digest = digest[:16]
	}
	return digest
}

func isLoopbackHost(host string) bool {
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return host == "localhost"
}
