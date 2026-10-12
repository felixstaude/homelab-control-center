package config_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
)

// envMap returns a lookup function over a fixed map, so tests never mutate
// the real process environment and can run in parallel.
func envMap(values map[string]string) func(string) (string, bool) {
	return func(key string) (string, bool) {
		value, ok := values[key]
		return value, ok
	}
}

// validEnv is a minimal working configuration.
//
// Note what is *not* here: no credential. The agent credential lives in a
// file read at the point of use, and this struct is deliberately
// secret-free — see the package documentation.
func validEnv() map[string]string {
	return map[string]string{
		config.EnvControllerURL: "https://hcc.internal:8000",
	}
}

func loadAndValidate(t *testing.T, values map[string]string) (config.Config, error) {
	t.Helper()
	cfg, err := config.Load(envMap(values))
	if err != nil {
		return cfg, err
	}
	return cfg, cfg.Validate()
}

func TestLoadAppliesDefaults(t *testing.T) {
	t.Parallel()

	cfg, err := loadAndValidate(t, validEnv())
	if err != nil {
		t.Fatalf("expected a valid configuration, got: %v", err)
	}

	if cfg.CredentialFile != config.DefaultCredentialFile {
		t.Errorf("credential file = %q, want %q", cfg.CredentialFile, config.DefaultCredentialFile)
	}
	if cfg.ShutdownTimeout != config.DefaultShutdownGrace {
		t.Errorf("shutdown timeout = %s, want %s", cfg.ShutdownTimeout, config.DefaultShutdownGrace)
	}
	if cfg.ReconnectMinWait != config.DefaultReconnectMin {
		t.Errorf("reconnect min = %s, want %s", cfg.ReconnectMinWait, config.DefaultReconnectMin)
	}
	if cfg.ReconnectMaxWait != config.DefaultReconnectMax {
		t.Errorf("reconnect max = %s, want %s", cfg.ReconnectMaxWait, config.DefaultReconnectMax)
	}
	if cfg.LogLevel != "info" || cfg.LogFormat != config.LogFormatText {
		t.Errorf("logging defaults = %q/%q, want info/text", cfg.LogLevel, cfg.LogFormat)
	}
}

func TestValidateRequiresControllerURL(t *testing.T) {
	t.Parallel()

	_, err := loadAndValidate(t, map[string]string{})
	if err == nil {
		t.Fatal("expected a missing controller URL to be rejected")
	}
	if !strings.Contains(err.Error(), config.EnvControllerURL) {
		t.Errorf("the error should name %s, got: %v", config.EnvControllerURL, err)
	}
}

func TestValidateRequiresANonEmptyCredentialPath(t *testing.T) {
	t.Parallel()

	// An empty path is distinct from an unset one: unset takes the default,
	// while an explicitly empty value is a mistake in a unit file that would
	// otherwise send the agent looking for a credential at "".
	values := validEnv()
	values[config.EnvCredentialFile] = "   "
	cfg, err := config.Load(envMap(values))
	if err != nil {
		t.Fatalf("load: %v", err)
	}

	// Load treats whitespace as unset and keeps the default, which is the
	// forgiving reading and the right one for a path.
	if cfg.CredentialFile != config.DefaultCredentialFile {
		t.Errorf("credential file = %q, want the default", cfg.CredentialFile)
	}

	cfg.CredentialFile = ""
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected an empty credential path to be rejected")
	}
}

func TestReconnectBoundsAreEnforced(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name    string
		values  map[string]string
		wantErr bool
	}{
		{"at the minimum", map[string]string{config.EnvReconnectMinWait: "1s"}, false},
		{"at the maximum", map[string]string{config.EnvReconnectMaxWait: "15m"}, false},
		// Zero would be a tight retry loop against the agent's own controller,
		// which is what the backoff exists to prevent.
		{"zero minimum", map[string]string{config.EnvReconnectMinWait: "0s"}, true},
		{"negative minimum", map[string]string{config.EnvReconnectMinWait: "-5s"}, true},
		{"beyond the maximum", map[string]string{config.EnvReconnectMaxWait: "2h"}, true},
		// A maximum below the minimum would make the backoff run backwards.
		{
			"max below min",
			map[string]string{
				config.EnvReconnectMinWait: "30s",
				config.EnvReconnectMaxWait: "5s",
			},
			true,
		},
		{"unparseable", map[string]string{config.EnvReconnectMinWait: "soon"}, true},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			values := validEnv()
			for key, value := range testCase.values {
				values[key] = value
			}

			_, err := loadAndValidate(t, values)
			if testCase.wantErr && err == nil {
				t.Fatalf("expected %v to be rejected", testCase.values)
			}
			if !testCase.wantErr && err != nil {
				t.Fatalf("expected %v to be accepted, got: %v", testCase.values, err)
			}
		})
	}
}

func TestLoadReportsAllProblemsAtOnce(t *testing.T) {
	t.Parallel()

	// An operator fixing a unit file should see every problem in one go rather
	// than discovering them one restart at a time.
	values := validEnv()
	values[config.EnvShutdownTimeout] = "not-a-duration"
	values[config.EnvReconnectMinWait] = "also-not"

	_, err := config.Load(envMap(values))
	if err == nil {
		t.Fatal("expected the load to fail")
	}
	message := err.Error()
	if !strings.Contains(message, config.EnvShutdownTimeout) ||
		!strings.Contains(message, config.EnvReconnectMinWait) {
		t.Errorf("both problems should be reported, got: %v", err)
	}
}

func TestValidateRejectsUnknownLogLevelAndFormat(t *testing.T) {
	t.Parallel()

	values := validEnv()
	values[config.EnvLogLevel] = "chatty"
	values[config.EnvLogFormat] = "yaml"

	_, err := loadAndValidate(t, values)
	if err == nil {
		t.Fatal("expected an invalid log level and format to be rejected")
	}
}

func TestShutdownTimeoutMustBePositive(t *testing.T) {
	t.Parallel()

	values := validEnv()
	values[config.EnvShutdownTimeout] = "0s"

	if _, err := loadAndValidate(t, values); err == nil {
		t.Fatal("expected a zero shutdown timeout to be rejected")
	}
}

// ---------------------------------------------------------------------------
// The removed settings
//
// Asserted as absences, because the point of removing them is that they
// cannot come back by accident. A test that merely stopped exercising them
// would leave the door open.
// ---------------------------------------------------------------------------

func TestThereIsNoWayToSkipTLSVerification(t *testing.T) {
	t.Parallel()

	// Both the variable the old agent honoured and the obvious alternatives.
	// Setting any of them must be inert: a setting the agent silently ignores
	// is far better than one it obeys, and this test is what keeps it that way
	// if somebody reintroduces the field without reading the package docs.
	values := validEnv()
	for _, key := range []string{
		"HCC_AGENT_TLS_SKIP_VERIFY",
		"HCC_AGENT_INSECURE",
		"HCC_AGENT_INSECURE_SKIP_VERIFY",
	} {
		values[key] = "true"
	}

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("the variables should be ignored, not rejected: %v", err)
	}

	rendered := cfg.String()
	for _, word := range []string{"SkipVerify", "Insecure", "skip_verify"} {
		if strings.Contains(rendered, word) {
			t.Errorf("Config still has a verification-skipping field: %s", rendered)
		}
	}
}

func TestTheConfigurationCarriesNoSecret(t *testing.T) {
	t.Parallel()

	// The strongest form of "the token is never logged": there is no token.
	// A redacting String method depends on everybody using it; an absent field
	// depends on nothing.
	values := validEnv()
	values["HCC_AGENT_TOKEN"] = "a-secret-that-should-be-ignored"

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("load: %v", err)
	}

	for _, rendered := range []string{cfg.String(), stringify(cfg.LogValue())} {
		if strings.Contains(rendered, "a-secret-that-should-be-ignored") {
			t.Errorf("the configuration leaked a value from the environment: %s", rendered)
		}
		if strings.Contains(strings.ToLower(rendered), "token:") {
			t.Errorf("the configuration still has a token field: %s", rendered)
		}
	}
}

func TestTheRenderedConfigurationNamesTheCredentialPath(t *testing.T) {
	t.Parallel()

	// The path is exactly what an operator diagnosing a permission problem
	// needs, and it is not a secret.
	values := validEnv()
	values[config.EnvCredentialFile] = "/etc/hcc-agent/credential"

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("load: %v", err)
	}

	if !strings.Contains(cfg.String(), "/etc/hcc-agent/credential") {
		t.Errorf("the credential path should be visible, got: %s", cfg.String())
	}
}

// ---------------------------------------------------------------------------
// The enrollment credential
// ---------------------------------------------------------------------------

func TestEnrollmentCredentialReadsTheFileForm(t *testing.T) {
	t.Parallel()

	path := writeSecret(t, "  enrollment-credential\n", 0o600)

	credential, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile: path,
	}))
	if err != nil {
		t.Fatalf("expected the file to be read: %v", err)
	}
	if credential != "enrollment-credential" {
		t.Errorf("credential = %q, want it trimmed", credential)
	}
}

func TestEnrollmentCredentialPrefersTheFileOverTheEnvironment(t *testing.T) {
	t.Parallel()

	// The file form keeps the secret out of /proc/<pid>/environ, where it is
	// readable by anyone who can see the process and is inherited by every
	// child. When both are given, the safer one wins.
	path := writeSecret(t, "from-the-file", 0o600)

	credential, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile:  path,
		config.EnvEnrollmentToken: "from-the-environment",
	}))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if credential != "from-the-file" {
		t.Errorf("credential = %q, want the file's value", credential)
	}
}

func TestEnrollmentCredentialAcceptsTheEnvironmentForm(t *testing.T) {
	t.Parallel()

	// Supported because an operator pasting a fifteen-minute credential into a
	// terminal should not have to create a file first.
	credential, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentToken: "  pasted-value  ",
	}))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if credential != "pasted-value" {
		t.Errorf("credential = %q, want it trimmed", credential)
	}
}

func TestEnrollmentCredentialRejectsAGroupReadableFile(t *testing.T) {
	t.Parallel()

	// Short-lived is not the same as safe to leave lying around: fifteen
	// minutes is long enough for another local user to read it and enroll a
	// host of their own choosing.
	path := writeSecret(t, "exposed", 0o640)

	_, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile: path,
	}))
	if err == nil {
		t.Fatal("expected a group-readable credential file to be rejected")
	}
	if !strings.Contains(err.Error(), "chmod 600") {
		t.Errorf("the error should name the fix, got: %v", err)
	}
}

func TestEnrollmentCredentialRejectsASymlink(t *testing.T) {
	t.Parallel()

	// The classic local attack: point the path at a file you own, so the agent
	// reads a credential you chose.
	target := writeSecret(t, "planted", 0o600)
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	_, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile: link,
	}))
	if err == nil {
		t.Fatal("expected a symlinked credential file to be rejected")
	}
	if !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("the error should name the problem, got: %v", err)
	}
}

func TestEnrollmentCredentialReportsAMissingFile(t *testing.T) {
	t.Parallel()

	_, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile: filepath.Join(t.TempDir(), "absent"),
	}))
	if err == nil {
		t.Fatal("expected a missing credential file to be reported")
	}
}

func TestEnrollmentCredentialReportsAnEmptyFile(t *testing.T) {
	t.Parallel()

	path := writeSecret(t, "   \n", 0o600)

	_, err := config.EnrollmentCredential(envMap(map[string]string{
		config.EnvEnrollmentFile: path,
	}))
	if err == nil {
		t.Fatal("expected an empty credential file to be reported")
	}
}

func TestEnrollmentCredentialNamesBothVariablesWhenAbsent(t *testing.T) {
	t.Parallel()

	// The message is the only instruction an operator gets at this point, so
	// it has to name what to set rather than only that something is missing.
	_, err := config.EnrollmentCredential(envMap(map[string]string{}))
	if err == nil {
		t.Fatal("expected an absent credential to be reported")
	}
	if !strings.Contains(err.Error(), config.EnvEnrollmentFile) ||
		!strings.Contains(err.Error(), config.EnvEnrollmentToken) {
		t.Errorf("the error should name both variables, got: %v", err)
	}
}

// writeSecret creates a file with the given contents and mode.
func writeSecret(t *testing.T, contents string, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(path, []byte(contents), mode); err != nil {
		t.Fatalf("write: %v", err)
	}
	// WriteFile's mode is subject to the umask, so it is set explicitly —
	// otherwise the permission tests would pass for the wrong reason on a
	// machine with a restrictive umask.
	if err := os.Chmod(path, mode); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	return path
}

func stringify(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

// A compile-time reminder that the duration bounds are exported and meant to
// be referenced rather than duplicated.
var _ = []time.Duration{config.MinReconnectWait, config.MaxReconnectWait}
