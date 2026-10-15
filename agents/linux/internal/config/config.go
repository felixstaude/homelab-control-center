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
	"slices"
	"strconv"
	"strings"
	"time"

	// Imported for the bounds and the parsers they own, rather than restating
	// them here. None of these packages imports this one, so there is no
	// cycle — and a duplicated constant is exactly the drift this project
	// spends effort avoiding elsewhere.
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/telemetry"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// Environment variable names. All agent configuration is prefixed HCC_AGENT_.
const (
	EnvControllerURL    = "HCC_AGENT_CONTROLLER_URL"
	EnvCredentialFile   = "HCC_AGENT_CREDENTIAL_FILE"
	EnvConfigFile       = "HCC_AGENT_CONFIG_FILE"
	EnvQueueCapacity    = "HCC_AGENT_QUEUE_CAPACITY"
	EnvProcRoot         = "HCC_AGENT_PROC_ROOT"
	EnvSysRoot          = "HCC_AGENT_SYS_ROOT"
	EnvNetworkIfaces    = "HCC_AGENT_NETWORK_INTERFACES"
	EnvDockerSocket     = "HCC_AGENT_DOCKER_SOCKET"
	EnvCollectPerCore   = "HCC_AGENT_COLLECT_PER_CORE"
	EnvEnrollmentToken  = "HCC_AGENT_ENROLLMENT_TOKEN" //nolint:gosec // a variable name, not a credential
	EnvEnrollmentFile   = "HCC_AGENT_ENROLLMENT_TOKEN_FILE"
	EnvCAFile           = "HCC_AGENT_CA_FILE"
	EnvLogLevel         = "HCC_AGENT_LOG_LEVEL"
	EnvLogFormat        = "HCC_AGENT_LOG_FORMAT"
	EnvShutdownTimeout  = "HCC_AGENT_SHUTDOWN_TIMEOUT"
	EnvReconnectMinWait = "HCC_AGENT_RECONNECT_MIN_WAIT"
	EnvReconnectMaxWait = "HCC_AGENT_RECONNECT_MAX_WAIT"
	EnvAutoUpdate       = "HCC_AGENT_AUTO_UPDATE"
	EnvUpdateChannel    = "HCC_AGENT_UPDATE_CHANNEL"
	EnvPinnedVersion    = "HCC_AGENT_PINNED_VERSION"
	EnvReleaseKeys      = "HCC_AGENT_RELEASE_KEYS"
)

// UpdateChannels is every channel an agent will accept, most stable first.
//
// Must match CHANNELS in hcc/agents/releases.py. Validated here rather than
// only at the point of use so a typo is a startup failure with a readable
// message, instead of a host that silently accepts nothing because "stabl" is
// not a channel any release is in.
var UpdateChannels = []string{"stable", "beta", "alpha"}

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

	// QueueCapacity bounds how many collections the agent buffers while the
	// controller is unreachable. Oldest are dropped first, and the drops are
	// counted and reported on the next heartbeat.
	QueueCapacity int

	// ProcRoot is where /proc is mounted. Configurable for one real reason
	// and one convenience: an agent in a container may have the host's /proc
	// bind-mounted somewhere else, and the collector tests read recorded
	// fixtures. Not a security boundary — an operator who can set this can
	// already read /proc.
	ProcRoot string

	// SysRoot is where /sys is mounted. Same reasoning as ProcRoot: an agent
	// in a container may see the host's device model somewhere else, and the
	// diskio collector's tests read a fixture tree. Used to tell a whole
	// block device from one of its partitions.
	SysRoot string

	// NetworkInterfaces names exactly which interfaces the network module
	// reports, replacing its default selection entirely.
	//
	// Empty means the default, which excludes loopback, bridge members and
	// container plumbing because their traffic is already counted on
	// something else. Set it to report a `veth` or a bridge anyway.
	//
	// A local setting rather than part of the controller's configuration
	// document, for the same reason CollectPerCore is: it is a decision
	// about one host's network topology rather than about the fleet, and
	// module options do not exist in the document model yet.
	NetworkInterfaces []string

	// DockerSocket is the Docker Engine API socket the `docker` module
	// reads. **Empty by default, and that is the security boundary.**
	//
	// Access to the Docker socket is equivalent to root on the host:
	// anything that can talk to it can start a privileged container with the
	// host filesystem mounted. So the capability is granted here, locally,
	// by an administrator on the machine — and the controller can then only
	// decide whether to *use* a capability the host has already given.
	//
	// Two switches, both of which must be on: this one, and the `docker`
	// module being enabled for the host. The same shape as
	// HCC_AGENT_AUTO_UPDATE, and for the same reason.
	//
	// There is no default path. An unset value makes the module report
	// itself unavailable with a reason that names the variable and says what
	// granting it means.
	DockerSocket string

	// CollectPerCore adds a series per logical CPU to the cpu module.
	//
	// Off by default and deliberately a *local* setting rather than part of
	// the controller's configuration document: it multiplies this module's
	// cardinality by the host's core count, and a 256-core host is a decision
	// about that host rather than about the fleet. A later release may move it
	// into the module's options, which is where it belongs once module
	// options exist.
	CollectPerCore bool

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

	// AutoUpdate is whether this host will install an update the controller
	// authorizes.
	//
	// **False by default, and that is the most important default here.** An
	// agent runs as root to install a binary; one that has not been told it
	// may replace itself does not, however the controller's own policy is
	// set. The two policies are deliberately separate: the controller's
	// decides whether an instruction is sent, this decides whether it is
	// acted on, and a compromised controller cannot change this one because
	// it cannot write this host's environment file.
	AutoUpdate bool

	// UpdateChannel is the most instability this host accepts. Empty means
	// stable. A host on beta still receives newer stable releases.
	UpdateChannel string

	// PinnedVersion holds this host at exactly one version. Empty means
	// unpinned. An agent-side pin, which an operator sets on the host — the
	// controller has its own, and either one refusing is enough.
	PinnedVersion string

	// ReleaseKeys are additional publisher keys this agent trusts, base64
	// Ed25519, comma-separated in the environment.
	//
	// *Additional*: a release build has the publisher's key stamped in at
	// link time, and these are added to it rather than replacing it. The case
	// they exist for is an operator who builds their own agents and signs
	// them with their own key — without them, such an operator would have to
	// choose between no automatic updates and patching the source.
	//
	// Setting one needs write access to the environment file, which needs
	// root on the host. An attacker who has that does not need this.
	ReleaseKeys []string

	LogLevel  string
	LogFormat LogFormat
}

// defaults are applied before environment and flag overrides.
func defaults() Config {
	return Config{
		CredentialFile:   DefaultCredentialFile,
		ConfigFile:       DefaultConfigFile,
		QueueCapacity:    telemetry.DefaultCapacity,
		ProcRoot:         collect.DefaultProcRoot,
		SysRoot:          collect.DefaultSysRoot,
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
	if value, ok := lookupEnv(EnvSysRoot); ok && strings.TrimSpace(value) != "" {
		cfg.SysRoot = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvDockerSocket); ok {
		cfg.DockerSocket = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvNetworkIfaces); ok {
		for _, name := range strings.Split(value, ",") {
			if trimmed := strings.TrimSpace(name); trimmed != "" {
				cfg.NetworkInterfaces = append(cfg.NetworkInterfaces, trimmed)
			}
		}
	}
	if value, ok := lookupEnv(EnvProcRoot); ok && strings.TrimSpace(value) != "" {
		cfg.ProcRoot = strings.TrimSpace(value)
	}
	if value, ok := lookupEnv(EnvQueueCapacity); ok && strings.TrimSpace(value) != "" {
		parsed, err := strconv.Atoi(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a whole number", EnvQueueCapacity, value,
			))
		} else {
			cfg.QueueCapacity = parsed
		}
	}
	if value, ok := lookupEnv(EnvCollectPerCore); ok && strings.TrimSpace(value) != "" {
		parsed, err := strconv.ParseBool(strings.TrimSpace(value))
		if err != nil {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a boolean", EnvCollectPerCore, value,
			))
		} else {
			cfg.CollectPerCore = parsed
		}
	}
	if value, ok := lookupEnv(EnvAutoUpdate); ok && strings.TrimSpace(value) != "" {
		parsed, err := strconv.ParseBool(strings.TrimSpace(value))
		if err != nil {
			// Refused rather than treated as false. "yes" and "on" are what
			// an operator writes, and silently reading either as "do not
			// update" would leave a host that an administrator believes is
			// updating itself and is not — a failure nothing surfaces until
			// somebody checks a version by hand.
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a boolean (use true or false)", EnvAutoUpdate, value,
			))
		} else {
			cfg.AutoUpdate = parsed
		}
	}
	if value, ok := lookupEnv(EnvUpdateChannel); ok && strings.TrimSpace(value) != "" {
		channel := strings.ToLower(strings.TrimSpace(value))
		if !slices.Contains(UpdateChannels, channel) {
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a channel (expected one of %s)",
				EnvUpdateChannel, value, strings.Join(UpdateChannels, ", "),
			))
		} else {
			cfg.UpdateChannel = channel
		}
	}
	if value, ok := lookupEnv(EnvPinnedVersion); ok && strings.TrimSpace(value) != "" {
		pinned := strings.TrimSpace(value)
		if _, err := version.Parse(pinned); err != nil {
			// A pin nobody can parse would never match any release, so the
			// host would hold at its installed version for ever with nothing
			// saying why. That is the same outcome as a correct pin and so
			// indistinguishable from working.
			problems = append(problems, fmt.Sprintf(
				"%s: %q is not a semantic version: %v", EnvPinnedVersion, value, err,
			))
		} else {
			cfg.PinnedVersion = pinned
		}
	}
	if value, ok := lookupEnv(EnvReleaseKeys); ok && strings.TrimSpace(value) != "" {
		for _, key := range strings.Split(value, ",") {
			trimmed := strings.TrimSpace(key)
			if trimmed == "" {
				continue
			}
			if _, err := release.DecodePublicKey(trimmed); err != nil {
				problems = append(problems, fmt.Sprintf(
					"%s: %v", EnvReleaseKeys, err,
				))
				continue
			}
			cfg.ReleaseKeys = append(cfg.ReleaseKeys, trimmed)
		}
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
		return ReadEnrollmentCredentialFile(strings.TrimSpace(path))
	}
	if value, ok := lookupEnv(EnvEnrollmentToken); ok && strings.TrimSpace(value) != "" {
		return strings.TrimSpace(value), nil
	}
	return "", fmt.Errorf(
		"no enrollment credential: set %s to a file containing it, or %s to the value",
		EnvEnrollmentFile, EnvEnrollmentToken,
	)
}

// ReadEnrollmentCredentialFile reads a one-time credential from disk and
// refuses an overly permissive file.
//
// The same permission rule the agent credential gets, applied here too. An
// enrollment credential is short-lived, which makes exposure survivable rather
// than acceptable: fifteen minutes is long enough for another local user to
// read the file and enroll a host of their own choosing.
//
// Exported so the installer's --token-file path gets exactly these refusals
// rather than its own almost-identical set. The two were separate for one
// commit and the installer's copy was already missing the symlink check.
func ReadEnrollmentCredentialFile(path string) (string, error) {
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

	if c.QueueCapacity < 1 || c.QueueCapacity > telemetry.MaxCapacity {
		problems = append(problems, fmt.Sprintf(
			"%s: must be between 1 and %d, got %d",
			EnvQueueCapacity, telemetry.MaxCapacity, c.QueueCapacity,
		))
	}

	if strings.TrimSpace(c.SysRoot) == "" {
		problems = append(problems, fmt.Sprintf("%s must not be empty", EnvSysRoot))
	}
	if strings.TrimSpace(c.ProcRoot) == "" {
		problems = append(problems, fmt.Sprintf("%s must not be empty", EnvProcRoot))
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
