package install

import (
	"bytes"
	"context"
	"os"
	"strings"
	"testing"
)

// installedFixture is a fixture with a completed installation, for the
// commands that act on one.
func installedFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t)
	if err := f.run(); err != nil {
		t.Fatalf("the fixture install failed: %v\n%s", err, f.output.String())
	}
	f.output.Reset()
	// The install's own privileged calls are cleared, so a test asserting on
	// what the *next* command did is not reading the setup's record.
	f.system.mutex.Lock()
	f.system.calls = nil
	f.system.mutex.Unlock()
	return f
}

func (f *fixture) uninstaller(options UninstallOptions) *Uninstaller {
	f.t.Helper()
	if options.Out == nil {
		options.Out = f.output
	}
	return &Uninstaller{
		System:  f.system,
		Paths:   f.paths,
		Options: options,
		out:     options.Out,
	}
}

func TestUninstallRemovesTheServiceAndKeepsTheCredential(t *testing.T) {
	f := installedFixture(t)

	if err := f.uninstaller(UninstallOptions{}).Run(context.Background()); err != nil {
		t.Fatalf("uninstall failed: %v\n%s", err, f.output.String())
	}

	for _, gone := range []string{f.paths.Unit(), f.paths.Binary()} {
		if _, err := os.Stat(gone); err == nil {
			t.Errorf("%s survived the uninstall", gone)
		}
	}
	// The default keeps the credential. It is the only thing standing between
	// a reinstall and a re-enrolment, and somebody moving an agent to a new
	// disk has not asked to go back to the interface for a new one.
	if _, err := os.Stat(f.paths.Credential()); err != nil {
		t.Errorf("the credential was removed without --purge: %v", err)
	}
	if _, err := f.system.LookupUser(ServiceUser); err != nil {
		t.Error("the service account was removed without --purge")
	}

	calls := strings.Join(f.system.recorded(), "\n")
	if !strings.Contains(calls, "systemctl disable --now hcc-agent") {
		t.Errorf("the service was not stopped and disabled; calls:\n%s", calls)
	}
	if !strings.Contains(calls, "systemctl daemon-reload") {
		t.Errorf("systemd was not reloaded after the unit was removed; calls:\n%s", calls)
	}

	output := f.output.String()
	// The most important line in the output. Removing an agent does not
	// revoke its credential: that file is only a secret for as long as nobody
	// has a copy, and a decommissioned disk is a copy.
	if !strings.Contains(output, "Revoke this host's agent credential") {
		t.Errorf("the reminder to revoke is missing:\n%s", output)
	}
	// And the other one: an operator must not be left wondering whether
	// removing the agent threw away the history it collected.
	if !strings.Contains(output, "deletes no history") {
		t.Errorf("the output does not say controller-side data is untouched:\n%s", output)
	}
}

func TestUninstallPurgeRemovesTheCredentialAndTheAccount(t *testing.T) {
	f := installedFixture(t)

	if err := f.uninstaller(UninstallOptions{Purge: true}).Run(context.Background()); err != nil {
		t.Fatalf("uninstall --purge failed: %v\n%s", err, f.output.String())
	}

	for _, gone := range []string{
		f.paths.Unit(), f.paths.Binary(), f.paths.Credential(), f.paths.ConfigDir(),
	} {
		if _, err := os.Stat(gone); err == nil {
			t.Errorf("%s survived --purge", gone)
		}
	}
	if _, err := f.system.LookupUser(ServiceUser); err == nil {
		t.Error("the service account survived --purge")
	}
}

func TestUninstallDryRunRemovesNothing(t *testing.T) {
	f := installedFixture(t)

	if err := f.uninstaller(UninstallOptions{DryRun: true, Purge: true}).
		Run(context.Background()); err != nil {
		t.Fatalf("uninstall --dry-run failed: %v\n%s", err, f.output.String())
	}

	for _, present := range []string{
		f.paths.Unit(), f.paths.Binary(), f.paths.Credential(), f.paths.ConfigDir(),
	} {
		if _, err := os.Stat(present); err != nil {
			t.Errorf("the dry run removed %s", present)
		}
	}
	if len(f.system.recorded()) != 0 {
		t.Errorf("the dry run ran privileged operations: %v", f.system.recorded())
	}
	if !strings.Contains(f.output.String(), "Nothing above has been done") {
		t.Errorf("the dry run did not say it had changed nothing:\n%s", f.output.String())
	}
}

func TestUninstallOnAHostWithNothingInstalled(t *testing.T) {
	f := newFixture(t)
	output := &bytes.Buffer{}

	if err := f.uninstaller(UninstallOptions{Out: output}).Run(context.Background()); err != nil {
		t.Fatalf("uninstall on a clean host failed: %v\n%s", err, output.String())
	}
	if !strings.Contains(output.String(), "Nothing to remove") {
		t.Errorf("uninstall did not say there was nothing to do:\n%s", output.String())
	}
}

func TestUninstallRefusesWithoutRoot(t *testing.T) {
	f := installedFixture(t)
	f.system.euid = 1000

	err := f.uninstaller(UninstallOptions{}).Run(context.Background())
	if err == nil {
		t.Fatal("uninstall proceeded as an ordinary user")
	}
	if !strings.Contains(err.Error(), "needs root") {
		t.Errorf("the refusal does not mention root: %v", err)
	}
	if _, statErr := os.Stat(f.paths.Binary()); statErr != nil {
		t.Error("a refused uninstall removed the binary anyway")
	}
}

func TestUninstallNeverPrintsTheCredential(t *testing.T) {
	f := installedFixture(t)

	if err := f.uninstaller(UninstallOptions{Purge: true}).Run(context.Background()); err != nil {
		t.Fatalf("uninstall failed: %v", err)
	}
	if strings.Contains(f.output.String(), "AGENT-CREDENTIAL-VALUE") {
		t.Error("the uninstaller printed the credential it was removing")
	}
}
