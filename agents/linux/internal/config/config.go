// Package config loads and validates agent configuration.
//
// Configuration comes from the environment, with command-line flags able to
// override individual values. Validation happens once at startup: an agent
// with a bad configuration exits immediately with a readable message rather
// than running in a degraded state that is hard to diagnose remotely.
//
// The enrolment token is read from the environment or from a file on disk. It
// is never accepted as a command-line flag, because command lines are visible
// to every user on the host via /proc.
package config

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Environment variable names. All agent configuration is prefixed HCC_AGENT_.
const (
	EnvControllerURL   = "HCC_AGENT_CONTROLLER_URL"
	EnvToken           = "HCC_AGENT_TOKEN"
	EnvTokenFile       = "HCC_AGENT_TOKEN_FILE"
	EnvHostname        = "HCC_AGENT_HOSTNAME"
	EnvLogLevel        = "HCC_AGENT_LOG_LEVEL"
	EnvLogFormat       = "HCC_AGENT_LOG_FORMAT"
	EnvReportInterval  = "HCC_AGENT_REPORT_INTERVAL"
	EnvShutdownTimeout = "HCC_AGENT_SHUTDOWN_TIMEOUT"
	EnvTLSSkipVerify   = "HCC_AGENT_TLS_SKIP_VERIFY"
)

// LogFormat selects the log encoding.
type LogFormat string

const (
	// LogFormatText is human-readable, for interactive use.
	LogFormatText LogFormat = "text"
	// LogFormatJSON is machine-readable, for journald and log shippers.
	LogFormatJSON LogFormat = "json"
)

// Bounds on the reporting interval. Below the minimum an agent would generate
// more load than signal; above the maximum the controller cannot distinguish
// a slow agent from a dead one within a useful window.
const (
	MinReportInterval = 5 * time.Second
	MaxReportInterval = 15 * time.Minute
)

// Config is the validated agent configuration.
type Config struct {
	// ControllerURL is the base URL of the control plane, e.g.
	// https://hcc.internal:8000. Required.
	ControllerURL string

	// Token authenticates this agent to the controller. Required.
	//
	// Treated as a secret: it is excluded from String() and must never be
	// logged. See the redaction test in config_test.go.
	Token string

	// Hostname identifies this host to the controller. Defaults to the
	// operating system hostname.
	Hostname string

	// ReportInterval is how often the agent will push collected data once
	// collection is implemented. Phase 0 only validates and logs it.
	ReportInterval time.Duration

	// ShutdownTimeout bounds how long graceful shutdown may take before the
	// process exits regardless.
	ShutdownTimeout time.Duration

	LogLevel  string
	LogFormat LogFormat

	// TLSSkipVerify disables TLS certificate verification.
	//
	// Intended only for a homelab running an internal CA that has not been
	// installed on the host yet. It makes the connection vulnerable to
	// interception, so the agent logs a prominent warning whenever it is on.
	TLSSkipVerify bool
}

// Default values applied before environment and flag overrides.
func defaults() Config {
	return Config{
		ReportInterval:  30 * time.Second,
		ShutdownTimeout: 10 * time.Second,
		LogLevel:        "info",
		LogFormat:       LogFormatText,
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
	if value, ok := lookupEnv(EnvHostname); ok {
		cfg.Hostname = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvLogLevel); ok {
		cfg.LogLevel = strings.ToLower(strings.TrimSpace(value))
	}
	if value, ok := lookupEnv(EnvLogFormat); ok {
		cfg.LogFormat = LogFormat(strings.ToLower(strings.TrimSpace(value)))
	}

	// Token: the file form is preferred, because it keeps the secret out of
	// the process environment where it would be readable via /proc/<pid>/environ
	// and inherited by any child process.
	if path, ok := lookupEnv(EnvTokenFile); ok && strings.TrimSpace(path) != "" {
		token, err := readTokenFile(strings.TrimSpace(path))
		if err != nil {
			problems = append(problems, err.Error())
		} else {
			cfg.Token = token
		}
	} else if value, ok := lookupEnv(EnvToken); ok {
		cfg.Token = strings.TrimSpace(value)
	}

	if value, ok := lookupEnv(EnvReportInterval); ok {
		duration, err := time.ParseDuration(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a valid duration (expected e.g. 30s, 2m)", EnvReportInterval, value,
			))
		} else {
			cfg.ReportInterval = duration
		}
	}

	if value, ok := lookupEnv(EnvShutdownTimeout); ok {
		duration, err := time.ParseDuration(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a valid duration (expected e.g. 10s)", EnvShutdownTimeout, value,
			))
		} else {
			cfg.ShutdownTimeout = duration
		}
	}

	if value, ok := lookupEnv(EnvTLSSkipVerify); ok {
		parsed, err := strconv.ParseBool(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a valid boolean", EnvTLSSkipVerify, value,
			))
		} else {
			cfg.TLSSkipVerify = parsed
		}
	}

	if len(problems) > 0 {
		return Config{}, &ValidationError{Problems: problems}
	}
	return cfg, nil
}

// readTokenFile reads a secret from disk and refuses overly permissive files.
func readTokenFile(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("%s: cannot read %q: %w", EnvTokenFile, path, err)
	}

	// Refuse a world- or group-readable token. A secret readable by every
	// local user is not a secret, and silently accepting it would give a false
	// sense of security.
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		return "", fmt.Errorf(
			"%s: %q has permissions %#o; it must not be group- or world-readable (use chmod 600)",
			EnvTokenFile, path, mode,
		)
	}

	contents, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("%s: cannot read %q: %w", EnvTokenFile, path, err)
	}

	token := strings.TrimSpace(string(contents))
	if token == "" {
		return "", fmt.Errorf("%s: %q is empty", EnvTokenFile, path)
	}
	return token, nil
}

// Validate checks that the configuration is usable, applying the OS hostname
// as a default if none was supplied.
func (c *Config) Validate() error {
	var problems []string

	switch {
	case c.ControllerURL == "":
		problems = append(problems, fmt.Sprintf("%s is required", EnvControllerURL))
	default:
		parsed, err := url.Parse(c.ControllerURL)
		switch {
		case err != nil:
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a valid URL", EnvControllerURL, c.ControllerURL,
			))
		case parsed.Scheme != "http" && parsed.Scheme != "https":
			problems = append(problems, fmt.Sprintf(
				"%s: scheme must be http or https, got %q", EnvControllerURL, parsed.Scheme,
			))
		case parsed.Host == "":
			problems = append(problems, fmt.Sprintf(
				"%s: %q has no host", EnvControllerURL, c.ControllerURL,
			))
		}
	}

	if c.Token == "" {
		problems = append(problems, fmt.Sprintf(
			"%s or %s is required", EnvTokenFile, EnvToken,
		))
	}

	if c.Hostname == "" {
		hostname, err := os.Hostname()
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s is not set and the OS hostname could not be determined: %v",
				EnvHostname, err,
			))
		} else {
			c.Hostname = hostname
		}
	}

	if c.ReportInterval < MinReportInterval || c.ReportInterval > MaxReportInterval {
		problems = append(problems, fmt.Sprintf(
			"%s: %s is outside the supported range %s to %s",
			EnvReportInterval, c.ReportInterval, MinReportInterval, MaxReportInterval,
		))
	}

	if c.ShutdownTimeout <= 0 {
		problems = append(problems, fmt.Sprintf(
			"%s: must be positive, got %s", EnvShutdownTimeout, c.ShutdownTimeout,
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

// String renders the configuration for logging with the token redacted.
//
// Config deliberately has no exported secret-bearing formatter other than
// this one. Anything logging a Config must go through here.
func (c Config) String() string {
	return fmt.Sprintf(
		"Config{ControllerURL:%q Hostname:%q Token:%s ReportInterval:%s "+
			"ShutdownTimeout:%s LogLevel:%q LogFormat:%q TLSSkipVerify:%t}",
		c.ControllerURL, c.Hostname, redacted(c.Token), c.ReportInterval,
		c.ShutdownTimeout, c.LogLevel, c.LogFormat, c.TLSSkipVerify,
	)
}

// LogValue implements slog.LogValuer so that logging a Config as a structured
// attribute also redacts the token, not just the %v form.
func (c Config) LogValue() any {
	return c.String()
}

func redacted(secret string) string {
	if secret == "" {
		return "<unset>"
	}
	return "<redacted>"
}

// ErrNotConfigured is returned by helpers that require enrolment to have
// completed. Exported so callers can test for it with errors.Is.
var ErrNotConfigured = errors.New("agent is not configured")
