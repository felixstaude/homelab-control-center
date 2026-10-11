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
func validEnv() map[string]string {
	return map[string]string{
		config.EnvControllerURL: "https://hcc.internal:8000",
		config.EnvToken:         "test-token-value",
		config.EnvHostname:      "test-host",
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

	if cfg.ReportInterval != 30*time.Second {
		t.Errorf("ReportInterval = %s, want 30s", cfg.ReportInterval)
	}
	if cfg.ShutdownTimeout != 10*time.Second {
		t.Errorf("ShutdownTimeout = %s, want 10s", cfg.ShutdownTimeout)
	}
	if cfg.LogLevel != "info" {
		t.Errorf("LogLevel = %q, want \"info\"", cfg.LogLevel)
	}
	if cfg.LogFormat != config.LogFormatText {
		t.Errorf("LogFormat = %q, want %q", cfg.LogFormat, config.LogFormatText)
	}
	if cfg.TLSSkipVerify {
		t.Error("TLSSkipVerify must default to false; insecure behaviour must be opt-in")
	}
}

func TestValidateRequiresControllerURL(t *testing.T) {
	t.Parallel()

	values := validEnv()
	delete(values, config.EnvControllerURL)

	_, err := loadAndValidate(t, values)
	if err == nil {
		t.Fatal("expected an error when the controller URL is missing")
	}
	if !strings.Contains(err.Error(), config.EnvControllerURL) {
		t.Errorf("error should name the missing variable, got: %v", err)
	}
}

func TestValidateRequiresToken(t *testing.T) {
	t.Parallel()

	values := validEnv()
	delete(values, config.EnvToken)

	_, err := loadAndValidate(t, values)
	if err == nil {
		t.Fatal("expected an error when no token is configured")
	}
	if !strings.Contains(err.Error(), config.EnvTokenFile) {
		t.Errorf("error should mention the preferred token file variable, got: %v", err)
	}
}

func TestValidateRejectsNonHTTPSchemes(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		"ftp://hcc.internal",
		"file:///etc/passwd",
		"unix:///var/run/docker.sock",
	} {
		t.Run(raw, func(t *testing.T) {
			t.Parallel()

			values := validEnv()
			values[config.EnvControllerURL] = raw

			if _, err := loadAndValidate(t, values); err == nil {
				t.Errorf("expected %q to be rejected", raw)
			}
		})
	}
}

func TestValidateRejectsURLWithoutHost(t *testing.T) {
	t.Parallel()

	values := validEnv()
	values[config.EnvControllerURL] = "https://"

	if _, err := loadAndValidate(t, values); err == nil {
		t.Error("expected a URL with no host to be rejected")
	}
}

func TestLoadReportsAllProblemsAtOnce(t *testing.T) {
	t.Parallel()

	values := validEnv()
	values[config.EnvReportInterval] = "not-a-duration"
	values[config.EnvShutdownTimeout] = "also-not-a-duration"
	values[config.EnvTLSSkipVerify] = "maybe"

	_, err := config.Load(envMap(values))
	if err == nil {
		t.Fatal("expected an error")
	}

	// An operator should be able to fix their unit file in one pass.
	var validationErr *config.ValidationError
	if !asValidationError(err, &validationErr) {
		t.Fatalf("expected a *config.ValidationError, got %T", err)
	}
	if len(validationErr.Problems) != 3 {
		t.Errorf("expected 3 problems, got %d: %v", len(validationErr.Problems), validationErr.Problems)
	}
}

func TestReportIntervalBounds(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		interval string
		wantErr  bool
	}{
		{"below minimum", "1s", true},
		{"at minimum", "5s", false},
		{"typical", "30s", false},
		{"at maximum", "15m", false},
		{"above maximum", "1h", true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			values := validEnv()
			values[config.EnvReportInterval] = tc.interval

			_, err := loadAndValidate(t, values)
			if tc.wantErr && err == nil {
				t.Errorf("interval %s should have been rejected", tc.interval)
			}
			if !tc.wantErr && err != nil {
				t.Errorf("interval %s should have been accepted, got: %v", tc.interval, err)
			}
		})
	}
}

func TestValidateRejectsUnknownLogLevelAndFormat(t *testing.T) {
	t.Parallel()

	values := validEnv()
	values[config.EnvLogLevel] = "trace"
	values[config.EnvLogFormat] = "xml"

	if _, err := loadAndValidate(t, values); err == nil {
		t.Error("expected unknown log level and format to be rejected")
	}
}

func TestHostnameDefaultsToOSHostname(t *testing.T) {
	t.Parallel()

	values := validEnv()
	delete(values, config.EnvHostname)

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expected, err := os.Hostname()
	if err != nil {
		t.Skipf("OS hostname unavailable on this machine: %v", err)
	}
	if cfg.Hostname != expected {
		t.Errorf("Hostname = %q, want %q", cfg.Hostname, expected)
	}
}

// --- Token file handling ---

func TestTokenFileIsReadAndTrimmed(t *testing.T) {
	t.Parallel()

	path := writeTokenFile(t, "  secret-from-file\n", 0o600)

	values := validEnv()
	delete(values, config.EnvToken)
	values[config.EnvTokenFile] = path

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Token != "secret-from-file" {
		t.Errorf("Token = %q, want %q", cfg.Token, "secret-from-file")
	}
}

func TestTokenFileTakesPrecedenceOverEnvVar(t *testing.T) {
	t.Parallel()

	path := writeTokenFile(t, "from-file", 0o600)

	values := validEnv()
	values[config.EnvToken] = "from-env"
	values[config.EnvTokenFile] = path

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Token != "from-file" {
		t.Errorf("Token = %q, want the file to win", cfg.Token)
	}
}

func TestTokenFileWithLoosePermissionsIsRejected(t *testing.T) {
	t.Parallel()

	for _, mode := range []os.FileMode{0o644, 0o640, 0o604, 0o666} {
		t.Run(mode.String(), func(t *testing.T) {
			t.Parallel()

			path := writeTokenFile(t, "secret", mode)

			values := validEnv()
			delete(values, config.EnvToken)
			values[config.EnvTokenFile] = path

			_, err := config.Load(envMap(values))
			if err == nil {
				t.Fatalf("a token file with mode %#o must be rejected", mode.Perm())
			}
			if !strings.Contains(err.Error(), "readable") {
				t.Errorf("error should explain the permission problem, got: %v", err)
			}
		})
	}
}

func TestMissingTokenFileIsReported(t *testing.T) {
	t.Parallel()

	values := validEnv()
	delete(values, config.EnvToken)
	values[config.EnvTokenFile] = filepath.Join(t.TempDir(), "does-not-exist")

	if _, err := config.Load(envMap(values)); err == nil {
		t.Error("expected a missing token file to be an error, not silently ignored")
	}
}

func TestEmptyTokenFileIsReported(t *testing.T) {
	t.Parallel()

	path := writeTokenFile(t, "   \n", 0o600)

	values := validEnv()
	delete(values, config.EnvToken)
	values[config.EnvTokenFile] = path

	if _, err := config.Load(envMap(values)); err == nil {
		t.Error("expected an empty token file to be an error")
	}
}

// --- Secret redaction ---

func TestStringRedactsTheToken(t *testing.T) {
	t.Parallel()

	const secret = "super-secret-enrolment-token"

	values := validEnv()
	values[config.EnvToken] = secret

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	rendered := cfg.String()
	if strings.Contains(rendered, secret) {
		t.Errorf("String() leaked the token: %s", rendered)
	}
	if !strings.Contains(rendered, "<redacted>") {
		t.Errorf("String() should mark the token as redacted, got: %s", rendered)
	}
	// Non-secret fields must still be visible, or the output is useless.
	if !strings.Contains(rendered, "hcc.internal") {
		t.Errorf("String() should include the controller URL, got: %s", rendered)
	}
}

func TestLogValueRedactsTheToken(t *testing.T) {
	t.Parallel()

	const secret = "another-secret-token"

	values := validEnv()
	values[config.EnvToken] = secret

	cfg, err := loadAndValidate(t, values)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Guards the slog path specifically: String() alone would not protect a
	// logger that formats the value as a structured attribute.
	rendered, ok := cfg.LogValue().(string)
	if !ok {
		t.Fatalf("LogValue() returned %T, want string", cfg.LogValue())
	}
	if strings.Contains(rendered, secret) {
		t.Errorf("LogValue() leaked the token: %s", rendered)
	}
}

func TestStringMarksAnUnsetToken(t *testing.T) {
	t.Parallel()

	var cfg config.Config
	if !strings.Contains(cfg.String(), "<unset>") {
		t.Errorf("an empty token should render as <unset>, got: %s", cfg.String())
	}
}

// --- helpers ---

func writeTokenFile(t *testing.T, contents string, mode os.FileMode) string {
	t.Helper()

	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(contents), mode); err != nil {
		t.Fatalf("writing token file: %v", err)
	}
	// WriteFile honours umask, so set the mode explicitly.
	if err := os.Chmod(path, mode); err != nil {
		t.Fatalf("chmod token file: %v", err)
	}
	return path
}

func asValidationError(err error, target **config.ValidationError) bool {
	for err != nil {
		if ve, ok := err.(*config.ValidationError); ok {
			*target = ve
			return true
		}
		unwrapped, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = unwrapped.Unwrap()
	}
	return false
}
