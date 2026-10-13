package install

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestSystemdAcceptsEveryDirectiveInTheUnit runs the real parser over the real
// unit.
//
// `systemd-analyze verify` is how the misplaced StartLimitIntervalSec in this
// file was originally found: systemd reads StartLimit* from [Unit] and ignores
// them in [Service] *without complaining*, so the restart limit the file
// appeared to set did nothing at all. A directive misspelt or in the wrong
// section is the same class of silent failure, and reading the file carefully
// is not a reliable way to catch it.
//
// # What this does not prove
//
// That the service works. `verify` parses and resolves; it does not execute,
// does not apply a sandbox, and cannot tell that ProcSubset=pid would hide the
// files the agent reads. The installation matrix in CI starts the service for
// real and asserts that a sample arrives, which is the test that proves
// behaviour. This one proves the file is well-formed, cheaply, on every run.
//
// Skipped when systemd-analyze is absent, which is the case on a runner
// without systemd — stated as a skip rather than quietly passing.
func TestSystemdAcceptsEveryDirectiveInTheUnit(t *testing.T) {
	analyze, err := exec.LookPath("systemd-analyze")
	if err != nil {
		t.Skip("systemd-analyze is not installed; the installation matrix covers this for real")
	}

	// ExecStart is substituted, and only ExecStart. `verify` checks that the
	// command exists and is executable, and /usr/local/bin/hcc-agent is not
	// installed on the machine running the test — so without this the test
	// would fail for the one reason that says nothing about the file. Every
	// other directive is verified exactly as written.
	substituted := bytes.Replace(
		UnitFile(),
		[]byte("ExecStart="+DefaultBinaryPath+" run"),
		[]byte("ExecStart=/bin/true"),
		1,
	)
	if bytes.Equal(substituted, UnitFile()) {
		t.Fatalf("the unit's ExecStart is not %q any more; this test needs updating",
			"ExecStart="+DefaultBinaryPath+" run")
	}

	path := filepath.Join(t.TempDir(), ServiceName+".service")
	if err := os.WriteFile(path, substituted, 0o644); err != nil {
		t.Fatalf("cannot write the unit: %v", err)
	}

	output, err := exec.Command(analyze, "verify", path).CombinedOutput() //nolint:gosec // a resolved path
	if err != nil {
		t.Fatalf("systemd-analyze verify rejected the unit: %v\n%s", err, output)
	}
	// Non-fatal complaints still arrive on stdout with a zero exit, so they
	// are surfaced rather than discarded: a warning about a deprecated
	// directive is worth seeing before it becomes an error.
	if trimmed := strings.TrimSpace(string(output)); trimmed != "" {
		t.Logf("systemd-analyze had something to say:\n%s", trimmed)
	}
}

// TestTheUnitIsTheOnlyCopyInTheRepository guards the decision that there is
// one unit file and the installer owns it.
//
// The file used to live under agents/linux/deploy/ and be pasted by hand. Two
// copies — one embedded in the binary, one documented for humans — is two
// copies that drift, and the one people read would be the stale one. So the
// embedded file is the only one, `install --dry-run` prints it, and the deploy
// documentation points at it rather than reproducing it.
func TestTheUnitIsTheOnlyCopyInTheRepository(t *testing.T) {
	// Relative to this package's directory, which is where `go test` runs.
	for _, stale := range []string{
		"../../deploy/hcc-agent.service",
		"../../../../deploy/hcc-agent.service",
	} {
		if _, err := os.Stat(stale); err == nil {
			t.Errorf(
				"%s exists; the unit is embedded in this package and must not be "+
					"duplicated — a second copy is a copy that drifts", stale,
			)
		}
	}
}

func TestTheUnitReferencesTheInstallersOwnPaths(t *testing.T) {
	unit := string(UnitFile())

	// Every path the unit names has to be a path the installer actually
	// creates. A unit referencing /opt/hcc-agent would parse, verify, and
	// fail to start on a host the installer had just set up correctly.
	for _, expected := range []string{
		"ExecStart=" + DefaultBinaryPath + " run",
		"EnvironmentFile=" + DefaultEnvFile,
		"ReadWritePaths=" + DefaultConfigDir,
		"User=" + ServiceUser,
		"Group=" + ServiceUser,
		"SyslogIdentifier=" + ServiceName,
	} {
		if !strings.Contains(unit, expected) {
			t.Errorf("the unit does not contain %q", expected)
		}
	}
}
