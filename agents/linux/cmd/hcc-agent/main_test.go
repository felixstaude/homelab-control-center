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

// clearAgentEnv removes every HCC_AGENT_* variable for one test.
//
// Needed because these tests exercise the real configuration path, and a
// developer's shell — or an earlier test in the same process — may have set
// one. t.Setenv registers the cleanup that restores it.
func clearAgentEnv(t *testing.T) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, found := strings.Cut(entry, "=")
		if !found || !strings.HasPrefix(name, "HCC_AGENT_") {
			continue
		}
		t.Setenv(name, "")
		if err := os.Unsetenv(name); err != nil {
			t.Fatalf("unsetting %s: %v", name, err)
		}
	}
}

func TestVersionSubcommandPrintsVersionAndExitsZero(t *testing.T) {
	code, stdout, _ := captureRun(t, []string{"version"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
	if !strings.Contains(stdout, "hcc-agent") {
		t.Errorf("expected version output on stdout, got: %q", stdout)
	}
	// `version` must not require a valid configuration, so an operator can
	// check what they installed before configuring it.
	if strings.Contains(stdout, "invalid configuration") {
		t.Error("`version` must not require configuration")
	}
}

func TestTheVersionFlagStillWorks(t *testing.T) {
	// Kept alongside the subcommand because it is what packaging tools and
	// humans habitually try, and answering it is cheaper than explaining that
	// it moved.
	for _, flag := range []string{"-version", "--version"} {
		code, stdout, _ := captureRun(t, []string{flag})
		if code != exitOK {
			t.Errorf("%s: exit code = %d, want %d", flag, code, exitOK)
		}
		if !strings.Contains(stdout, "hcc-agent") {
			t.Errorf("%s: expected version output, got: %q", flag, stdout)
		}
	}
}

func TestHelpGoesToStdoutAndExitsZero(t *testing.T) {
	// Stdout, because an explicit request for help is not an error — so
	// `hcc-agent -h | less` works.
	code, stdout, _ := captureRun(t, []string{"-h"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d for -h", code, exitOK)
	}
	if !strings.Contains(stdout, "Usage") {
		t.Errorf("expected usage text on stdout, got: %q", stdout)
	}
	if !strings.Contains(stdout, "HCC_AGENT_CONTROLLER_URL") {
		t.Errorf("usage should document the required variables, got: %q", stdout)
	}
	for _, command := range []string{"enroll", "run", "status", "version"} {
		if !strings.Contains(stdout, command) {
			t.Errorf("usage should list the %q command, got: %q", command, stdout)
		}
	}
}

func TestNoArgumentsPrintsUsageAndExitsWithBadConfig(t *testing.T) {
	// A bare invocation used to start the agent. It no longer does, because
	// `run` and `enroll` are different operations with different privileges —
	// and silently picking one would be the wrong guess half the time.
	code, _, stderr := captureRun(t, nil)

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "Usage") {
		t.Errorf("expected usage on stderr, got: %q", stderr)
	}
}

func TestAnUnknownCommandIsNamedInTheError(t *testing.T) {
	code, _, stderr := captureRun(t, []string{"strat"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "strat") {
		t.Errorf("the error should name the command, got: %q", stderr)
	}
}

func TestAnUnknownFlagExitsWithBadConfig(t *testing.T) {
	code, _, _ := captureRun(t, []string{"run", "-not-a-real-flag"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
}

func TestMissingConfigurationExitsWithBadConfig(t *testing.T) {
	// Startup must fail with the configuration exit code and a readable
	// message rather than running in a broken state.
	clearAgentEnv(t)

	code, _, stderr := captureRun(t, []string{"run"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "HCC_AGENT_CONTROLLER_URL") {
		t.Errorf("the error should name the missing variable, got: %q", stderr)
	}
}

func TestNoSecretIsSettableViaAFlag(t *testing.T) {
	// Command lines are readable by every local user through /proc, so a
	// secret must not be a flag. This test fails if somebody adds one.
	_, stdout, _ := captureRun(t, []string{"-h"})

	for _, forbidden := range []string{
		"-token", "-credential ", "-credential=", "-secret", "-enrollment-token",
	} {
		if strings.Contains(stdout, forbidden) {
			t.Errorf("no secret may be settable via a flag, found %q in usage", forbidden)
		}
	}
}

func TestThereIsNoFlagThatSkipsTLSVerification(t *testing.T) {
	// The usage text is the inventory an operator reads, so the absence is
	// asserted there as well as in the configuration package.
	_, stdout, _ := captureRun(t, []string{"-h"})

	for _, forbidden := range []string{"-insecure", "-k ", "-skip-verify", "-no-verify"} {
		if strings.Contains(stdout, forbidden) {
			t.Errorf("found a verification-skipping flag %q in usage", forbidden)
		}
	}
	if !strings.Contains(stdout, "no option to skip TLS verification") {
		t.Errorf("usage should state the policy, got: %q", stdout)
	}
}

func TestStatusReportsAnUnenrolledHostWithAnInstruction(t *testing.T) {
	clearAgentEnv(t)
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", filepath.Join(t.TempDir(), "absent"))

	code, stdout, _ := captureRun(t, []string{"status"})

	if code != exitNotEnrolled {
		t.Errorf("exit code = %d, want %d", code, exitNotEnrolled)
	}
	if !strings.Contains(stdout, "Enrolled:        no") {
		t.Errorf("status should say so plainly, got: %q", stdout)
	}
	if !strings.Contains(stdout, "hcc-agent enroll") {
		t.Errorf("status should name the command to run, got: %q", stdout)
	}
}

func TestStatusReportsAnEnrolledHost(t *testing.T) {
	clearAgentEnv(t)
	path := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(path, []byte(strings.Repeat("c", 43)), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", path)

	code, stdout, _ := captureRun(t, []string{"status"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
	if !strings.Contains(stdout, "Enrolled:        yes") {
		t.Errorf("status should report the host as enrolled, got: %q", stdout)
	}
}

func TestStatusNeverPrintsTheCredential(t *testing.T) {
	// A command that printed it would put the secret in shell history and in
	// whatever it was piped to. There is deliberately no flag to ask for it.
	clearAgentEnv(t)
	secret := "SECRET-CREDENTIAL-VALUE-9f2c"
	path := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(path, []byte(secret), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", path)

	_, stdout, stderr := captureRun(t, []string{"status"})

	if strings.Contains(stdout+stderr, secret) {
		t.Errorf("status printed the credential: %q", stdout+stderr)
	}
}

func TestStatusReportsAnInsecureCredentialFileRatherThanUsingIt(t *testing.T) {
	// The first question on a host that is not appearing is "is it enrolled?",
	// and "yes, but the file is world-readable" is a different and more urgent
	// answer than "no".
	clearAgentEnv(t)
	path := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(path, []byte(strings.Repeat("c", 43)), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", path)

	code, _, stderr := captureRun(t, []string{"status"})

	if code != exitRuntimeError {
		t.Errorf("exit code = %d, want %d", code, exitRuntimeError)
	}
	if !strings.Contains(stderr, "chmod 600") {
		t.Errorf("the error should name the fix, got: %q", stderr)
	}
}

func TestEnrollRefusesToReplaceAnExistingCredentialWithoutForce(t *testing.T) {
	// Re-enrolling revokes the credential the running agent is using, and the
	// one-time credential would be spent — so the mistake would not even be
	// undoable without issuing another.
	clearAgentEnv(t)
	path := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(path, []byte(strings.Repeat("c", 43)), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", path)
	t.Setenv("HCC_AGENT_ENROLLMENT_TOKEN", strings.Repeat("e", 43))

	code, _, stderr := captureRun(t, []string{"enroll"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "-force") {
		t.Errorf("the error should name the override, got: %q", stderr)
	}
}

func TestStatusReportsAnUnusableControllerURL(t *testing.T) {
	// `status` is the command an operator runs to diagnose "why is this host
	// not appearing", so it has to be able to see the most common
	// misconfiguration. Before this check it reported "Enrolled: yes" for a
	// host whose plaintext controller URL the agent would refuse on every
	// connection attempt — found by the browser security suite, which is the
	// only place the two halves were exercised together.
	clearAgentEnv(t)
	path := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(path, []byte(strings.Repeat("c", 43)), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "http://hcc.example.com")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", path)

	code, stdout, stderr := captureRun(t, []string{"status"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stdout, "UNUSABLE") {
		t.Errorf("status should say the URL is unusable, got: %q", stdout)
	}
	if !strings.Contains(stderr, "in the clear") {
		t.Errorf("the error should explain why, got: %q", stderr)
	}
	// And it must not go on to report the host as fine.
	if strings.Contains(stdout, "Enrolled:        yes") {
		t.Errorf("status reported a working host behind an unusable URL: %q", stdout)
	}
}

func TestEnrollRefusesAPlaintextControllerURL(t *testing.T) {
	// Plain HTTP would send this host's credential in the clear, and the
	// refusal happens before the credential is spent.
	clearAgentEnv(t)
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "http://hcc.example.com")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", filepath.Join(t.TempDir(), "credential"))
	t.Setenv("HCC_AGENT_ENROLLMENT_TOKEN", strings.Repeat("e", 43))

	code, _, stderr := captureRun(t, []string{"enroll"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "in the clear") {
		t.Errorf("the error should explain why, got: %q", stderr)
	}
}

func TestEnrollReportsAMissingEnrollmentCredential(t *testing.T) {
	clearAgentEnv(t)
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.internal:8000")
	t.Setenv("HCC_AGENT_CREDENTIAL_FILE", filepath.Join(t.TempDir(), "credential"))

	code, _, stderr := captureRun(t, []string{"enroll"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "HCC_AGENT_ENROLLMENT_TOKEN") {
		t.Errorf("the error should name the variable, got: %q", stderr)
	}
}
