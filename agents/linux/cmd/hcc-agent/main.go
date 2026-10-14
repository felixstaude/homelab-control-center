// Command hcc-agent is the Homelab Control Center Linux agent.
//
// It runs as a long-lived process on a monitored host, dials out to the
// control plane, and reports what the operator has configured it to report.
// It never listens on a network port and never exposes the Docker socket.
//
// Usage:
//
//	hcc-agent install           install, enroll and start the service
//	hcc-agent upgrade           replace the binary with a verified download
//	hcc-agent uninstall         remove the service, the unit and the binary
//	hcc-agent doctor            diagnose this installation, changing nothing
//	hcc-agent enroll            claim this host with a one-time credential
//	hcc-agent run               connect and stay connected (the service command)
//	hcc-agent status            report whether this host is enrolled
//	hcc-agent version           print version information
//
// Configuration is read from HCC_AGENT_* environment variables; see
// agents/linux/README.md. No secret is accepted as a flag, because a command
// line is readable by every local user through /proc.
//
// # The binary is its own installer
//
// `install` exists so that the artifact somebody verifies with `sha256sum -c`
// is the artifact that performs the installation and then *is* the installed
// program. There is no installation shell script and no `curl … | sudo bash`,
// because that pattern fails three ways at once: unaudited code runs as root,
// the credential arrives in argv where /proc makes it world-readable, and the
// whole command lands in shell history. `install --dry-run` prints the exact
// unit, paths and modes and changes nothing, which is the audit step a pipe
// into a shell cannot offer.
//
// # Why subcommands rather than one mode
//
// Enrollment and running are different operations with different privileges
// and different lifetimes. Enrollment happens once, interactively, needs write
// access to /etc/hcc-agent, and spends a credential that cannot be spent
// twice. Running happens forever, unattended, and needs only to read the
// credential file. One binary that did both on startup would mean the service
// unit held the privilege to write its own credential for the whole time it
// was running — and a restart loop would try to re-enroll with a credential
// that was spent on the first attempt.
//
// `status` exists because the question "is this host enrolled?" is the first
// one an operator asks on a machine that is not appearing in the interface,
// and answering it should not require reading a file by hand or starting the
// service to see what happens.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/logging"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/updater"
)

// Exit codes. Distinct codes let a service manager and an operator tell a
// misconfiguration apart from a runtime failure without reading logs — and
// let systemd's Restart=on-failure do the right thing.
const (
	exitOK           = 0
	exitRuntimeError = 1
	exitBadConfig    = 2
	// exitNotEnrolled is its own code because it is the one failure with an
	// obvious remedy an operator can act on, and because a restart cannot fix
	// it. `systemctl status` showing this is a clear instruction.
	exitNotEnrolled = 3
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is main's testable core.
//
// Taking args and writers as parameters, and returning an exit code instead of
// calling os.Exit, means the whole startup path can be exercised by a test.
func run(args []string, stdout, stderr io.Writer) int {
	// -version is kept alongside the `version` subcommand. The flag form is
	// what packaging tools and humans habitually try, and answering it is
	// cheaper than explaining that it moved.
	if len(args) == 1 && (args[0] == "-version" || args[0] == "--version") {
		args = []string{"version"}
	}
	if len(args) == 0 {
		usage(stderr)
		return exitBadConfig
	}

	command, rest := args[0], args[1:]
	switch command {
	case "version":
		fmt.Fprintln(stdout, buildinfo.Get().String())
		return exitOK
	case "install":
		return runInstall(rest, stdout, stderr)
	case "uninstall":
		return runUninstall(rest, stdout, stderr)
	case "upgrade":
		return runUpgrade(rest, stdout, stderr)
	case "doctor":
		return runDoctor(rest, stdout, stderr)
	case "enroll":
		return runEnroll(rest, stdout, stderr)
	case "run":
		return runAgent(rest, stdout, stderr)
	case "status":
		return runStatus(rest, stdout, stderr)
	case "-h", "--help", "help":
		usage(stdout)
		return exitOK
	default:
		fmt.Fprintf(stderr, "hcc-agent: unknown command %q\n\n", command)
		usage(stderr)
		return exitBadConfig
	}
}

func usage(out io.Writer) {
	fmt.Fprint(out, `hcc-agent — Homelab Control Center Linux agent

Usage:
  hcc-agent <command> [flags]

Installing (needs root):
  hcc-agent install --controller URL   install, enroll and start the service
  hcc-agent install --dry-run ...      print exactly what that would do
  hcc-agent upgrade                    replace the binary with a verified download
  hcc-agent uninstall                  remove the service, unit and binary
  hcc-agent uninstall --purge          also the credential and the account

Operating:
  hcc-agent doctor     diagnose this installation; changes nothing
  hcc-agent doctor --json  the same, machine-readable
  hcc-agent status     report whether this host is enrolled
  hcc-agent version    print version information

Lower level:
  hcc-agent enroll     claim this host using a one-time enrollment credential
  hcc-agent run        connect to the controller and stay connected

Install flags:
  --controller URL     base URL of the control plane (required, https)
  --token-file PATH    file holding the one-time enrollment credential
  --non-interactive    never prompt; the absence of a terminal is an error
  --ca-file PATH       a CA bundle to trust in addition to the system roots
  --force              re-enroll a host that is already enrolled
  --dry-run            print the plan and change nothing

Configuration (environment):
  `+config.EnvControllerURL+`         base URL of the control plane (required, https)
  `+config.EnvCredentialFile+`        path to the agent credential
                                       (default `+config.DefaultCredentialFile+`)
  `+config.EnvConfigFile+`            where to store the controller's monitoring
                                       configuration
                                       (default `+config.DefaultConfigFile+`)
  `+config.EnvEnrollmentFile+`  file holding the one-time credential (enroll only)
  `+config.EnvEnrollmentToken+`       the one-time credential itself (enroll only)
  `+config.EnvCAFile+`                extra CA bundle to trust, for an internal CA
  `+config.EnvLogLevel+`              debug | info | warn | error
  `+config.EnvLogFormat+`             text | json

Automatic updates (off unless this host says otherwise):
  `+config.EnvAutoUpdate+`            act on an update the controller authorizes
                                       (default false)
  `+config.EnvUpdateChannel+`         stable | beta | alpha (default stable)
  `+config.EnvPinnedVersion+`         hold this host at one exact version
  `+config.EnvReleaseKeys+`           extra publisher keys to trust, base64,
                                       comma-separated; added to the one this
                                       build was stamped with

No secret is accepted as a flag: a command line is readable by every local
user through /proc. There is no option to skip TLS verification.

An update is installed only when this host has enabled it, the controller's
policy for this host permits the step, and the release metadata carries a
valid publisher signature. The controller cannot change the first or the last.

See agents/linux/README.md for the full reference.
`)
}

// loadConfig is the shared configuration path for every subcommand that needs
// one. Flags override the environment, so an operator can raise verbosity for
// one invocation without editing a unit file.
func loadConfig(flags *flag.FlagSet, args []string, stderr io.Writer) (config.Config, int, bool) {
	logLevel := flags.String("log-level", "", "override "+config.EnvLogLevel)
	logFormat := flags.String("log-format", "", "override "+config.EnvLogFormat)

	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return config.Config{}, exitOK, false
		}
		return config.Config{}, exitBadConfig, false
	}

	cfg, err := config.Load(os.LookupEnv)
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return config.Config{}, exitBadConfig, false
	}
	if *logLevel != "" {
		cfg.LogLevel = *logLevel
	}
	if *logFormat != "" {
		cfg.LogFormat = config.LogFormat(*logFormat)
	}

	if err := cfg.Validate(); err != nil {
		// Printed rather than logged: the logger's own configuration may be
		// what failed validation.
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return config.Config{}, exitBadConfig, false
	}
	return cfg, exitOK, true
}

// runEnroll spends a one-time credential and installs the durable one.
func runEnroll(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent enroll", flag.ContinueOnError)
	flags.SetOutput(stderr)
	force := flags.Bool("force", false,
		"replace an existing credential (the previous one stops working)")

	cfg, code, ok := loadConfig(flags, args, stderr)
	if !ok {
		return code
	}

	// Checked before the credential is spent. Enrolling a host that is already
	// enrolled revokes the credential it is using, so doing it by accident
	// takes a working agent offline — and the one-time credential would be
	// gone, so the mistake would not even be undoable without issuing another.
	if !*force {
		if _, err := credentials.Load(cfg.CredentialFile); err == nil {
			fmt.Fprintf(stderr,
				"hcc-agent: this host is already enrolled (%s).\n"+
					"Re-enrolling revokes the credential the running agent is using.\n"+
					"Pass -force if that is what you intend.\n",
				cfg.CredentialFile)
			return exitBadConfig
		}
	}

	enrollmentCredential, err := config.EnrollmentCredential(os.LookupEnv)
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return exitBadConfig
	}

	info := buildinfo.Get()
	client, err := controller.New(controller.Config{
		BaseURL:      cfg.ControllerURL,
		CAFile:       cfg.CAFile,
		AgentVersion: info.Version,
		Platform:     info.Platform,
	})
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return exitBadConfig
	}

	// Cancellable, so an enrollment against an unreachable controller can be
	// interrupted rather than waited out.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	result, err := client.Enroll(ctx, enrollmentCredential)
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent: enrollment failed: %v\n", err)
		if errors.Is(err, controller.ErrUnauthorized) {
			fmt.Fprintf(stderr,
				"\nAn enrollment credential is single-use and short-lived. "+
					"Issue a new one from the host's page and try again.\n")
		}
		return exitRuntimeError
	}

	if err := credentials.Save(cfg.CredentialFile, result.Credential); err != nil {
		// The credential has been spent and the controller has already
		// recorded this host as enrolled, so this is the worst place to fail.
		// Said plainly, with the remedy, rather than reported as a generic
		// write error: the operator has to know that retrying needs a *new*
		// enrollment credential.
		fmt.Fprintf(stderr,
			"hcc-agent: enrollment succeeded but the credential could not be stored: %v\n\n"+
				"The one-time credential has been spent. Fix the problem above, then "+
				"issue a new enrollment credential and run this command again.\n",
			err)
		return exitRuntimeError
	}

	fmt.Fprintf(stdout, "Enrolled as %q (host %s).\n", result.HostName, result.HostID)
	fmt.Fprintf(stdout, "Credential stored in %s (mode 0600).\n", cfg.CredentialFile)
	if result.SupersededPrevious {
		fmt.Fprintf(stdout,
			"\nThis host had an existing agent credential, which has now been revoked.\n"+
				"Any agent still using it will be disconnected.\n")
	}
	fmt.Fprintf(stdout, "\nStart the agent with:\n  systemctl enable --now hcc-agent\n")
	return exitOK
}

// runAgent is the service command.
func runAgent(args []string, _, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent run", flag.ContinueOnError)
	flags.SetOutput(stderr)

	cfg, code, ok := loadConfig(flags, args, stderr)
	if !ok {
		return code
	}

	logger := logging.New(stderr, cfg.LogLevel, logging.Format(cfg.LogFormat))

	// SIGTERM is what systemd and container runtimes send; SIGINT is Ctrl-C.
	// NotifyContext cancels ctx on either, which the agent's Run loop treats
	// as its shutdown trigger. A second signal aborts immediately, because an
	// operator pressing Ctrl-C twice wants out now.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	options, err := updateOptions(cfg, logger)
	if err != nil {
		// A trust store that cannot be built is a configuration error, not a
		// reason to run without updates: an operator who set a key meant it,
		// and starting anyway would leave a host that reports itself healthy
		// and quietly declines every update.
		logger.Error("the update trust store could not be built", "error", err)
		return exitBadConfig
	}

	instance, err := agent.New(cfg, logger, options...)
	if err != nil {
		logger.Error("failed to construct agent", "error", err)
		return exitBadConfig
	}

	err = instance.Run(ctx)
	switch {
	case err == nil:
		return exitOK
	case errors.Is(err, credentials.ErrNotEnrolled):
		return exitNotEnrolled
	case errors.Is(err, agent.ErrRevoked):
		// Non-zero, deliberately. A revoked agent that exited 0 would leave
		// systemd reporting a clean stop, which is the opposite of what
		// happened; Restart=on-failure then does nothing and `systemctl
		// status` says the service is inactive rather than broken.
		return exitRuntimeError
	default:
		logger.Error("agent terminated with an error", "error", err)
		return exitRuntimeError
	}
}

// updateOptions builds the controller-driven update engine, or nothing.
//
// # Why this is in main rather than in internal/agent
//
// The updater depends on internal/install, which is the privileged half of
// this binary: it replaces the executable, writes a unit file and restarts a
// service. internal/agent — the session loop, the thing a socket talks to —
// deliberately has no path to it. The two are joined here, once, at the top,
// where the joining is visible.
//
// # What makes an update possible at all
//
// Four things, each independently sufficient to refuse:
//
//  1. The operator has enabled it on this host (HCC_AGENT_AUTO_UPDATE).
//  2. This build trusts at least one publisher key.
//  3. The controller sends an instruction — which it only does when *its*
//     policy for this host says so, and when the release is compatible.
//  4. The instruction's metadata carries a valid signature by a trusted key,
//     and the archive's bytes hash to what that signed metadata says.
//
// The first two are the host's own and the controller cannot affect them. That
// asymmetry is the design: the controller chooses *whether* and *when*, the
// publisher's signature establishes *what*, and the host keeps a veto over
// both.
//
// When updates are off, no applier is installed and the agent tells the
// controller nothing is coming. It does not pretend: a host with automatic
// updates disabled reports the instruction as declined with that reason, so
// "why has this host not updated" has an answer in the history.
func updateOptions(cfg config.Config, logger *slog.Logger) ([]agent.Option, error) {
	applier, err := updateApplier(cfg, logger)
	if err != nil {
		return nil, err
	}
	if applier == nil {
		return nil, nil
	}
	return []agent.Option{agent.WithUpdateApplier(applier)}, nil
}

// updateApplier builds the engine, or returns nil when this host has not
// enabled updates.
//
// Separate from [updateOptions] so that both the decision and the resulting
// engine are testable: an agent.Option is a closure over private state, and a
// test that could only observe "one option was returned" would not be able to
// assert the thing that matters — that an unsigned instruction is declined.
func updateApplier(cfg config.Config, logger *slog.Logger) (agent.UpdateApplier, error) {
	if !cfg.AutoUpdate {
		logger.Info("controller-driven updates are disabled on this host",
			"enable_with", config.EnvAutoUpdate+"=true",
		)
		return nil, nil
	}

	// The publisher's key from the build, plus whatever the operator added.
	// Union rather than override: an operator building their own agents signs
	// with their own key and should not have to give up the publisher's.
	keys := append([]string{}, buildinfo.ReleaseKeys()...)
	keys = append(keys, cfg.ReleaseKeys...)
	store, err := release.NewTrustStore(keys)
	if err != nil {
		return nil, err
	}
	if store.Len() == 0 {
		// Not an error: it is the state of every build of this repository that
		// is not an official release, and refusing to start would make a
		// development agent unusable. The agent runs, declines instructions,
		// and says why — which is exactly what it should do, because such a
		// binary cannot tell a real release from a forged one.
		logger.Warn("automatic updates are enabled but this build trusts no publisher key",
			"consequence", "every update instruction will be declined",
			"configure_with", config.EnvReleaseKeys,
		)
	}

	paths := install.Paths{}
	engine := updater.New(updater.Options{
		TrustStore: store,
		Policy: updater.Policy{
			AutoUpdate:    cfg.AutoUpdate,
			Channel:       cfg.UpdateChannel,
			PinnedVersion: cfg.PinnedVersion,
		},
		Platform:         buildinfo.Get().Platform,
		InstalledVersion: buildinfo.Get().Version,
		Paths:            paths,
		StagingDir:       updater.StagingDirFor(paths),
		// The same CA the control connection trusts. An operator running an
		// internal CA mirrors the archives behind it too, and a downloader
		// that only trusted public roots would make internal mirrors
		// impossible.
		CAFile: cfg.CAFile,
		Logger: logger,
	})
	logger.Info("controller-driven updates are enabled",
		"channel", channelOrStable(cfg.UpdateChannel),
		"pinned", cfg.PinnedVersion,
		"trusted_keys", store.Len(),
	)
	return updateAdapter{engine}, nil
}

func channelOrStable(channel string) string {
	if channel == "" {
		return "stable"
	}
	return channel
}

// updateAdapter satisfies agent.UpdateApplier using the real updater.
//
// The two interfaces have identical method sets but are distinct named types,
// which is the point: internal/agent declares what it needs without importing
// the package that provides it. Go will accept an agent.UpdateReporter where
// an updater.Reporter is wanted, so the adapter is one line.
type updateAdapter struct {
	engine *updater.Updater
}

func (a updateAdapter) Apply(
	ctx context.Context, instruction protocol.UpdateInstruction, reporter agent.UpdateReporter,
) error {
	return a.engine.Apply(ctx, instruction, reporter)
}

// runStatus answers "is this host enrolled?" without starting anything.
//
// Reports the credential's *presence and safety*, never its value. There is
// deliberately no flag that prints it: the credential is in a file the
// operator can read if they are entitled to, and a command that printed it
// would put it in shell history and in whatever they piped it to.
// reportConfiguration prints what this agent is set up to collect.
//
// Part of `status` because it answers the second question an operator asks
// after "is this host enrolled": "and is it actually collecting anything?".
// Without it the answer requires reading a JSON file by hand, and the common
// case — enrolled, connected, collecting nothing because no module has been
// enabled — would look identical to a fault.
//
// Never fatal, and the three outcomes are deliberately distinguishable:
// "none" means the controller has not sent one yet, "unusable" means the file
// is there and broken, and a version means it is in force. Reporting "none"
// for a corrupt file would hide exactly the problem this is meant to surface.
func reportConfiguration(path string, stdout io.Writer) {
	stored, err := monitoring.Load(path)
	switch {
	case errors.Is(err, monitoring.ErrNoStoredConfig):
		fmt.Fprintf(stdout, "Configuration:   none yet; the controller sends one on connect\n")
	case err != nil:
		fmt.Fprintf(stdout, "Configuration:   UNUSABLE (%v)\n", err)
	default:
		enabled := stored.Document.EnabledModules()
		fmt.Fprintf(stdout, "Configuration:   v%d\n", stored.Document.Version)
		if len(enabled) == 0 {
			// Said in words rather than printed as an empty list, because
			// "collecting nothing" is the default state and an operator
			// looking at a blank line cannot tell it from a rendering fault.
			fmt.Fprintf(stdout, "Modules:         none enabled\n")
		} else {
			fmt.Fprintf(stdout, "Modules:         %s\n", strings.Join(enabled, ", "))
		}
	}
}

func runStatus(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent status", flag.ContinueOnError)
	flags.SetOutput(stderr)

	cfg, code, ok := loadConfig(flags, args, stderr)
	if !ok {
		return code
	}

	fmt.Fprintf(stdout, "Controller:      %s\n", cfg.ControllerURL)
	fmt.Fprintf(stdout, "Credential file: %s\n", cfg.CredentialFile)
	fmt.Fprintf(stdout, "Config file:     %s\n", cfg.ConfigFile)

	// The controller URL is checked here, not only by `enroll` and `run`.
	//
	// Found by the security suite: without this, `status` reported
	// "Enrolled: yes" for a host whose plaintext controller URL the agent
	// would refuse on every connection attempt — so the one command an
	// operator runs to diagnose "why is this host not appearing" told them
	// everything was fine. A diagnostic that cannot see the most common
	// misconfiguration is not a diagnostic.
	info := buildinfo.Get()
	if _, err := controller.New(controller.Config{
		BaseURL:      cfg.ControllerURL,
		CAFile:       cfg.CAFile,
		AgentVersion: info.Version,
		Platform:     info.Platform,
	}); err != nil {
		fmt.Fprintf(stdout, "Controller URL:  UNUSABLE\n\n")
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return exitBadConfig
	}

	_, err := credentials.Load(cfg.CredentialFile)
	switch {
	case err == nil:
		fmt.Fprintf(stdout, "Enrolled:        yes\n")
		fmt.Fprintf(stdout, "Protocol:        v%d\n", protocol.Version)
		reportConfiguration(cfg.ConfigFile, stdout)
		return exitOK
	case errors.Is(err, credentials.ErrNotEnrolled):
		fmt.Fprintf(stdout, "Enrolled:        no\n\n")
		fmt.Fprintf(stdout,
			"Issue an enrollment credential from this host's page in the control "+
				"plane, then run:\n  %s=... hcc-agent enroll\n",
			config.EnvEnrollmentToken)
		return exitNotEnrolled
	default:
		fmt.Fprintf(stdout, "Enrolled:        no\n\n")
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return exitRuntimeError
	}
}
