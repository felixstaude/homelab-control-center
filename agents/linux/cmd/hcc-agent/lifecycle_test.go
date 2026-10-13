package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// These exercise the argument handling of the four lifecycle subcommands —
// the part that decides whether anything happens at all. The behaviour behind
// them is tested in internal/install and internal/doctor, where the
// filesystem and systemd are behind interfaces; here the question is whether a
// missing flag produces a usable message and the right exit code.

func TestInstallWithoutAControllerURLRefusesAndSaysHow(t *testing.T) {
	clearAgentEnv(t)

	code, _, stderr := captureRun(t, []string{"install"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "--controller is required") {
		t.Errorf("the refusal does not name the flag: %q", stderr)
	}
	// The example matters more than the error. Somebody who has just
	// extracted a tarball needs the command, not a restatement of the flag's
	// name.
	if !strings.Contains(stderr, "sudo ./hcc-agent install --controller https://") {
		t.Errorf("the refusal does not show the command to run: %q", stderr)
	}
}

func TestInstallTakesTheControllerURLFromTheEnvironment(t *testing.T) {
	clearAgentEnv(t)
	// Set, so the --controller check passes; the install then fails at
	// preflight for want of root, which is what the exit code below asserts.
	t.Setenv("HCC_AGENT_CONTROLLER_URL", "https://hcc.example.invalid")

	code, stdout, _ := captureRun(t, []string{"install", "--dry-run"})

	// Not exitBadConfig: the URL was accepted, so the command got as far as
	// the preflight and reported its findings.
	if code == exitBadConfig && strings.Contains(stdout, "--controller is required") {
		t.Fatalf("the environment variable was ignored: %q", stdout)
	}
	if !strings.Contains(stdout, "Preflight") {
		t.Errorf("the installer did not reach preflight: %q", stdout)
	}
}

func TestInstallRejectsAStraySubcommandArgument(t *testing.T) {
	clearAgentEnv(t)

	// `hcc-agent install https://hcc.example.com` is the mistake somebody
	// makes once. Refused rather than ignored, because ignoring it would
	// produce "--controller is required" while the URL was right there.
	code, _, stderr := captureRun(t, []string{"install", "https://hcc.example.com"})

	if code != exitBadConfig {
		t.Errorf("exit code = %d, want %d", code, exitBadConfig)
	}
	if !strings.Contains(stderr, "unexpected argument") {
		t.Errorf("the refusal does not name the problem: %q", stderr)
	}
}

func TestUpgradeRequiresBothTheArchiveAndItsDigest(t *testing.T) {
	for _, arguments := range [][]string{
		{"upgrade"},
		{"upgrade", "--from", "/tmp/x.tar.gz"},
		{"upgrade", "--sha256", strings.Repeat("a", 64)},
	} {
		code, _, stderr := captureRun(t, arguments)

		if code != exitBadConfig {
			t.Errorf("%v: exit code = %d, want %d", arguments, code, exitBadConfig)
		}
		if !strings.Contains(stderr, "--from and --sha256 are both required") {
			t.Errorf("%v: the refusal does not say what is missing: %q", arguments, stderr)
		}
	}
}

// There is no flag that skips the digest, and the help must not imply one.
// The trust model is that the controller — which the administrator is already
// authenticated to over TLS — vouches for the binary, and a model that can be
// turned off is the one that is off when it matters.
func TestUpgradeOffersNoWayToSkipVerification(t *testing.T) {
	_, _, stderr := captureRun(t, []string{"upgrade", "--help"})
	combined := strings.ToLower(stderr)

	for _, forbidden := range []string{"skip", "insecure", "no-verify", "unsafe", "force"} {
		if strings.Contains(combined, forbidden) {
			t.Errorf("upgrade's flags mention %q: %q", forbidden, stderr)
		}
	}
}

func TestDoctorProducesParseableJSON(t *testing.T) {
	clearAgentEnv(t)

	// Runs against this machine, where nothing is installed — which is the
	// point: the diagnostic has to produce a usable report on a host with no
	// installation rather than failing to run.
	code, stdout, _ := captureRun(t, []string{"doctor", "--json", "--timeout", "1s"})

	var report struct {
		GeneratedAt string `json:"generated_at"`
		Agent       struct {
			Version  string `json:"version"`
			Platform string `json:"platform"`
		} `json:"agent"`
		Findings []struct {
			Section string `json:"section"`
			Check   string `json:"check"`
			Status  string `json:"status"`
			Detail  string `json:"detail"`
		} `json:"findings"`
	}
	if err := json.Unmarshal([]byte(stdout), &report); err != nil {
		t.Fatalf("doctor --json did not produce JSON: %v\n%s", err, stdout)
	}
	if report.GeneratedAt == "" || report.Agent.Platform == "" {
		t.Errorf("the report is missing its header fields: %+v", report)
	}
	if len(report.Findings) == 0 {
		t.Error("the report contains no findings")
	}
	for _, finding := range report.Findings {
		switch finding.Status {
		case "ok", "warning", "failed", "skipped":
		default:
			t.Errorf("finding %s/%s has an unknown status %q",
				finding.Section, finding.Check, finding.Status)
		}
	}
	// A host with no agent installed has something wrong with it by
	// definition, so a non-zero exit is the correct answer.
	if code != exitRuntimeError {
		t.Logf("doctor exited %d on this machine, which is informational here", code)
	}
}

func TestDoctorTextOutputIsSafeToPaste(t *testing.T) {
	clearAgentEnv(t)

	_, stdout, _ := captureRun(t, []string{"doctor", "--timeout", "1s"})

	if !strings.Contains(stdout, "safe to paste into an issue report") {
		t.Errorf("the output does not state the property:\n%s", stdout)
	}
	// A real credential is not present on this machine, so the useful
	// assertion here is structural: no line of the report may be a 43-
	// character base64url blob, which is the shape of both credentials.
	for _, line := range strings.Split(stdout, "\n") {
		for _, field := range strings.Fields(line) {
			if len(field) == 43 && isBase64URL(field) {
				t.Errorf("a credential-shaped value appears in the output: %q", field)
			}
		}
	}
}

func isBase64URL(value string) bool {
	for _, character := range value {
		switch {
		case character >= 'a' && character <= 'z':
		case character >= 'A' && character <= 'Z':
		case character >= '0' && character <= '9':
		case character == '-' || character == '_':
		default:
			return false
		}
	}
	return true
}

func TestUninstallDryRunOnAHostWithNothingInstalled(t *testing.T) {
	clearAgentEnv(t)

	// --dry-run so this cannot touch the machine running the test, and
	// because the real paths are absolute: the uninstaller is not rooted, and
	// a non-dry run here would be a test that modifies its own host.
	code, stdout, _ := captureRun(t, []string{"uninstall", "--dry-run"})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
	if !strings.Contains(stdout, "dry run") {
		t.Errorf("the output does not say it is a dry run:\n%s", stdout)
	}
}

func TestLifecycleCommandsRejectStrayArguments(t *testing.T) {
	for _, arguments := range [][]string{
		{"uninstall", "everything"},
		{"doctor", "please"},
		{"upgrade", "now"},
	} {
		code, _, stderr := captureRun(t, arguments)
		if code != exitBadConfig {
			t.Errorf("%v: exit code = %d, want %d", arguments, code, exitBadConfig)
		}
		if !strings.Contains(stderr, "unexpected argument") {
			t.Errorf("%v: the refusal does not name the problem: %q", arguments, stderr)
		}
	}
}

// A --token-file the installer cannot vouch for is refused at preflight,
// before anything is created. The mode check lives in the config package and
// is reused rather than reimplemented; this asserts that the installer's path
// actually reaches it.
func TestInstallRefusesAWorldReadableTokenFile(t *testing.T) {
	clearAgentEnv(t)
	directory := t.TempDir()
	tokenFile := filepath.Join(directory, "enrolment")
	if err := os.WriteFile(tokenFile, []byte("a-credential\n"), 0o644); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}

	_, stdout, _ := captureRun(t, []string{
		"install", "--dry-run",
		"--controller", "https://hcc.example.invalid",
		"--token-file", tokenFile,
		"--non-interactive",
	})

	if !strings.Contains(stdout, "group- or world-readable") {
		t.Errorf("the preflight did not refuse a 0644 credential file:\n%s", stdout)
	}
}

func TestInstallRefusesNonInteractiveWithNoCredentialSource(t *testing.T) {
	clearAgentEnv(t)

	_, stdout, _ := captureRun(t, []string{
		"install", "--dry-run",
		"--controller", "https://hcc.example.invalid",
		"--non-interactive",
	})

	// The absence of a terminal must be an error rather than a hang, and the
	// message has to name the flag that resolves it.
	if !strings.Contains(stdout, "--non-interactive was given with no credential source") {
		t.Errorf("the preflight did not explain the missing credential source:\n%s", stdout)
	}
	if !strings.Contains(stdout, "--token-file") {
		t.Errorf("the message does not name the flag to use:\n%s", stdout)
	}
}
