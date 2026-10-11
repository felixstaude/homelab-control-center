package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// captureRun executes run() with its output redirected to temporary files, so
// the exit code and the operator-visible output can both be asserted.
func captureRun(t *testing.T, args []string) (code int, stdout, stderr string) {
	t.Helper()

	dir := t.TempDir()

	outFile, err := os.Create(filepath.Join(dir, "stdout"))
	if err != nil {
		t.Fatalf("creating stdout file: %v", err)
	}
	defer outFile.Close()

	errFile, err := os.Create(filepath.Join(dir, "stderr"))
	if err != nil {
		t.Fatalf("creating stderr file: %v", err)
	}
	defer errFile.Close()

	code = run(args, outFile, errFile)

	outBytes, err := os.ReadFile(filepath.Join(dir, "stdout"))
	if err != nil {
		t.Fatalf("reading stdout: %v", err)
	}
	errBytes, err := os.ReadFile(filepath.Join(dir, "stderr"))
	if err != nil {
		t.Fatalf("reading stderr: %v", err)
	}

	return code, string(outBytes), string(errBytes)
}

func TestVersionFlagPrintsVersionAndExitsZero(t *testing.T) {
	code, stdout, _ := captureRun(t, []string{"-version"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
	if !strings.Contains(stdout, "hcc-agent") {
		t.Errorf("expected version output on stdout, got: %q", stdout)
	}
	// -version must not require a valid configuration, so an operator can
	// check what they installed before configuring it.
	if strings.Contains(stdout, "invalid configuration") {
		t.Error("-version must not require configuration")
	}
}

func TestHelpFlagExitsZero(t *testing.T) {
	code, _, stderr := captureRun(t, []string{"-h"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d for -h", code, exitOK)
	}
	if !strings.Contains(stderr, "Usage") {
		t.Errorf("expected usage text, got: %q", stderr)
	}
	// Usage should document the environment, which is where real config lives.
	if !strings.Contains(stderr, "HCC_AGENT_CONTROLLER_URL") {
		t.Errorf("usage should document the required variables, got: %q", stderr)
	}
}

func TestUnknownFlagExitsWithBadConfig(t *testing.T) {
	code, _, _ := captureRun(t, []string{"-not-a-real-flag"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
}

func TestMissingConfigurationExitsWithBadConfig(t *testing.T) {
	// Startup must fail with the configuration exit code and a readable
	// message rather than running in a broken state.
	for _, key := range []string{
		"HCC_AGENT_CONTROLLER_URL",
		"HCC_AGENT_TOKEN",
		"HCC_AGENT_TOKEN_FILE",
	} {
		// Setenv registers cleanup so the original value is restored, then
		// Unsetenv removes it for the duration of this test.
		t.Setenv(key, "")
		if err := os.Unsetenv(key); err != nil {
			t.Fatalf("unsetting %s: %v", key, err)
		}
	}

	code, _, stderr := captureRun(t, nil)

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "HCC_AGENT_CONTROLLER_URL") {
		t.Errorf("error should name the missing variable, got: %q", stderr)
	}
}

func TestNoFlagAcceptsTheEnrolmentToken(t *testing.T) {
	// Command lines are world-readable via /proc, so the token must not be a
	// flag. This test fails if someone adds one.
	_, _, stderr := captureRun(t, []string{"-h"})

	for _, forbidden := range []string{"-token ", "-token=", "-enrolment-token"} {
		if strings.Contains(stderr, forbidden) {
			t.Errorf("the token must not be settable via a flag, found %q in usage", forbidden)
		}
	}
}
