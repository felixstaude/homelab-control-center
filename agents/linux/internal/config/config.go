// Package config loads and validates agent configuration.
//
// Configuration comes from the environment, with a few command-line flags able
// to override individual values. Validation happens once at startup: an agent
// with a bad configuration exits immediately with a readable message rather
// than running in a degraded state that is hard to diagnose remotely.
//
// # No secret is ever accepted as a flag
//
// Not the enrollment credential and not the agent credential. A command line
// is readable by every local user through /proc/<pid>/cmdline, so a flag is
// the one place a secret must never go — and because a flag that exists gets
// used, there is no such flag rather than a documented recommendation against
// one.
//
// The agent credential lives in a file and only in a file ([CredentialFile]).
// The enrollment credential may come from the environment, because it is
// short-lived and single-use and the operator is typing it interactively; the
// file form is still offered and preferred.
//
// # What this package no longer has, and why
//
// Three settings were removed in milestone 1.8 rather than carried forward:
//
//   - HCC_AGENT_TLS_SKIP_VERIFY. An agent whose credential crosses an
//     unverified TLS session is an agent whose credential can be collected by
//     anyone on the path. The warning the old code logged at every start was
//     not a mitigation — it was a note nobody reads attached to a flag that
//     ends up in a real unit file. An operator with an internal CA installs it
//     on the host, or points [CAFile] at it.
//   - HCC_AGENT_HOSTNAME. The agent used to report a hostname and the
//     controller used to believe it. The host's identity now comes from the
//     credential that authenticated the connection, so a self-reported name is
//     at best redundant and at worst a field for claiming to be somebody else.
//   - HCC_AGENT_REPORT_INTERVAL. The heartbeat cadence is the controller's to
//     state, and it states it in every acknowledgement. A host that could
//     configure its own liveness signal could hide its own outage
//     (docs/AGENT_ARCHITECTURE.md §9).
package config

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"time"
)

// Environment variable names. All agent configuration is prefixed HCC_AGENT_.
const (
	EnvControllerURL    = "HCC_AGENT_CONTROLLER_URL"
	EnvCredentialFile   = "HCC_AGENT_CREDENTIAL_FILE"
	EnvConfigFile       = "HCC_AGENT_CONFIG_FILE"
	EnvEnrollmentToken  = "HCC_AGENT_ENROLLMENT_TOKEN" //nolint:gosec // a variable name, not a credential
	EnvEnrollmentFile   = "HCC_AGENT_ENROLLMENT_TOKEN_FILE"
	EnvCAFile           = "HCC_AGENT_CA_FILE"
	EnvLogLevel         = "HCC_AGENT_LOG_LEVEL"
	EnvLogFormat        = "HCC_AGENT_LOG_FORMAT"
	EnvShutdownTimeout  = "HCC_AGENT_SHUTDOWN_TIMEOUT"
	EnvReconnectMinWait = "HCC_AGENT_RECONNECT_MIN_WAIT"
	EnvReconnectMaxWait = "HCC_AGENT_RECONNECT_MAX_WAIT"
)

// DefaultCredentialFile is where enrollment installs the agent credential.
//
// Under /etc rather than /var: it is configuration an operator may need to
// replace by hand, it must survive a package upgrade, and /etc is where a
// Debian administrator looks for a service's secrets.
const DefaultCredentialFile = "/etc/hcc-agent/credential"

// DefaultConfigFile is where the agent stores the configuration the controller
// gave it.
//
// Beside the credential, in the same 0700 directory. Not under /var/lib,
// which is where mutable state conventionally lives, for one reason that
// outweighs the convention: the agent must be able to start and collect with
// its last known good configuration before anything else is mounted or
// available, and /etc is the directory that is always there. The file is
// controller-owned rather than operator-edited — an operator who changes it
// will have it overwritten on the next connection — and the deployment
// documentation says so.
const DefaultConfigFile = "/etc/hcc-agent/config.json"

// LogFormat selects the log encoding.
type LogFormat string

const (
	// LogFormatText is human-readable, for interactive use.
	LogFormatText LogFormat = "text"
	// LogFormatJSON is machine-readable, for journald and log shippers.
	LogFormatJSON LogFormat = "json"
)

// Bounds on the reconnection backoff.
//
// The minimum keeps a controller restart from being met with a reconnection
// storm from the whole fleet at once; the maximum keeps a host from staying
// invisible for an hour after a brief outage has ended. Both are configurable
// within these bounds, and neither can be configured to zero — an agent that
// retried instantly in a loop would be a denial of service against its own
// controller.
const (
	MinReconnectWait     = 1 * time.Second
	MaxReconnectWait     = 15 * time.Minute
	DefaultReconnectMin  = 2 * time.Second
	DefaultReconnectMax  = 60 * time.Second
	DefaultShutdownGrace = 10 * time.Second
)

// Config is the validated agent configuration.
//
// It holds no secret. The agent credential is read from [CredentialFile] at
// the point of use and not retained on this struct, so there is no field for a
// future log line to print — which is a stronger guarantee than a redacting
// String method, because it does not depend on anybody remembering to use one.
type Config struct {
	// ControllerURL is the base URL of the control plane, e.g.
	// https://hcc.example.com. Required. Must be https, unless the host is
	// loopback.
	ControllerURL string

	// CredentialFile is the path to the long-lived agent credential.
	CredentialFile string

	// ConfigFile is where the controller-supplied monitoring configuration is
	// stored. Written atomically on every accepted configuration, read at
	// startup so a restarted agent collects immediately rather than waiting
	// for the controller.
	ConfigFile string

	// CAFile optionally points at a PEM bundle to trust in addition to the
	// system roots. For an internal CA the operator would rather not install
	// host-wide.
	CAFile string

	// ShutdownTimeout bounds how long graceful shutdown may take before the
	// process exits regardless.
	ShutdownTimeout time.Duration

	// ReconnectMinWait and ReconnectMaxWait bound the exponential backoff
	// between connection attempts.
	ReconnectMinWait time.Duration
	ReconnectMaxWait time.Duration

	LogLevel  string
	LogFormat LogFormat
}

// defaults are applied before environment and flag overrides.
func defaults() Config {
	return Config{
		CredentialFile:   DefaultCredentialFile,
		ConfigFile:       DefaultConfigFile,
		ShutdownTimeout:  DefaultShutdownGrace,
		ReconnectMinWait: DefaultReconnectMin,
		ReconnectMaxWait: DefaultReconnectMax,
		LogLevel:         "info",
		LogFormat:        LogFormatText,
	}
}

// ValidationError reports one or more configuration problems.
//
// Collecting every problem rather than failing on the first means an operator
// fixes their unit file once instead of iterating through errors one restart
// at a time.
type ValidationError struct {
	Problems []string
}

func (e *ValidationError) Error() string {
	if len(e.Problems) == 1 {
		return fmt.Sprintf("invalid configuration: %s", e.Problems[0])
	}
	return fmt.Sprintf(
		"invalid configuration:\n  - %s",
		strings.Join(e.Problems, "\n  - "),
	)
}

// Load builds a Config from the environment.
//
// lookupEnv is injected so tests do not mutate the real process environment.
// Pass os.LookupEnv in production.
func Load(lookupEnv func(string) (string, bool)) (Config, error) {
	cfg := defaults()
	var problems []string

	if value, ok := lookupEnv(EnvControllerURL); ok {
		cfg.ControllerURL = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvCredentialFile); ok && strings.TrimSpace(value) != "" {
		cfg.CredentialFile = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvConfigFile); ok && strings.TrimSpace(value) != "" {
		cfg.ConfigFile = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvCAFile); ok {
		cfg.CAFile = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvLogLevel); ok {
		cfg.LogLevel = strings.ToLower(strings.TrimSpace(value))
	}
	if value, ok := lookupEnv(EnvLogFormat); ok {
		cfg.LogFormat = LogFormat(strings.ToLower(strings.TrimSpace(value)))
	}

	for _, spec := range []struct {
		name   string
		target *time.Duration
	}{
		{EnvShutdownTimeout, &cfg.ShutdownTimeout},
		{EnvReconnectMinWait, &cfg.ReconnectMinWait},
		{EnvReconnectMaxWait, &cfg.ReconnectMaxWait},
	} {
		value, ok := lookupEnv(spec.name)
		if !ok {
			continue
		}
		duration, err := time.ParseDuration(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a valid duration (expected e.g. 30s, 2m)", spec.name, value,
			))
			continue
		}
		*spec.target = duration
	}

	if len(problems) > 0 {
		return Config{}, &ValidationError{Problems: problems}
	}
	return cfg, nil
}

// EnrollmentCredential reads the one-time enrollment credential.
//
// Separate from [Load] because it is needed by exactly one subcommand, and
// loading it unconditionally would mean the long-running `run` process held a
// spent secret in memory for no reason.
//
// The file form is preferred and checked first: it keeps the secret out of the
// process environment, where it is readable through /proc/<pid>/environ and
// inherited by every child process. The environment form is still supported,
// because an operator pasting a fifteen-minute credential into a terminal
// should not have to create a file first.
func EnrollmentCredential(lookupEnv func(string) (string, bool)) (string, error) {
	if path, ok := lookupEnv(EnvEnrollmentFile); ok && strings.TrimSpace(path) != "" {
		return readSecretFile(strings.TrimSpace(path))
	}
	if value, ok := lookupEnv(EnvEnrollmentToken); ok && strings.TrimSpace(value) != "" {
		return strings.TrimSpace(value), nil
	}
	return "", fmt.Errorf(
		"no enrollment credential: set %s to a file containing it, or %s to the value",
		EnvEnrollmentFile, EnvEnrollmentToken,
	)
}

// readSecretFile reads a secret from disk and refuses an overly permissive file.
//
// The same permission rule the agent credential gets, applied here too. An
// enrollment credential is short-lived, which makes exposure survivable rather
// than acceptable: fifteen minutes is long enough for another local user to
// read the file and enroll a host of their own choosing.
func readSecretFile(path string) (string, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("%s: cannot read %q: %w", EnvEnrollmentFile, path, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf(
			"%s: %q is a symbolic link; the agent cannot tell whose file it would read",
			EnvEnrollmentFile, path,
		)
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		return "", fmt.Errorf(
			"%s: %q has permissions %#o; it must not be group- or world-readable (use chmod 600)",
			EnvEnrollmentFile, path, mode,
		)
	}

	contents, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("%s: cannot read %q: %w", EnvEnrollmentFile, path, err)
	}

	credential := strings.TrimSpace(string(contents))
	if credential == "" {
		return "", fmt.Errorf("%s: %q is empty", EnvEnrollmentFile, path)
	}
	return credential, nil
}

// Validate checks that the configuration is usable.
//
// The controller URL's scheme is validated here *and* in the controller
// client. Two layers deliberately: this one gives the operator a message at
// startup naming the environment variable, and the client's is the one that
// cannot be bypassed by a future caller that builds a client directly.
func (c *Config) Validate() error {
	var problems []string

	if c.ControllerURL == "" {
		problems = append(problems, fmt.Sprintf("%s is required", EnvControllerURL))
	}

	if strings.TrimSpace(c.CredentialFile) == "" {
		problems = append(problems, fmt.Sprintf("%s must not be empty", EnvCredentialFile))
	}

	if strings.TrimSpace(c.ConfigFile) == "" {
		problems = append(problems, fmt.Sprintf("%s must not be empty", EnvConfigFile))
	} else if c.ConfigFile == c.CredentialFile {
		// Caught explicitly, because the failure otherwise is that the first
		// accepted configuration overwrites the credential and the agent
		// locks itself out of its own host — recoverable only by re-enrolling.
		problems = append(problems, fmt.Sprintf(
			"%s and %s must not be the same path", EnvConfigFile, EnvCredentialFile,
		))
	}

	if c.ShutdownTimeout <= 0 {
		problems = append(problems, fmt.Sprintf(
			"%s: must be positive, got %s", EnvShutdownTimeout, c.ShutdownTimeout,
		))
	}

	if c.ReconnectMinWait < MinReconnectWait || c.ReconnectMinWait > MaxReconnectWait {
		problems = append(problems, fmt.Sprintf(
			"%s: %s is outside the supported range %s to %s",
			EnvReconnectMinWait, c.ReconnectMinWait, MinReconnectWait, MaxReconnectWait,
		))
	}
	if c.ReconnectMaxWait < MinReconnectWait || c.ReconnectMaxWait > MaxReconnectWait {
		problems = append(problems, fmt.Sprintf(
			"%s: %s is outside the supported range %s to %s",
			EnvReconnectMaxWait, c.ReconnectMaxWait, MinReconnectWait, MaxReconnectWait,
		))
	}
	if c.ReconnectMaxWait < c.ReconnectMinWait {
		problems = append(problems, fmt.Sprintf(
			"%s (%s) must not be shorter than %s (%s)",
			EnvReconnectMaxWait, c.ReconnectMaxWait, EnvReconnectMinWait, c.ReconnectMinWait,
		))
	}

	if c.LogFormat != LogFormatText && c.LogFormat != LogFormatJSON {
		problems = append(problems, fmt.Sprintf(
			"%s: must be %q or %q, got %q",
			EnvLogFormat, LogFormatText, LogFormatJSON, c.LogFormat,
		))
	}

	if !validLogLevel(c.LogLevel) {
		problems = append(problems, fmt.Sprintf(
			"%s: must be one of debug, info, warn, error; got %q", EnvLogLevel, c.LogLevel,
		))
	}

	if len(problems) > 0 {
		return &ValidationError{Problems: problems}
	}
	return nil
}

func validLogLevel(level string) bool {
	switch level {
	case "debug", "info", "warn", "error":
		return true
	default:
		return false
	}
}

// String renders the configuration for logging.
//
// There is no redaction here, and that is the point: this struct holds no
// secret, so there is nothing to redact and nothing to forget to redact. It
// prints the credential *path*, which is exactly what an operator diagnosing
// a permission problem wants to see.
func (c Config) String() string {
	return fmt.Sprintf(
		"Config{ControllerURL:%q CredentialFile:%q CAFile:%q ShutdownTimeout:%s "+
			"ReconnectMinWait:%s ReconnectMaxWait:%s LogLevel:%q LogFormat:%q}",
		c.ControllerURL, c.CredentialFile, c.CAFile, c.ShutdownTimeout,
		c.ReconnectMinWait, c.ReconnectMaxWait, c.LogLevel, c.LogFormat,
	)
}

// LogValue implements slog.LogValuer so the structured form matches String.
func (c Config) LogValue() any { return c.String() }

// ErrNotConfigured is returned by helpers that require enrollment to have
// completed. Exported so callers can test for it with errors.Is.
var ErrNotConfigured = errors.New("agent is not configured")
