package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// envSubprocess marks the re-executed child process. See
// TestGracefulShutdownOnSIGTERM.
const envSubprocess = "HCC_AGENT_TEST_SUBPROCESS"

// TestGracefulShutdownOnSIGTERM verifies the real signal path end to end.
//
// The agent's Run loop is unit-tested against a cancelled context elsewhere;
// this test covers what that cannot — that signal.NotifyContext in run() is
// actually wired to SIGTERM, so systemd stopping the unit produces a clean
// exit rather than a killed process.
//
// It uses the standard Go helper-process idiom: the test re-executes its own
// binary with a guard variable set, and the child behaves as the agent.
func TestGracefulShutdownOnSIGTERM(t *testing.T) {
	if os.Getenv(envSubprocess) == "1" {
		// Child: behave as the agent process.
		os.Exit(run([]string{"run"}, os.Stdout, os.Stderr))
	}

	credentialPath := filepath.Join(t.TempDir(), "credential")
	if err := os.WriteFile(credentialPath, []byte(strings.Repeat("c", 43)), 0o600); err != nil {
		t.Fatalf("writing credential file: %v", err)
	}

	cmd := exec.Command(os.Args[0], "-test.run=^TestGracefulShutdownOnSIGTERM$")
	cmd.Env = append(os.Environ(),
		envSubprocess+"=1",
		// A controller nothing is listening on: the agent will fail to connect
		// and enter its backoff, which is exactly the state a SIGTERM has to
		// interrupt. Testing shutdown from a *connected* agent would need a
		// real controller and would test less, because the interruptible wait
		// is the part that can go wrong.
		"HCC_AGENT_CONTROLLER_URL=https://127.0.0.1:1",
		"HCC_AGENT_CREDENTIAL_FILE="+credentialPath,
		// A long backoff, so the process is definitely inside the wait when
		// the signal arrives rather than between attempts.
		"HCC_AGENT_RECONNECT_MIN_WAIT=10m",
		"HCC_AGENT_RECONNECT_MAX_WAIT=10m",
		"HCC_AGENT_LOG_FORMAT=text",
	)

	// exec.Cmd writes from its own goroutines while waitForLog reads, so
	// this buffer must be synchronized (go test -race enforces it).
	var output syncBuffer
	cmd.Stdout = &output
	cmd.Stderr = &output

	if err := cmd.Start(); err != nil {
		t.Fatalf("starting subprocess: %v", err)
	}

	// Wait until the agent has installed its signal handler. Sending SIGTERM
	// before that would kill it with the default disposition and make this
	// test pass or fail for the wrong reason.
	// Waits for the *reconnect* line rather than "agent starting", because the
	// signal must arrive while the process is inside its interruptible wait.
	// Signalling earlier would test the default disposition instead.
	waitForLog(t, &output, "reconnecting", 10*time.Second)

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("sending SIGTERM: %v", err)
	}

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	select {
	case err := <-done:
		if err != nil {
			var exitErr *exec.ExitError
			if errors.As(err, &exitErr) {
				t.Fatalf("agent exited with %s (want a clean exit 0); output:\n%s",
					exitErr.ProcessState, output.String())
			}
			t.Fatalf("waiting for subprocess: %v; output:\n%s", err, output.String())
		}
	case <-time.After(15 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatalf("agent did not exit within 15s of SIGTERM; output:\n%s", output.String())
	}

	logs := output.String()
	for _, want := range []string{"shutting down", "shutdown complete"} {
		if !strings.Contains(logs, want) {
			t.Errorf("expected %q in the output, got:\n%s", want, logs)
		}
	}
}

// waitForLog blocks until substr appears in buf, or fails the test.
func waitForLog(t *testing.T, buf *syncBuffer, substr string, timeout time.Duration) {
	t.Helper()

	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(buf.String(), substr) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %q in output:\n%s", substr, buf.String())
}
