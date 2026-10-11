// Command hcc-agent is the Homelab Control Center Linux agent.
//
// It runs as a long-lived process on a monitored host, dials out to the
// control plane, and reports what the operator has configured it to report.
// It never listens on a network port and never exposes the Docker socket.
//
// Usage:
//
//	hcc-agent [-version] [-log-level level] [-log-format text|json]
//
// Configuration is read from HCC_AGENT_* environment variables; see
// agents/linux/README.md. The enrolment token is intentionally not accepted
// as a flag, because command lines are world-readable via /proc.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/agent"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/logging"
)

// Exit codes. Distinct codes let a service manager and an operator tell a
// misconfiguration apart from a runtime failure without reading logs.
const (
	exitOK           = 0
	exitRuntimeError = 1
	exitBadConfig    = 2
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is main's testable core.
//
// Taking args and writers as parameters, and returning an exit code instead of
// calling os.Exit, means the whole startup path can be exercised by a test.
func run(args []string, stdout, stderr *os.File) int {
	flags := flag.NewFlagSet("hcc-agent", flag.ContinueOnError)
	flags.SetOutput(stderr)

	var (
		showVersion = flags.Bool("version", false, "print version information and exit")
		logLevel    = flags.String("log-level", "", "override "+config.EnvLogLevel+" (debug|info|warn|error)")
		logFormat   = flags.String("log-format", "", "override "+config.EnvLogFormat+" (text|json)")
	)

	flags.Usage = func() {
		fmt.Fprintf(stderr, "hcc-agent — Homelab Control Center Linux agent\n\n")
		fmt.Fprintf(stderr, "Usage:\n  hcc-agent [flags]\n\nFlags:\n")
		flags.PrintDefaults()
		fmt.Fprintf(stderr, "\nConfiguration is read from the environment:\n")
		fmt.Fprintf(stderr, "  %s   base URL of the control plane (required)\n", config.EnvControllerURL)
		fmt.Fprintf(stderr, "  %s         path to a file containing the enrolment token (preferred)\n", config.EnvTokenFile)
		fmt.Fprintf(stderr, "  %s             enrolment token (readable via /proc; prefer the file form)\n", config.EnvToken)
		fmt.Fprintf(stderr, "  %s          identity reported to the controller (default: OS hostname)\n", config.EnvHostname)
		fmt.Fprintf(stderr, "  %s   how often to report (default 30s)\n", config.EnvReportInterval)
		fmt.Fprintf(stderr, "\nSee agents/linux/README.md for the full reference.\n")
	}

	if err := flags.Parse(args); err != nil {
		// flag already reported the problem to stderr. ErrHelp is a
		// successful -h, not a failure.
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitBadConfig
	}

	if *showVersion {
		fmt.Fprintln(stdout, buildinfo.Get().String())
		return exitOK
	}

	cfg, err := config.Load(os.LookupEnv)
	if err != nil {
		fmt.Fprintf(stderr, "hcc-agent: %v\n", err)
		return exitBadConfig
	}

	// Flags override the environment, so an operator can raise verbosity for
	// one invocation without editing a unit file.
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
		return exitBadConfig
	}

	logger := logging.New(stderr, cfg.LogLevel, logging.Format(cfg.LogFormat))

	// SIGTERM is what systemd and container runtimes send; SIGINT is Ctrl-C.
	// NotifyContext cancels ctx on either, which the agent's Run loop treats
	// as its shutdown trigger. A second signal aborts immediately, because an
	// operator pressing Ctrl-C twice wants out now.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	instance, err := agent.New(cfg, logger)
	if err != nil {
		logger.Error("failed to construct agent", "error", err)
		return exitRuntimeError
	}

	if err := instance.Run(ctx); err != nil {
		logger.Error("agent terminated with an error", "error", err)
		return exitRuntimeError
	}

	return exitOK
}
