package doctor

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// theCredential is the value that must never appear in any output. A single
// constant, so the leak tests cannot drift from the fixture.
const theCredential = "THE-AGENT-CREDENTIAL-WHICH-MUST-NEVER-BE-PRINTED"

type fixture struct {
	t       *testing.T
	root    string
	paths   install.Paths
	options Options
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	root := t.TempDir()

	for _, directory := range []string{"/usr/local/bin", "/etc/systemd/system", "/proc", "/etc"} {
		if err := os.MkdirAll(filepath.Join(root, directory), 0o755); err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}
	for name, contents := range map[string]string{
		"stat":    "cpu  1 2 3 4 5 6 7 0 0 0\n",
		"meminfo": "MemTotal: 1 kB\n",
		"loadavg": "0.0 0.0 0.0 1/1 1\n",
		"uptime":  "1.0 1.0\n",
	} {
		if err := os.WriteFile(filepath.Join(root, "proc", name), []byte(contents), 0o644); err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}

	// The account the service would run as. A test cannot create a file owned
	// by hcc-agent, so the fixture declares that the service runs as whoever
	// is running the test — which is what makes the ownership checks
	// exercisable at all rather than permanently warning.
	self, err := user.Current()
	if err != nil {
		t.Fatalf("cannot determine the current user: %v", err)
	}

	paths := install.Paths{Root: root}
	f := &fixture{t: t, root: root, paths: paths}
	f.options = Options{
		Timeout:        time.Second,
		ServiceAccount: self.Username,
		Paths:          paths,
		ProcRoot:       filepath.Join(root, "proc"),
		LookupEnv:      func(string) (string, bool) { return "", false },
		Now:            func() time.Time { return time.Unix(1760000000, 0) },
		Probe: func(context.Context, string, string) (*controller.ProbeResult, error) {
			return &controller.ProbeResult{
				TLS:        true,
				Status:     200,
				ServerTime: time.Unix(1760000000, 0),
				RoundTrip:  10 * time.Millisecond,
			}, nil
		},
		Resolve: func(_ context.Context, host string) ([]string, error) {
			return []string{"192.0.2.10"}, nil
		},
		Dial: func(context.Context, string) error { return nil },
		Systemctl: func(context.Context, ...string) (string, error) {
			return "LoadState=loaded\nActiveState=active\nSubState=running\n" +
				"UnitFileState=enabled\nNRestarts=0\nResult=success\n", nil
		},
	}
	return f
}

// installed builds a complete, healthy installation.
func (f *fixture) installed() *fixture {
	f.t.Helper()
	f.writeBinary()
	f.writeConfigDir()
	f.writeCredential()
	f.writeEnvFile("https://hcc.example.com")
	f.writeUnit()
	f.writeConfiguration(1, nil)
	return f
}

func (f *fixture) writeBinary() {
	f.t.Helper()
	write(f.t, f.paths.Binary(), []byte("\x7fELF fake"), 0o755)
}

func (f *fixture) writeConfigDir() {
	f.t.Helper()
	if err := os.MkdirAll(f.paths.ConfigDir(), 0o750); err != nil {
		f.t.Fatalf("cannot create the configuration directory: %v", err)
	}
	if err := os.Chmod(f.paths.ConfigDir(), 0o750); err != nil {
		f.t.Fatalf("cannot set the configuration directory's mode: %v", err)
	}
}

func (f *fixture) writeCredential() {
	f.t.Helper()
	if err := credentials.Save(f.paths.Credential(), theCredential); err != nil {
		f.t.Fatalf("cannot write the credential: %v", err)
	}
}

func (f *fixture) writeEnvFile(controllerURL string) {
	f.t.Helper()
	write(f.t, f.paths.EnvFile(), install.EnvironmentFile(controllerURL, ""), 0o644)
}

func (f *fixture) writeUnit() {
	f.t.Helper()
	write(f.t, f.paths.Unit(), install.UnitFile(), 0o644)
}

func (f *fixture) writeConfiguration(version int64, enabled []string) {
	f.t.Helper()
	modules := make(map[string]monitoring.ModuleConfig)
	for _, id := range monitoring.ModuleIDs() {
		modules[id] = monitoring.ModuleConfig{Enabled: false}
	}
	for _, id := range enabled {
		modules[id] = monitoring.ModuleConfig{
			Enabled: true,
			Schedule: monitoring.Schedule{
				Kind:            monitoring.KindInterval,
				IntervalSeconds: 60,
			},
		}
	}
	encoded, err := json.Marshal(monitoring.Document{
		DocumentVersion: monitoring.DocumentVersion,
		RegistryVersion: monitoring.RegistryVersion,
		HostID:          "3f2c8a14-0000-0000-0000-000000000000",
		Version:         version,
		GeneratedAt:     "2026-10-13T00:00:00Z",
		Modules:         modules,
		Limits: monitoring.Limits{
			MinIntervalSeconds: monitoring.MinIntervalSeconds,
			MaxIntervalSeconds: monitoring.MaxIntervalSeconds,
		},
	})
	if err != nil {
		f.t.Fatalf("cannot encode the configuration: %v", err)
	}
	write(f.t, f.paths.Configuration(), encoded, 0o600)
	if _, err := monitoring.Load(f.paths.Configuration()); err != nil {
		f.t.Fatalf("the fixture configuration is not loadable: %v", err)
	}
}

func write(t *testing.T, path string, contents []byte, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("cannot prepare %s: %v", path, err)
	}
	if err := os.WriteFile(path, contents, mode); err != nil {
		t.Fatalf("cannot write %s: %v", path, err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatalf("cannot set the mode on %s: %v", path, err)
	}
}

func (f *fixture) run() Report {
	f.t.Helper()
	return Run(context.Background(), f.options)
}

// finding returns the first finding with the given check label.
func finding(t *testing.T, report Report, section, check string) Finding {
	t.Helper()
	for _, item := range report.Findings {
		if item.Section == section && item.Check == check {
			return item
		}
	}
	var labels []string
	for _, item := range report.Findings {
		labels = append(labels, item.Section+"/"+item.Check)
	}
	t.Fatalf("no finding for %s/%s; the report has: %s", section, check, strings.Join(labels, ", "))
	return Finding{}
}

// --- The leak guarantee -----------------------------------------------------

// Doctor's output is meant to be pasted into an issue report. That means
// "contains no secret" has to be a property of the program rather than a habit
// of whoever wrote the last check, so it is a test — run against both output
// formats, against a healthy installation and a broken one, because the error
// paths are where a value gets printed by accident.
func TestOutputNeverContainsTheCredential(t *testing.T) {
	for _, scenario := range []struct {
		name  string
		setup func(f *fixture)
	}{
		{"healthy", func(f *fixture) { f.installed() }},
		{
			"world-readable credential",
			func(f *fixture) {
				f.installed()
				if err := os.Chmod(f.paths.Credential(), 0o644); err != nil {
					f.t.Fatalf("cannot loosen the credential: %v", err)
				}
			},
		},
		{
			"unreachable controller",
			func(f *fixture) {
				f.installed()
				f.options.Probe = func(context.Context, string, string) (*controller.ProbeResult, error) {
					return nil, fmt.Errorf("x509: certificate signed by unknown authority")
				}
			},
		},
		{
			"corrupt configuration",
			func(f *fixture) {
				f.installed()
				write(f.t, f.paths.Configuration(), []byte("{not json"), 0o600)
			},
		},
		{
			"credential in the environment too",
			func(f *fixture) {
				f.installed()
				// The worst case: somebody has put a credential in the
				// environment doctor is reading, which is exactly how a
				// diagnostic ends up echoing one.
				f.options.LookupEnv = func(name string) (string, bool) {
					if name == "HCC_AGENT_ENROLLMENT_TOKEN" {
						return theCredential, true
					}
					return "", false
				}
			},
		},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			f := newFixture(t)
			scenario.setup(f)
			report := f.run()

			var text bytes.Buffer
			report.WriteText(&text)
			if strings.Contains(text.String(), theCredential) {
				t.Errorf("the text output contains the credential:\n%s", text.String())
			}

			var encoded bytes.Buffer
			if err := report.WriteJSON(&encoded); err != nil {
				t.Fatalf("cannot encode the report: %v", err)
			}
			if strings.Contains(encoded.String(), theCredential) {
				t.Errorf("the JSON output contains the credential:\n%s", encoded.String())
			}
		})
	}
}

// --- A healthy installation -------------------------------------------------

func TestHealthyInstallationPassesEverythingThatMatters(t *testing.T) {
	f := newFixture(t).installed()
	report := f.run()

	if report.Failed() {
		var text bytes.Buffer
		report.WriteText(&text)
		t.Fatalf("a healthy installation reported a failure:\n%s", text.String())
	}

	for _, expectation := range []struct{ section, check string }{
		{sectionHost, "/proc"},
		{sectionInstallation, "binary"},
		{sectionInstallation, "configuration directory"},
		{sectionCredential, "enrolled"},
		{sectionService, "state"},
		{sectionController, "tls"},
		{sectionCollection, "configuration"},
	} {
		item := finding(t, report, expectation.section, expectation.check)
		if item.Status != StatusOK {
			t.Errorf("%s/%s is %s: %s",
				expectation.section, expectation.check, item.Status, item.Detail)
		}
	}
}

func TestNoModulesEnabledIsAWarningRatherThanAFailure(t *testing.T) {
	f := newFixture(t).installed()
	report := f.run()

	modules := finding(t, report, sectionCollection, "modules")
	// The default state of a freshly enrolled host. Reporting it as a failure
	// would train people to ignore failures.
	if modules.Status != StatusWarning {
		t.Errorf("modules is %s, want a warning: %s", modules.Status, modules.Detail)
	}
	if !strings.Contains(modules.Fix, "every module is off until an administrator enables it") {
		t.Errorf("the explanation does not say this is the default: %q", modules.Fix)
	}
	if report.Failed() {
		t.Error("a host with nothing enabled is reported as broken")
	}
}

func TestEnabledModulesAreListed(t *testing.T) {
	f := newFixture(t).installed()
	f.writeConfiguration(3, []string{"cpu", "memory"})
	report := f.run()

	modules := finding(t, report, sectionCollection, "modules")
	if modules.Status != StatusOK {
		t.Fatalf("modules is %s: %s", modules.Status, modules.Detail)
	}
	for _, id := range []string{"cpu", "memory"} {
		if !strings.Contains(modules.Detail, id) {
			t.Errorf("the enabled module %q is not listed: %q", id, modules.Detail)
		}
	}
}

// --- The failures it exists to find -----------------------------------------

func TestARootOwnedConfigurationDirectoryIsAFailure(t *testing.T) {
	// This is the defect that shipped once: the agent connects, is sent a
	// configuration, cannot write it, correctly never acknowledges, and the
	// interface shows the host stuck on "saved, not yet confirmed" while
	// nothing is collected. A diagnostic that did not catch it would be
	// missing the one thing it was written for.
	//
	// Simulated by ownership rather than performed: a test cannot chown to
	// root. The check reads the owner's *name*, so a fixture whose owner
	// resolves to something other than the service account exercises the same
	// branch — the root case is asserted on the message, below.
	f := newFixture(t).installed()
	report := f.run()

	directory := finding(t, report, sectionInstallation, "configuration directory")
	// Running as an ordinary user, the directory is owned by that user rather
	// than by hcc-agent — which is the "owned by somebody else" branch.
	if directory.Status == StatusOK {
		t.Skip("this test only means something when the fixture's owner is not hcc-agent")
	}
	if !strings.Contains(directory.Fix, "chown") {
		t.Errorf("the finding does not say how to fix it: %q", directory.Fix)
	}
}

func TestAWorldWritableConfigurationDirectoryIsAFailure(t *testing.T) {
	f := newFixture(t).installed()
	if err := os.Chmod(f.paths.ConfigDir(), 0o777); err != nil {
		t.Fatalf("cannot loosen the directory: %v", err)
	}

	report := f.run()
	directory := finding(t, report, sectionInstallation, "configuration directory")
	if directory.Status != StatusFailed {
		t.Errorf("a world-writable configuration directory is %s: %s",
			directory.Status, directory.Detail)
	}
	if !report.Failed() {
		t.Error("the report does not fail overall")
	}
}

func TestAWorldReadableCredentialIsAFailureWithTheChmodToRun(t *testing.T) {
	f := newFixture(t).installed()
	if err := os.Chmod(f.paths.Credential(), 0o644); err != nil {
		t.Fatalf("cannot loosen the credential: %v", err)
	}

	report := f.run()
	enrolled := finding(t, report, sectionCredential, "enrolled")
	if enrolled.Status != StatusFailed {
		t.Fatalf("a world-readable credential is %s: %s", enrolled.Status, enrolled.Detail)
	}
	// The diagnostic uses the agent's own loader, so what it reports is
	// exactly what the service would refuse — and the fix it prints is the
	// one the loader itself suggests.
	if !strings.Contains(enrolled.Fix, "chmod 600") {
		t.Errorf("the fix is not the chmod to run: %q", enrolled.Fix)
	}
}

func TestPlainHTTPToARemoteHostIsAFailure(t *testing.T) {
	f := newFixture(t).installed()
	f.writeEnvFile("http://hcc.example.com")

	report := f.run()
	url := finding(t, report, sectionConfig, "controller URL")
	if url.Status != StatusFailed {
		t.Fatalf("plain HTTP to a remote host is %s: %s", url.Status, url.Detail)
	}
	if !strings.Contains(url.Fix, "no option to skip verification") {
		t.Errorf("the fix does not say verification cannot be skipped: %q", url.Fix)
	}
	// And the connectivity section must not then try the URL the agent itself
	// would refuse — reporting "reachable" for an endpoint the service will
	// never use would be worse than saying nothing.
	reachability := finding(t, report, sectionController, "reachability")
	if reachability.Status != StatusSkipped {
		t.Errorf("connectivity was tested against an unusable URL: %s", reachability.Detail)
	}
}

func TestPlainHTTPToLoopbackIsOnlyAWarning(t *testing.T) {
	f := newFixture(t).installed()
	f.writeEnvFile("http://127.0.0.1:8000")

	report := f.run()
	url := finding(t, report, sectionConfig, "controller URL")
	if url.Status != StatusWarning {
		t.Errorf("plain HTTP to loopback is %s, want a warning: %s", url.Status, url.Detail)
	}
}

func TestAnUntrustedCertificateNamesTheRemedy(t *testing.T) {
	f := newFixture(t).installed()
	f.options.Probe = func(context.Context, string, string) (*controller.ProbeResult, error) {
		return nil, fmt.Errorf("Get \"https://hcc.example.com/healthz\": tls: " +
			"failed to verify certificate: x509: certificate signed by unknown authority")
	}

	report := f.run()
	tlsFinding := finding(t, report, sectionController, "tls")
	if tlsFinding.Status != StatusFailed {
		t.Fatalf("an untrusted certificate is %s", tlsFinding.Status)
	}
	// The single most common enrolment failure there is. The original error
	// string is preserved, because it is the one thing an operator can
	// usefully search for.
	if !strings.Contains(tlsFinding.Detail, "x509: certificate signed by unknown authority") {
		t.Errorf("the original error was replaced: %q", tlsFinding.Detail)
	}
	if !strings.Contains(tlsFinding.Fix, "HCC_AGENT_CA_FILE") {
		t.Errorf("the fix does not name the setting: %q", tlsFinding.Fix)
	}
}

func TestClockSkewIsReported(t *testing.T) {
	f := newFixture(t).installed()
	f.options.Probe = func(context.Context, string, string) (*controller.ProbeResult, error) {
		return &controller.ProbeResult{
			TLS:    true,
			Status: 200,
			// Three days out, which is the shape of the problem that reads as
			// a certificate error.
			ServerTime: time.Unix(1760000000, 0).Add(-72 * time.Hour),
			RoundTrip:  5 * time.Millisecond,
		}, nil
	}

	report := f.run()
	clock := finding(t, report, sectionController, "clock")
	if clock.Status != StatusFailed {
		t.Fatalf("a three-day skew is %s: %s", clock.Status, clock.Detail)
	}
	if !strings.Contains(clock.Fix, "NTP") {
		t.Errorf("the fix does not mention NTP: %q", clock.Fix)
	}
}

func TestASlowLinkIsNotReportedAsClockSkew(t *testing.T) {
	f := newFixture(t).installed()
	f.options.Probe = func(context.Context, string, string) (*controller.ProbeResult, error) {
		return &controller.ProbeResult{
			TLS:    true,
			Status: 200,
			// The controller's clock appears to be three minutes behind, and
			// the round trip was three minutes. An absurd link, and the point
			// is that the measurement's own imprecision is subtracted rather
			// than reported as a fault.
			ServerTime: time.Unix(1760000000, 0).Add(-3 * time.Minute),
			RoundTrip:  3 * time.Minute,
		}, nil
	}

	report := f.run()
	clock := finding(t, report, sectionController, "clock")
	if clock.Status == StatusFailed {
		t.Errorf("a slow link was reported as clock skew: %s", clock.Detail)
	}
}

func TestAFailedServiceIsAFailureWithTheJournalCommand(t *testing.T) {
	f := newFixture(t).installed()
	f.options.Systemctl = func(context.Context, ...string) (string, error) {
		return "LoadState=loaded\nActiveState=failed\nSubState=failed\n" +
			"UnitFileState=enabled\nNRestarts=5\nResult=exit-code\n", nil
	}

	report := f.run()
	state := finding(t, report, sectionService, "state")
	if state.Status != StatusFailed {
		t.Fatalf("a failed service is %s: %s", state.Status, state.Detail)
	}
	if !strings.Contains(state.Fix, "journalctl -u hcc-agent") {
		t.Errorf("the fix is not the command to run: %q", state.Fix)
	}
	restarts := finding(t, report, sectionService, "restarts")
	if restarts.Status != StatusWarning {
		t.Errorf("five restarts is %s, want a warning", restarts.Status)
	}
}

func TestADisabledServiceWarnsAboutReboots(t *testing.T) {
	f := newFixture(t).installed()
	f.options.Systemctl = func(context.Context, ...string) (string, error) {
		return "LoadState=loaded\nActiveState=active\nSubState=running\n" +
			"UnitFileState=disabled\nNRestarts=0\nResult=success\n", nil
	}

	report := f.run()
	boot := finding(t, report, sectionService, "at boot")
	if boot.Status != StatusWarning {
		t.Fatalf("a disabled unit is %s: %s", boot.Status, boot.Detail)
	}
	if !strings.Contains(boot.Detail, "after a reboot") {
		t.Errorf("the finding does not say what it costs: %q", boot.Detail)
	}
}

func TestHiddenProcFilesAreAFailureNamingTheCause(t *testing.T) {
	f := newFixture(t).installed()
	// /proc mounted with subset=pid: present, listable, and missing exactly
	// the four files the agent reads.
	for _, name := range []string{"stat", "meminfo", "loadavg", "uptime"} {
		if err := os.Remove(filepath.Join(f.root, "proc", name)); err != nil {
			t.Fatalf("cannot prepare the fixture: %v", err)
		}
	}

	report := f.run()
	proc := finding(t, report, sectionHost, "/proc")
	if proc.Status != StatusFailed {
		t.Fatalf("unreadable /proc is %s: %s", proc.Status, proc.Detail)
	}
	if !strings.Contains(proc.Fix, "subset=pid") {
		t.Errorf("the fix does not name the likely cause: %q", proc.Fix)
	}
}

func TestAnUnenrolledHostIsAWarningNotAFailure(t *testing.T) {
	f := newFixture(t)
	f.writeConfigDir()
	f.writeEnvFile("https://hcc.example.com")

	report := f.run()
	enrolled := finding(t, report, sectionCredential, "enrolled")
	// A freshly installed, not-yet-enrolled agent is in a normal state, and
	// the right response is an instruction rather than an error.
	if enrolled.Status != StatusWarning {
		t.Errorf("an unenrolled host is %s, want a warning: %s", enrolled.Status, enrolled.Detail)
	}
	if !strings.Contains(enrolled.Fix, "hcc-agent install") {
		t.Errorf("the fix does not say what to run: %q", enrolled.Fix)
	}
}

func TestAMissingCAFileIsAFailure(t *testing.T) {
	f := newFixture(t).installed()
	write(f.t, f.paths.EnvFile(),
		install.EnvironmentFile("https://hcc.example.com", "/etc/hcc-agent/missing-ca.pem"), 0o644)

	report := f.run()
	bundle := finding(t, report, sectionConfig, "CA bundle")
	if bundle.Status != StatusFailed {
		t.Fatalf("a configured but missing CA bundle is %s: %s", bundle.Status, bundle.Detail)
	}
	if !strings.Contains(bundle.Fix, "every connection will fail") {
		t.Errorf("the fix does not say what it costs: %q", bundle.Fix)
	}
}

// --- Output -----------------------------------------------------------------

func TestJSONOutputIsValidAndCarriesEveryFinding(t *testing.T) {
	f := newFixture(t).installed()
	report := f.run()

	var encoded bytes.Buffer
	if err := report.WriteJSON(&encoded); err != nil {
		t.Fatalf("cannot encode the report: %v", err)
	}

	var decoded Report
	if err := json.Unmarshal(encoded.Bytes(), &decoded); err != nil {
		t.Fatalf("the JSON output does not parse: %v\n%s", err, encoded.String())
	}
	if len(decoded.Findings) != len(report.Findings) {
		t.Errorf("round-tripped %d findings, want %d", len(decoded.Findings), len(report.Findings))
	}
	if decoded.Agent.Version == "" {
		t.Error("the agent version is missing from the JSON")
	}
	if decoded.GeneratedAt == "" {
		t.Error("the timestamp is missing from the JSON")
	}
}

func TestTextOutputSaysItIsSafeToPaste(t *testing.T) {
	f := newFixture(t).installed()
	var text bytes.Buffer
	f.run().WriteText(&text)

	if !strings.Contains(text.String(), "safe to paste into an issue report") {
		t.Errorf("the output does not say it is safe to share:\n%s", text.String())
	}
}

func TestFailedIsTrueOnlyForFailures(t *testing.T) {
	healthy := Report{Findings: []Finding{
		{Status: StatusOK}, {Status: StatusWarning}, {Status: StatusSkipped},
	}}
	if healthy.Failed() {
		t.Error("warnings and skips were treated as failures")
	}
	broken := Report{Findings: []Finding{{Status: StatusOK}, {Status: StatusFailed}}}
	if !broken.Failed() {
		t.Error("a failure was not reported")
	}
}

func TestParseEnvironmentFileHandlesWhatSystemdAccepts(t *testing.T) {
	parsed := parseEnvironmentFile(strings.Join([]string{
		"# a comment",
		"",
		"PLAIN=value",
		`QUOTED="value with spaces"`,
		"SINGLE='single'",
		"  SPACED  =  padded  ",
		"NOT_AN_ASSIGNMENT",
		"EMPTY=",
	}, "\n"))

	for name, want := range map[string]string{
		"PLAIN":  "value",
		"QUOTED": "value with spaces",
		"SINGLE": "single",
		"SPACED": "padded",
		"EMPTY":  "",
	} {
		if got, ok := parsed[name]; !ok || got != want {
			t.Errorf("%s = %q (present %v), want %q", name, got, ok, want)
		}
	}
	if _, ok := parsed["NOT_AN_ASSIGNMENT"]; ok {
		t.Error("a line with no = was read as a setting")
	}
}
