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
		os.Exit(run(nil, os.Stdout, os.Stderr))
	}

	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("test-token"), 0o600); err != nil {
		t.Fatalf("writing token file: %v", err)
	}

	cmd := exec.Command(os.Args[0], "-test.run=^TestGracefulShutdownOnSIGTERM$")
	cmd.Env = append(os.Environ(),
		envSubprocess+"=1",
		"HCC_AGENT_CONTROLLER_URL=https://hcc.internal:8000",
		"HCC_AGENT_TOKEN_FILE="+tokenPath,
		// Long interval: this test is about shutdown, not ticking.
		"HCC_AGENT_REPORT_INTERVAL=10m",
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
	waitForLog(t, &output, "agent starting", 10*time.Second)

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
	for _, want := range []string{"shutdown signal received", "shutdown complete"} {
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
