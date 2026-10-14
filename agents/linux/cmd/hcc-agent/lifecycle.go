// Installation lifecycle subcommands: install, uninstall, upgrade, doctor.
//
// Kept in their own file because they are the only commands that change the
// machine, and because they are the only ones that need root. `run`, `status`
// and `version` are deliberately not in here.
//
// # No flag accepts a secret
//
// Every flag below takes a URL, a path or a boolean. --token-file takes a
// *path*, which is not a secret; there is no --token and there will not be
// one, because /proc/<pid>/cmdline is readable by every local user on the
// machine and a flag that exists gets used.

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/doctor"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
)

// interruptible returns a context cancelled by SIGINT or SIGTERM.
//
// Every lifecycle command is interruptible, because each of them can wait on
// the network and an operator who has realised they typed the wrong hostname
// should not have to wait out a timeout.
func interruptible() (context.Context, func()) {
	return signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
}

func runInstall(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent install", flag.ContinueOnError)
	flags.SetOutput(stderr)

	controllerURL := flags.String("controller", "",
		"base URL of the control plane, e.g. https://hcc.example.com (required)")
	tokenFile := flags.String("token-file", "",
		"path to a file containing the one-time enrollment credential")
	caFile := flags.String("ca-file", "",
		"PEM bundle to trust in addition to the system roots, for an internal CA")
	nonInteractive := flags.Bool("non-interactive", false,
		"never prompt; the absence of a terminal becomes an error rather than a hang")
	force := flags.Bool("force", false,
		"re-enroll a host that is already enrolled (disconnects the running agent)")
	dryRun := flags.Bool("dry-run", false,
		"print exactly what would be done, and do none of it")
	source := flags.String("source", "",
		"the agent binary to install (default: this executable)")
	timeout := flags.Duration("connect-timeout", install.DefaultConnectTimeout,
		"how long to wait for the started service to connect")

	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "hcc-agent install: unexpected argument %q\n", flags.Arg(0))
		return exitBadConfig
	}

	// The controller URL may also come from the environment, which is what
	// makes a re-run after a failure one word shorter and what lets a
	// configuration-management run put it in one place.
	resolvedURL := strings.TrimSpace(*controllerURL)
	if resolvedURL == "" {
		if value, ok := os.LookupEnv(config.EnvControllerURL); ok {
			resolvedURL = strings.TrimSpace(value)
		}
	}
	if resolvedURL == "" {
		fmt.Fprintf(stderr,
			"hcc-agent install: --controller is required.\n\n"+
				"  sudo ./hcc-agent install --controller https://hcc.example.com\n\n"+
				"It can also come from %s.\n", config.EnvControllerURL)
		return exitBadConfig
	}

	ctx, stop := interruptible()
	defer stop()

	installer := install.New(install.Options{
		ControllerURL:  resolvedURL,
		CAFile:         strings.TrimSpace(*caFile),
		TokenSource:    install.ResolveTokenSource(*tokenFile, *nonInteractive, os.LookupEnv),
		Force:          *force,
		DryRun:         *dryRun,
		SourceBinary:   strings.TrimSpace(*source),
		ConnectTimeout: *timeout,
		Out:            stdout,
	})

	if err := installer.Run(ctx); err != nil {
		fmt.Fprintf(stderr, "\nhcc-agent: %v\n", err)
		if install.InstalledNotConnected(err) {
			// Distinct from a configuration refusal and from a rolled-back
			// failure: the files are in place and correct, and the remedy is
			// on the network rather than in the command. Exit 1, because
			// something is wrong and a script must see that.
			return exitRuntimeError
		}
		return exitRuntimeError
	}
	return exitOK
}

func runUninstall(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent uninstall", flag.ContinueOnError)
	flags.SetOutput(stderr)

	purge := flags.Bool("purge", false,
		"also remove the configuration directory, the agent credential and the service account")
	dryRun := flags.Bool("dry-run", false,
		"print what would be removed, and remove none of it")

	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "hcc-agent uninstall: unexpected argument %q\n", flags.Arg(0))
		return exitBadConfig
	}

	ctx, stop := interruptible()
	defer stop()

	uninstaller := install.NewUninstaller(install.UninstallOptions{
		Purge:  *purge,
		DryRun: *dryRun,
		Out:    stdout,
	})
	if err := uninstaller.Run(ctx); err != nil {
		fmt.Fprintf(stderr, "\nhcc-agent: %v\n", err)
		return exitRuntimeError
	}
	return exitOK
}

func runUpgrade(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent upgrade", flag.ContinueOnError)
	flags.SetOutput(stderr)

	archive := flags.String("from", "",
		"a downloaded release archive to install (required)")
	expectedDigest := flags.String("sha256", "",
		"the archive's expected SHA-256 digest, as shown by the control plane (required)")
	dryRun := flags.Bool("dry-run", false,
		"verify the archive and print what would happen, changing nothing")
	timeout := flags.Duration("connect-timeout", install.DefaultConnectTimeout,
		"how long to wait for the restarted service to reconnect before rolling back")

	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "hcc-agent upgrade: unexpected argument %q\n", flags.Arg(0))
		return exitBadConfig
	}
	if strings.TrimSpace(*archive) == "" || strings.TrimSpace(*expectedDigest) == "" {
		fmt.Fprint(stderr,
			"hcc-agent upgrade: --from and --sha256 are both required.\n\n"+
				"The control plane's host page shows the download command and the digest\n"+
				"for the version it expects. The digest travels inside the session you are\n"+
				"already authenticated to, which is what makes it worth checking.\n\n"+
				"  curl -fsSLO https://.../hcc-agent_0.2.0_linux_amd64.tar.gz\n"+
				"  sudo hcc-agent upgrade --from hcc-agent_0.2.0_linux_amd64.tar.gz \\\n"+
				"      --sha256 <digest>\n")
		return exitBadConfig
	}

	ctx, stop := interruptible()
	defer stop()

	upgrader := install.NewUpgrader(install.UpgradeOptions{
		ArchivePath:    strings.TrimSpace(*archive),
		ExpectedSHA256: strings.TrimSpace(*expectedDigest),
		DryRun:         *dryRun,
		ConnectTimeout: *timeout,
		Out:            stdout,
	})
	if err := upgrader.Run(ctx); err != nil {
		fmt.Fprintf(stderr, "\nhcc-agent: %v\n", err)
		return exitRuntimeError
	}
	return exitOK
}

func runDoctor(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent doctor", flag.ContinueOnError)
	flags.SetOutput(stderr)

	asJSON := flags.Bool("json", false, "emit the findings as JSON")
	timeout := flags.Duration("timeout", doctor.DefaultTimeout,
		"bound on the network checks")

	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "hcc-agent doctor: unexpected argument %q\n", flags.Arg(0))
		return exitBadConfig
	}

	ctx, stop := interruptible()
	defer stop()
	if *timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *timeout+5*time.Second)
		defer cancel()
	}

	report := doctor.Run(ctx, doctor.Options{Timeout: *timeout})

	if *asJSON {
		if err := report.WriteJSON(stdout); err != nil {
			fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
			return exitRuntimeError
		}
	} else {
		report.WriteText(stdout)
	}

	// Exit non-zero when something is wrong, so a monitoring script or a
	// configuration-management run can branch on it without parsing output.
	if report.Failed() {
		return exitRuntimeError
	}
	return exitOK
}

// runApplyUpdate is the privileged half of a controller-driven update.
//
// Not a command an operator runs. It is the ExecStart of
// hcc-agent-update.service, triggered by hcc-agent-update.path when the
// unprivileged agent leaves a request — see internal/install/handoff.go for
// why that handoff exists and what it does and does not grant.
//
// Takes no arguments on purpose. Every input comes from the request file at a
// fixed path, so there is no flag an attacker who could influence the unit's
// invocation could use to redirect it, and the unit file has nothing in it
// that needs reading carefully.
//
// Exits 0 when there was nothing to do. The path unit can fire for a request
// another invocation already took, and a unit that reported failure for that
// would fill the journal with alarming nothing.
func runApplyUpdate(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hcc-agent apply-update", flag.ContinueOnError)
	flags.SetOutput(stderr)
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "hcc-agent apply-update: unexpected argument %q\n", flags.Arg(0))
		return exitBadConfig
	}

	// The trust store from the build stamp only. Deliberately **not** the
	// operator's HCC_AGENT_RELEASE_KEYS: this runs as root, and the
	// environment file it would read from is one the agent account cannot
	// write but an operator can — which is fine — while the environment of
	// *this* process is whatever systemd was given. Keeping it to the stamp
	// means the privileged step trusts exactly what the binary was built to
	// trust, and nothing a configuration change could add.
	store, err := release.NewTrustStore(buildinfo.ReleaseKeys())
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent apply-update: %v\n", err)
		return exitBadConfig
	}
	if store.Len() == 0 {
		fmt.Fprint(stderr,
			"hcc-agent apply-update: this build trusts no publisher key, so it cannot\n"+
				"verify a release. Nothing was installed. A build that can perform\n"+
				"automatic updates is one stamped with the publisher's key at build time.\n")
		return exitRuntimeError
	}

	ctx, stop := interruptible()
	defer stop()

	err = install.ApplyUpdateRequest(ctx, install.ApplyOptions{
		TrustStore: store,
		Out:        stdout,
	})
	switch {
	case err == nil:
		return exitOK
	case errors.Is(err, install.ErrNoUpdateRequest):
		fmt.Fprintln(stdout, "No update request; nothing to do.")
		return exitOK
	default:
		fmt.Fprintf(stderr, "\nhcc-agent apply-update: %v\n", err)
		return exitRuntimeError
	}
}
