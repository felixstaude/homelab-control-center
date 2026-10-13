package install

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// fakeSystem records privileged operations instead of performing them.
//
// Every method is one effect, so the fake is a list of calls rather than a
// simulated operating system. What it buys: the step ordering, every refusal
// and the whole rollback path are exercised by `go test` as an ordinary user,
// on a machine the test does not modify. What it cannot buy is whether the
// unit actually starts — that is what the installation matrix in CI is for,
// against real systemd in a real container, because a mocked systemd proves
// nothing about systemd.
type fakeSystem struct {
	mutex sync.Mutex

	euid          int
	systemd       bool
	machine       string
	users         map[string]UserInfo
	nextUID       int
	calls         []string
	systemctlErr  map[string]error
	activeState   string
	journal       string
	createUserErr error
	chownErr      error
	// configAfterStart makes `enable --now` write the configuration file the
	// installer waits for, which is how the success path is exercised without
	// a controller.
	configAfterStart func()
}

func newFakeSystem() *fakeSystem {
	return &fakeSystem{
		euid:         0,
		systemd:      true,
		machine:      "x86_64",
		users:        map[string]UserInfo{},
		nextUID:      970,
		systemctlErr: map[string]error{},
		activeState:  "active",
	}
}

func (f *fakeSystem) record(call string) {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	f.calls = append(f.calls, call)
}

func (f *fakeSystem) recorded() []string {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeSystem) Geteuid() int         { return f.euid }
func (f *fakeSystem) SystemdPresent() bool { return f.systemd }

func (f *fakeSystem) LookupUser(name string) (UserInfo, error) {
	f.mutex.Lock()
	defer f.mutex.Unlock()
	if entry, ok := f.users[name]; ok {
		return entry, nil
	}
	return UserInfo{}, ErrNoSuchUser
}

func (f *fakeSystem) CreateServiceAccount(_ context.Context, name string) error {
	f.record("create-user " + name)
	if f.createUserErr != nil {
		return f.createUserErr
	}
	f.mutex.Lock()
	defer f.mutex.Unlock()
	f.users[name] = UserInfo{Name: name, UID: f.nextUID, GID: f.nextUID, Shell: "/usr/sbin/nologin"}
	f.nextUID++
	return nil
}

func (f *fakeSystem) DeleteServiceAccount(_ context.Context, name string) error {
	f.record("delete-user " + name)
	f.mutex.Lock()
	defer f.mutex.Unlock()
	delete(f.users, name)
	return nil
}

func (f *fakeSystem) Systemctl(_ context.Context, args ...string) (string, error) {
	joined := strings.Join(args, " ")
	f.record("systemctl " + joined)
	if err, ok := f.systemctlErr[joined]; ok {
		return "", err
	}
	if joined == "is-active "+ServiceName {
		return f.activeState, nil
	}
	// Both of the verbs that bring the agent up. The installer and the
	// upgrader both wait for the configuration file to appear, which on a
	// real machine is the agent writing what the controller sent it — so the
	// fake produces that effect here rather than on a timer. A timer would
	// make the test a race against whatever the machine is doing, which it
	// was for exactly one commit.
	if f.configAfterStart != nil &&
		(strings.HasPrefix(joined, "enable --now") || strings.HasPrefix(joined, "restart")) {
		f.configAfterStart()
	}
	return "", nil
}

func (f *fakeSystem) JournalTail(_ context.Context, unit string, lines int) (string, error) {
	f.record(fmt.Sprintf("journalctl %s %d", unit, lines))
	return f.journal, nil
}

func (f *fakeSystem) MachineArchitecture() string { return f.machine }

func (f *fakeSystem) Chown(path string, uid, gid int) error {
	f.record(fmt.Sprintf("chown %d:%d %s", uid, gid, filepath.Base(path)))
	return f.chownErr
}

// fixture is a rooted installation under a temporary directory, with the
// machinery the installer needs in order to run as an ordinary user.
type fixture struct {
	t        *testing.T
	root     string
	paths    Paths
	system   *fakeSystem
	output   *bytes.Buffer
	options  Options
	enrolled int
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	root := t.TempDir()

	// The directories a real machine already has.
	for _, directory := range []string{
		"/usr/local/bin", "/etc/systemd/system", "/run/systemd/system", "/proc",
	} {
		if err := os.MkdirAll(filepath.Join(root, directory), 0o755); err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}
	// The four kernel files the preflight insists on, with content — the
	// check reads a byte rather than stat-ing, because a /proc mounted with
	// subset=pid is present and missing exactly these.
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

	source := filepath.Join(root, "source-hcc-agent")
	// A minimal ELF header, so readExecutable's check passes without needing
	// a 7 MB binary in a unit test.
	if err := os.WriteFile(source, fakeELF(), 0o755); err != nil {
		t.Fatalf("cannot build the fixture: %v", err)
	}

	f := &fixture{
		t:      t,
		root:   root,
		paths:  Paths{Root: root},
		system: newFakeSystem(),
		output: &bytes.Buffer{},
	}
	f.options = Options{
		ControllerURL:  "https://hcc.example.com",
		TokenSource:    staticToken{value: "ENROLMENT-CREDENTIAL-VALUE"},
		SourceBinary:   source,
		ConnectTimeout: 2 * time.Second,
		ProcRoot:       filepath.Join(root, "proc"),
		Out:            f.output,
		Probe: func(context.Context, string, string) (string, error) {
			return "reachable, certificate verified", nil
		},
		Enroll: func(context.Context, string, string, string) (*Enrollment, error) {
			f.enrolled++
			return &Enrollment{
				HostID:     "3f2c8a14-0000-0000-0000-000000000000",
				HostName:   "homeserver",
				Credential: "AGENT-CREDENTIAL-VALUE",
			}, nil
		},
	}
	// The success path needs the configuration file to appear, which on a
	// real machine is the agent writing what the controller sent it.
	f.system.configAfterStart = func() { f.writeConfiguration(1) }
	return f
}

// writeConfiguration writes what the agent writes when the controller sends
// it a configuration: a document with an entry for every known module, all
// disabled, which is the state of a freshly enrolled host.
//
// Built from the agent's own types rather than from a JSON literal, so a
// change to the document shape fails to compile here instead of producing a
// fixture the agent's loader quietly refuses — which is what the first draft
// of this did.
func (f *fixture) writeConfiguration(version int64) {
	f.t.Helper()

	modules := make(map[string]monitoring.ModuleConfig)
	for _, id := range monitoring.ModuleIDs() {
		modules[id] = monitoring.ModuleConfig{Enabled: false}
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
		f.t.Fatalf("cannot encode the fixture configuration: %v", err)
	}
	document := string(encoded)

	if err := os.MkdirAll(filepath.Dir(f.paths.Configuration()), 0o700); err != nil {
		f.t.Fatalf("cannot write the fixture configuration: %v", err)
	}
	if err := os.WriteFile(f.paths.Configuration(), []byte(document), 0o600); err != nil {
		f.t.Fatalf("cannot write the fixture configuration: %v", err)
	}
	// Sanity: if the agent's own loader would refuse this, the test is
	// asserting against a file the installer could never see.
	if _, err := monitoring.Load(f.paths.Configuration()); err != nil {
		f.t.Fatalf("the fixture configuration is not loadable: %v", err)
	}
}

func (f *fixture) installer() *Installer {
	f.t.Helper()
	options := f.options
	if options.Out == nil {
		options.Out = f.output
	}
	return &Installer{
		System:  f.system,
		Paths:   f.paths,
		Options: options,
		out:     options.Out,
	}
}

func (f *fixture) run() error {
	f.t.Helper()
	return f.installer().Run(context.Background())
}

// staticToken is a TokenSource with a fixed value.
type staticToken struct {
	value       string
	unavailable string
}

func (s staticToken) Available() string { return s.unavailable }
func (s staticToken) Describe() string  { return "a test fixture" }
func (s staticToken) Read() (string, error) {
	if s.unavailable != "" {
		return "", errors.New(s.unavailable)
	}
	return s.value, nil
}

// fakeELF is the smallest thing readExecutable and verifyELF both accept: a
// 64-bit little-endian ET_EXEC x86-64 header and nothing else.
func fakeELF() []byte {
	header := make([]byte, 64)
	copy(header, "\x7fELF")
	header[4] = 2 // 64-bit
	header[5] = 1 // little-endian
	header[6] = 1 // version
	header[16] = 2
	header[18] = 0x3E
	return header
}

// --- The happy path ---------------------------------------------------------

func TestInstallPlacesEverythingWithTheRightModes(t *testing.T) {
	f := newFixture(t)

	if err := f.run(); err != nil {
		t.Fatalf("install failed: %v\n%s", err, f.output.String())
	}

	for _, expectation := range []struct {
		path string
		mode fs.FileMode
	}{
		{f.paths.Binary(), BinaryMode},
		{f.paths.Unit(), UnitMode},
		{f.paths.EnvFile(), EnvFileMode},
		{f.paths.ConfigDir(), ConfigDirMode | fs.ModeDir},
		{f.paths.Credential(), credentials.FileMode},
	} {
		info, err := os.Lstat(expectation.path)
		if err != nil {
			t.Errorf("%s was not created: %v", expectation.path, err)
			continue
		}
		if info.Mode() != expectation.mode {
			t.Errorf("%s has mode %v, want %v", expectation.path, info.Mode(), expectation.mode)
		}
	}

	// The unit written is byte-for-byte the embedded one. This is what makes
	// `--dry-run` an audit: what it prints is what gets installed.
	written, err := os.ReadFile(f.paths.Unit())
	if err != nil {
		t.Fatalf("cannot read the unit: %v", err)
	}
	if !bytes.Equal(written, UnitFile()) {
		t.Error("the installed unit differs from the embedded one")
	}

	if f.enrolled != 1 {
		t.Errorf("enrolled %d times, want exactly 1", f.enrolled)
	}

	calls := strings.Join(f.system.recorded(), "\n")
	for _, expected := range []string{
		"create-user hcc-agent",
		"systemctl daemon-reload",
		"systemctl enable --now hcc-agent",
	} {
		if !strings.Contains(calls, expected) {
			t.Errorf("expected the installer to run %q; it ran:\n%s", expected, calls)
		}
	}
}

func TestInstallChownsTheCredentialToTheServiceAccount(t *testing.T) {
	f := newFixture(t)
	if err := f.run(); err != nil {
		t.Fatalf("install failed: %v\n%s", err, f.output.String())
	}

	calls := strings.Join(f.system.recorded(), "\n")
	// The credential is written by root at 0600 and then handed over. The
	// mode never changes, so there is no window in which it is readable by
	// anybody else — and the agent's own loader accepts a file owned by the
	// uid it runs as.
	if !strings.Contains(calls, "chown 970:970 credential") {
		t.Errorf("the credential was not given to the service account; calls:\n%s", calls)
	}
	if !strings.Contains(calls, "chown 970:970 hcc-agent") {
		t.Errorf("the configuration directory was not given to the service account; calls:\n%s", calls)
	}
}

func TestInstallWaitsForTheControllerToSendAConfiguration(t *testing.T) {
	f := newFixture(t)
	// Nothing writes the configuration, so the installer's definition of
	// "installed" — connected, not merely started — must not be satisfied.
	f.system.configAfterStart = nil
	f.options.ConnectTimeout = 300 * time.Millisecond
	f.system.journal = "Oct 13 22:00:00 host hcc-agent[1]: msg=\"cannot reach the controller\""

	err := f.run()
	if err == nil {
		t.Fatalf("install reported success without a connection:\n%s", f.output.String())
	}
	if !InstalledNotConnected(err) {
		t.Errorf("error is not the installed-not-connected kind: %v", err)
	}
	// The files stay. The credential has been spent and the fault is almost
	// always the network, so removing a correct installation would be the
	// wrong answer — and would cost a second one-time credential.
	if _, statErr := os.Stat(f.paths.Credential()); statErr != nil {
		t.Errorf("the credential was removed after a connection timeout: %v", statErr)
	}
	if _, statErr := os.Stat(f.paths.Binary()); statErr != nil {
		t.Errorf("the binary was removed after a connection timeout: %v", statErr)
	}
	if !strings.Contains(err.Error(), "cannot reach the controller") {
		t.Errorf("the journal was not included in the failure:\n%v", err)
	}
	if !strings.Contains(err.Error(), "hcc-agent doctor") {
		t.Errorf("the failure does not point at doctor:\n%v", err)
	}
}

func TestInstallStopsWaitingWhenTheServiceHasFailed(t *testing.T) {
	f := newFixture(t)
	f.system.configAfterStart = nil
	f.system.activeState = "failed"
	// Deliberately long: the point is that it does not wait this out.
	f.options.ConnectTimeout = time.Minute

	started := time.Now()
	err := f.run()
	elapsed := time.Since(started)

	if err == nil {
		t.Fatal("install reported success with a failed service")
	}
	if elapsed > 10*time.Second {
		t.Errorf("waited %s for a service that had already failed", elapsed)
	}
	if !strings.Contains(err.Error(), "the service is failed") {
		t.Errorf("the failure does not say the service failed: %v", err)
	}
}

// --- Refusals ---------------------------------------------------------------

func TestInstallRefusesWithoutRoot(t *testing.T) {
	f := newFixture(t)
	f.system.euid = 1000

	err := f.run()
	if err == nil {
		t.Fatal("install proceeded as an ordinary user")
	}
	if !strings.Contains(err.Error(), "needs root") {
		t.Errorf("the refusal does not mention root: %v", err)
	}
	if _, statErr := os.Stat(f.paths.Binary()); statErr == nil {
		t.Error("a refused installation placed a binary anyway")
	}
	if f.enrolled != 0 {
		t.Error("a refused installation spent an enrollment credential")
	}
}

func TestInstallRefusesWithoutSystemd(t *testing.T) {
	f := newFixture(t)
	f.system.systemd = false

	err := f.run()
	if err == nil {
		t.Fatal("install proceeded without systemd")
	}
	if !strings.Contains(err.Error(), "systemd is not running") {
		t.Errorf("the refusal does not mention systemd: %v", err)
	}
}

func TestInstallRefusesWhenProcIsHidden(t *testing.T) {
	f := newFixture(t)
	// What /proc mounted with subset=pid looks like: the directory is there
	// and the kernel's global files are not. The unit shipped with
	// ProcSubset=pid for a while, which would have produced exactly this.
	if err := os.Remove(filepath.Join(f.root, "proc", "stat")); err != nil {
		t.Fatalf("cannot prepare the fixture: %v", err)
	}

	err := f.run()
	if err == nil {
		t.Fatal("install proceeded with /proc/stat unreadable")
	}
	if !strings.Contains(err.Error(), "subset=pid") {
		t.Errorf("the refusal does not name the likely cause: %v", err)
	}
}

func TestInstallRefusesToAdoptALoginAccount(t *testing.T) {
	f := newFixture(t)
	// Somebody's user account that happens to be called hcc-agent. Running
	// the service as them would give them read access to the credential.
	f.system.users[ServiceUser] = UserInfo{
		Name: ServiceUser, UID: 1001, GID: 1001, Shell: "/bin/bash",
	}

	err := f.run()
	if err == nil {
		t.Fatal("install adopted a login account")
	}
	if !strings.Contains(err.Error(), "login account") {
		t.Errorf("the refusal does not explain itself: %v", err)
	}
	if f.enrolled != 0 {
		t.Error("a refused installation spent an enrollment credential")
	}
}

func TestInstallAdoptsAnExistingSystemAccount(t *testing.T) {
	f := newFixture(t)
	f.system.users[ServiceUser] = UserInfo{
		Name: ServiceUser, UID: 412, GID: 412, Shell: "/sbin/nologin",
	}

	if err := f.run(); err != nil {
		t.Fatalf("install failed: %v\n%s", err, f.output.String())
	}
	calls := strings.Join(f.system.recorded(), "\n")
	if strings.Contains(calls, "create-user") {
		t.Error("an existing system account was recreated")
	}
	if !strings.Contains(calls, "chown 412:412 credential") {
		t.Errorf("the credential was not given to the existing account; calls:\n%s", calls)
	}
}

func TestInstallRefusesAnAlreadyCompleteInstallation(t *testing.T) {
	f := newFixture(t)
	if err := f.run(); err != nil {
		t.Fatalf("the first install failed: %v\n%s", err, f.output.String())
	}
	before := f.enrolled

	f.output.Reset()
	err := f.run()
	if err == nil {
		t.Fatal("a second install proceeded without --force")
	}
	if !strings.Contains(err.Error(), "already has a complete installation") {
		t.Errorf("the refusal does not say why: %v", err)
	}
	if !strings.Contains(err.Error(), "hcc-agent upgrade") {
		t.Errorf("the refusal does not point at upgrade: %v", err)
	}
	if f.enrolled != before {
		t.Error("a refused re-installation spent an enrollment credential")
	}
}

func TestForceReenrollsACompleteInstallation(t *testing.T) {
	f := newFixture(t)
	if err := f.run(); err != nil {
		t.Fatalf("the first install failed: %v", err)
	}

	f.output.Reset()
	f.options.Force = true
	if err := f.run(); err != nil {
		t.Fatalf("--force install failed: %v\n%s", err, f.output.String())
	}
	if f.enrolled != 2 {
		t.Errorf("enrolled %d times, want 2 (the second under --force)", f.enrolled)
	}
}

func TestInstallRefusesAnUnreachableController(t *testing.T) {
	f := newFixture(t)
	f.options.Probe = func(context.Context, string, string) (string, error) {
		return "", errors.New("x509: certificate signed by unknown authority")
	}

	err := f.run()
	if err == nil {
		t.Fatal("install proceeded against an unverifiable controller")
	}
	// Nothing may have been created: a typo in a hostname must not leave
	// debris, which is why the probe runs before the first side effect.
	for _, path := range []string{f.paths.Binary(), f.paths.ConfigDir(), f.paths.Unit()} {
		if _, statErr := os.Stat(path); statErr == nil {
			t.Errorf("%s was created before the controller was verified", path)
		}
	}
	if len(f.system.recorded()) != 0 {
		t.Errorf("privileged operations ran before the controller was verified: %v",
			f.system.recorded())
	}
}

// --- The transaction --------------------------------------------------------

func TestAFailureBeforeEnrollmentRollsEverythingBack(t *testing.T) {
	f := newFixture(t)
	f.options.TokenSource = staticToken{unavailable: ""}
	f.options.Enroll = func(context.Context, string, string, string) (*Enrollment, error) {
		return nil, errors.New("the controller rejected this credential")
	}

	err := f.run()
	if err == nil {
		t.Fatal("install succeeded with a failing enrollment")
	}

	// Everything this run created is gone, including the account.
	for _, path := range []string{
		f.paths.Binary(), f.paths.Unit(), f.paths.EnvFile(), f.paths.ConfigDir(),
	} {
		if _, statErr := os.Stat(path); statErr == nil {
			t.Errorf("%s survived the rollback", path)
		}
	}
	calls := strings.Join(f.system.recorded(), "\n")
	if !strings.Contains(calls, "delete-user hcc-agent") {
		t.Errorf("the account this run created was not removed; calls:\n%s", calls)
	}
	// And the service was never started, so there is nothing to stop.
	if strings.Contains(calls, "enable --now") {
		t.Error("the service was started despite a failed enrollment")
	}
}

func TestAFailureAfterEnrollmentKeepsTheCredential(t *testing.T) {
	f := newFixture(t)
	// The unit write succeeds and the start fails — a wedged systemd, or a
	// unit systemd will not accept.
	f.system.systemctlErr["enable --now "+ServiceName] = errors.New("job failed")

	err := f.run()
	if err == nil {
		t.Fatal("install succeeded with a failing service start")
	}

	// The credential survives. A one-time enrollment credential was spent to
	// obtain it, and deleting it would cost the operator another one to
	// recover from a problem that has nothing to do with credentials.
	if _, statErr := os.Stat(f.paths.Credential()); statErr != nil {
		t.Fatalf("the credential was destroyed by the rollback: %v", statErr)
	}
	// And the directory holding it survives with it, even though this run
	// created it, because os.Remove on a non-empty directory fails and that
	// failure is the protection.
	if _, statErr := os.Stat(f.paths.ConfigDir()); statErr != nil {
		t.Errorf("the configuration directory was removed with a credential inside: %v", statErr)
	}
	// The account must survive too: it owns the credential file, and
	// recreating it later could allocate a different uid.
	if _, lookupErr := f.system.LookupUser(ServiceUser); lookupErr != nil {
		t.Error("the account owning the surviving credential was deleted")
	}
	if !strings.Contains(f.output.String(), "Re-running this command will complete") {
		t.Errorf("the operator was not told how to recover:\n%s", f.output.String())
	}
}

func TestARerunAfterAPartialInstallDoesNotEnrollAgain(t *testing.T) {
	f := newFixture(t)
	f.system.systemctlErr["enable --now "+ServiceName] = errors.New("job failed")
	if err := f.run(); err == nil {
		t.Fatal("the first install should have failed")
	}
	if f.enrolled != 1 {
		t.Fatalf("expected one enrollment in the failed run, got %d", f.enrolled)
	}

	// The operator fixes systemd and runs exactly the same command again.
	delete(f.system.systemctlErr, "enable --now "+ServiceName)
	f.output.Reset()
	if err := f.run(); err != nil {
		t.Fatalf("the second install failed: %v\n%s", err, f.output.String())
	}

	// This is the property that matters: no second one-time credential was
	// needed to recover from a failure that had nothing to do with
	// credentials.
	if f.enrolled != 1 {
		t.Errorf("the recovery run enrolled again (%d total)", f.enrolled)
	}
	if _, err := os.Stat(f.paths.Unit()); err != nil {
		t.Errorf("the recovery run did not finish the installation: %v", err)
	}
}

func TestInstallKeepsAPreviousBinaryAndRestoresItOnFailure(t *testing.T) {
	f := newFixture(t)
	// A binary from a previous version, already installed.
	marker := append(fakeELF(), []byte("PREVIOUS VERSION")...)
	if err := os.WriteFile(f.paths.Binary(), marker, 0o755); err != nil {
		t.Fatalf("cannot prepare the fixture: %v", err)
	}
	f.options.Enroll = func(context.Context, string, string, string) (*Enrollment, error) {
		return nil, errors.New("the controller rejected this credential")
	}

	if err := f.run(); err == nil {
		t.Fatal("install succeeded with a failing enrollment")
	}

	restored, err := os.ReadFile(f.paths.Binary())
	if err != nil {
		t.Fatalf("the previous binary was not restored: %v", err)
	}
	if !bytes.Equal(restored, marker) {
		t.Error("the binary at the target is not the one that was there before")
	}
	if _, err := os.Stat(f.paths.PreviousBinary()); err == nil {
		t.Error("the .previous copy was left behind after a rollback")
	}
}

// --- Dry run ----------------------------------------------------------------

func TestDryRunChangesNothingAndPrintsTheExactUnit(t *testing.T) {
	f := newFixture(t)
	f.options.DryRun = true

	if err := f.run(); err != nil {
		t.Fatalf("dry run failed: %v\n%s", err, f.output.String())
	}

	output := f.output.String()
	// The audit property: every line of the unit that will be installed is in
	// the output, so reviewing the dry run is reviewing the installation.
	for _, line := range strings.Split(strings.TrimSpace(string(UnitFile())), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if !strings.Contains(output, strings.TrimSpace(line)) {
			t.Fatalf("the dry run omitted a line of the unit: %q", line)
		}
	}
	// And the environment file, which is the other thing it writes.
	if !strings.Contains(output, "HCC_AGENT_CONTROLLER_URL=https://hcc.example.com") {
		t.Error("the dry run did not show the environment file's contents")
	}

	for _, path := range []string{
		f.paths.Binary(), f.paths.Unit(), f.paths.EnvFile(),
		f.paths.ConfigDir(), f.paths.Credential(),
	} {
		if _, err := os.Stat(path); err == nil {
			t.Errorf("the dry run created %s", path)
		}
	}
	if f.enrolled != 0 {
		t.Error("the dry run spent an enrollment credential")
	}
	if len(f.system.recorded()) != 0 {
		t.Errorf("the dry run ran privileged operations: %v", f.system.recorded())
	}
}

func TestDryRunShowsThePlanEvenWithoutRoot(t *testing.T) {
	f := newFixture(t)
	f.options.DryRun = true
	f.system.euid = 1000

	err := f.run()
	// It still reports the problem — but only after printing the plan, which
	// is the whole reason somebody runs --dry-run before using sudo.
	if err == nil {
		t.Fatal("the dry run did not report that root is needed")
	}
	output := f.output.String()
	if !strings.Contains(output, "Would do, in order") {
		t.Errorf("the dry run hid the plan behind the root check:\n%s", output)
	}
	if !strings.Contains(output, "Nothing above has been done") {
		t.Error("the dry run did not say it had changed nothing")
	}
}

func TestDryRunNeverPrintsTheEnrollmentCredential(t *testing.T) {
	const secret = "ENROLMENT-CREDENTIAL-VALUE"
	f := newFixture(t)
	f.options.DryRun = true
	f.options.TokenSource = staticToken{value: secret}

	if err := f.run(); err != nil {
		t.Fatalf("dry run failed: %v", err)
	}
	if strings.Contains(f.output.String(), secret) {
		t.Error("the dry run printed the enrollment credential")
	}
}

func TestInstallNeverPrintsEitherCredential(t *testing.T) {
	f := newFixture(t)
	const enrollmentSecret = "ENROLMENT-CREDENTIAL-VALUE"
	const agentSecret = "AGENT-CREDENTIAL-VALUE"
	f.options.TokenSource = staticToken{value: enrollmentSecret}

	if err := f.run(); err != nil {
		t.Fatalf("install failed: %v\n%s", err, f.output.String())
	}

	output := f.output.String()
	// Both, because there are two: the one-time credential the operator
	// pasted and the durable one the controller returned. The installer
	// prints the credential file's *path and mode* and never its contents.
	for _, secret := range []string{enrollmentSecret, agentSecret} {
		if strings.Contains(output, secret) {
			t.Errorf("the installer printed a credential (%q) in its output", secret)
		}
	}
	// And the environment file must not carry one either — the unit reads it,
	// and it is deliberately mode 0644.
	environment, err := os.ReadFile(f.paths.EnvFile())
	if err != nil {
		t.Fatalf("cannot read the environment file: %v", err)
	}
	for _, secret := range []string{enrollmentSecret, agentSecret} {
		if strings.Contains(string(environment), secret) {
			t.Errorf("the environment file carries a credential (%q)", secret)
		}
	}
}

// --- The unit ---------------------------------------------------------------

func TestTheUnitHasTheDirectivesThatMatter(t *testing.T) {
	unit := string(UnitFile())

	for _, required := range []string{
		"User=hcc-agent",
		"CapabilityBoundingSet=",
		"NoNewPrivileges=yes",
		"ProtectSystem=strict",
		"ReadWritePaths=/etc/hcc-agent",
		"RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
		"MemoryDenyWriteExecute=yes",
		"Restart=on-failure",
		"ExecStart=/usr/local/bin/hcc-agent run",
	} {
		if !strings.Contains(unit, required) {
			t.Errorf("the unit is missing %q", required)
		}
	}

	// ProcSubset=pid mounts /proc with subset=pid, which hides /proc/stat,
	// /proc/meminfo, /proc/loadavg and /proc/uptime — every file the agent
	// reads. With it the service starts, connects, reports itself healthy and
	// collects nothing, which is the worst failure mode available. It was in
	// this file until the installer took ownership of it, and the unit had
	// never been started, so nothing had caught it.
	for _, forbidden := range []string{"ProcSubset=pid", "ProcSubset=all"} {
		if strings.Contains(unit, "\n"+forbidden) {
			t.Errorf("the unit sets %q, which hides the kernel files the agent reads", forbidden)
		}
	}

	// StartLimit* are read from [Unit] and silently ignored in [Service], so
	// a restart limit written in the obvious place does nothing at all.
	// Split on the section header at the start of a line. A plain
	// Cut(unit, "[Service]") finds the comment above it that explains this
	// very trap, which is a pleasing way for a test to be wrong.
	unitSection, serviceSection, found := strings.Cut(unit, "\n[Service]\n")
	if !found {
		t.Fatal("the unit has no [Service] section")
	}
	if !strings.Contains(unitSection, "StartLimitIntervalSec=") {
		t.Error("StartLimitIntervalSec is not in [Unit], where systemd reads it")
	}
	if strings.Contains(serviceSection, "\nStartLimitIntervalSec=") {
		t.Error("StartLimitIntervalSec is in [Service], where systemd ignores it")
	}
}

func TestTheEnvironmentFileCarriesNoSecretAndNoTLSEscapeHatch(t *testing.T) {
	rendered := string(EnvironmentFile("https://hcc.example.com", "/etc/hcc-agent/ca.pem"))

	if !strings.Contains(rendered, "HCC_AGENT_CONTROLLER_URL=https://hcc.example.com") {
		t.Error("the controller URL is missing")
	}
	if !strings.Contains(rendered, "HCC_AGENT_CA_FILE=/etc/hcc-agent/ca.pem") {
		t.Error("the CA path is missing")
	}
	// There is no such variable and there must never be one. An agent whose
	// credential crosses an unverified TLS session is an agent whose
	// credential can be collected by anyone on the path.
	if strings.Contains(rendered, "TLS_SKIP_VERIFY") || strings.Contains(rendered, "INSECURE") {
		t.Error("the environment file offers a way to skip TLS verification")
	}
	if strings.Contains(rendered, "TOKEN") {
		t.Error("the environment file mentions a token, which belongs nowhere near it")
	}
}

// --- Detection --------------------------------------------------------------

func TestDetect(t *testing.T) {
	for _, testCase := range []struct {
		name  string
		setup func(t *testing.T, paths Paths)
		want  Mode
	}{
		{
			name:  "nothing installed",
			setup: func(*testing.T, Paths) {},
			want:  ModeFresh,
		},
		{
			name: "credential only",
			setup: func(t *testing.T, paths Paths) {
				writeCredential(t, paths)
			},
			want: ModeIncomplete,
		},
		{
			name: "binary and unit but no credential",
			setup: func(t *testing.T, paths Paths) {
				writeFile(t, paths.Binary(), fakeELF(), 0o755)
				writeFile(t, paths.Unit(), UnitFile(), 0o644)
			},
			// Fresh, because the thing that makes an installation an
			// installation is the credential: without one the agent cannot
			// connect, so the full sequence including enrollment is right.
			want: ModeFresh,
		},
		{
			name: "everything",
			setup: func(t *testing.T, paths Paths) {
				writeCredential(t, paths)
				writeFile(t, paths.Binary(), fakeELF(), 0o755)
				writeFile(t, paths.Unit(), UnitFile(), 0o644)
			},
			want: ModeInstalled,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			paths := Paths{Root: t.TempDir()}
			testCase.setup(t, paths)
			if got := Detect(paths); got != testCase.want {
				t.Errorf("Detect = %v, want %v", got, testCase.want)
			}
		})
	}
}

func writeCredential(t *testing.T, paths Paths) {
	t.Helper()
	if err := credentials.Save(paths.Credential(), "a-credential"); err != nil {
		t.Fatalf("cannot write the fixture credential: %v", err)
	}
}

func writeFile(t *testing.T, path string, contents []byte, mode fs.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("cannot prepare %s: %v", path, err)
	}
	if err := os.WriteFile(path, contents, mode); err != nil {
		t.Fatalf("cannot write %s: %v", path, err)
	}
}

func TestIsSystemAccount(t *testing.T) {
	for _, testCase := range []struct {
		name string
		user UserInfo
		want bool
	}{
		{"service account", UserInfo{UID: 974, Shell: "/usr/sbin/nologin"}, true},
		{"service account on fedora", UserInfo{UID: 974, Shell: "/sbin/nologin"}, true},
		{"false shell", UserInfo{UID: 974, Shell: "/bin/false"}, true},
		{"unknown shell", UserInfo{UID: 974, Shell: ""}, true},
		{"a person", UserInfo{UID: 1000, Shell: "/bin/bash"}, false},
		// The interesting one: a low uid with a real shell is somebody's
		// legacy login, and adopting it would hand them the credential.
		{"legacy login with a low uid", UserInfo{UID: 500, Shell: "/bin/bash"}, false},
		{"root", UserInfo{UID: 0, Shell: "/bin/bash"}, false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if got := testCase.user.IsSystemAccount(); got != testCase.want {
				t.Errorf("IsSystemAccount = %v, want %v", got, testCase.want)
			}
		})
	}
}
