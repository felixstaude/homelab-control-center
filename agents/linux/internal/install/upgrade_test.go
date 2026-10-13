package install

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// The upgrade tests need a binary that is both a real ELF object — the
// upgrader checks the machine type before it swaps anything — and able to
// answer `version`, because the upgrader runs it and reads what it says. A
// shell script satisfies the second and fails the first; an ELF header
// satisfies the first and cannot execute.
//
// So the test binary acts as the fake agent. [TestMain] below notices
// HCC_FAKE_AGENT and prints a version line instead of running tests, and
// [fakeAgentBinary] returns a copy of this binary with the version appended
// after the ELF image — which survives being copied into an archive, read out
// of it, and renamed into place. That is the whole point: what gets exercised
// is the real read-verify-swap path on real bytes.
const fakeAgentMarker = "HCC-FAKE-AGENT-VERSION="

func TestMain(m *testing.M) {
	if os.Getenv("HCC_FAKE_AGENT") == "1" {
		fmt.Printf(
			"hcc-agent %s (commit testcommit, built now, go-test, linux/%s)\n",
			fakeAgentSelfVersion(), GOARCH(),
		)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// fakeAgentSelfVersion reads the version appended to this binary's own image.
func fakeAgentSelfVersion() string {
	image, err := os.ReadFile("/proc/self/exe")
	if err != nil {
		return "0.0.0"
	}
	index := bytes.LastIndex(image, []byte(fakeAgentMarker))
	if index < 0 {
		return "0.0.0"
	}
	rest := image[index+len(fakeAgentMarker):]
	if end := bytes.IndexByte(rest, '\n'); end >= 0 {
		return string(rest[:end])
	}
	return "0.0.0"
}

// testBinary is this test binary's own bytes, read once.
var testBinary = func() []byte {
	self, err := os.Executable()
	if err != nil {
		panic("cannot find the test binary: " + err.Error())
	}
	image, err := os.ReadFile(self)
	if err != nil {
		panic("cannot read the test binary: " + err.Error())
	}
	return image
}()

// fakeAgentBinary returns a runnable ELF that reports the given version.
//
// Cached per version: the image is a copy of this test binary, which is tens
// of megabytes under the race detector, and several tests ask for the same two
// versions.
func fakeAgentBinary(t *testing.T, version string) []byte {
	t.Helper()
	// Set here rather than in each test, so a test that forgets it gets
	// "0.0.0" from a real binary rather than a confusing exec failure.
	t.Setenv("HCC_FAKE_AGENT", "1")

	fakeAgentCacheMutex.Lock()
	defer fakeAgentCacheMutex.Unlock()
	if cached, ok := fakeAgentCache[version]; ok {
		return cached
	}
	image := make([]byte, 0, len(testBinary)+64)
	image = append(image, testBinary...)
	image = append(image, []byte("\n"+fakeAgentMarker+version+"\n")...)
	fakeAgentCache[version] = image
	return image
}

var (
	fakeAgentCacheMutex sync.Mutex
	fakeAgentCache      = map[string][]byte{}
)

// buildArchive produces a gzipped tar with the given members.
func buildArchive(t *testing.T, members map[string][]byte, modes map[string]int64) []byte {
	t.Helper()
	var raw bytes.Buffer
	// No compression. These archives carry a copy of the race-instrumented
	// test binary, and gzipping tens of megabytes per test case dominated the
	// suite's runtime; the stream is still a valid gzip one, which is all the
	// upgrader cares about.
	compressed, err := gzip.NewWriterLevel(&raw, gzip.NoCompression)
	if err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	archive := tar.NewWriter(compressed)
	for name, contents := range members {
		mode := int64(0o755)
		if override, ok := modes[name]; ok {
			mode = override
		}
		if err := archive.WriteHeader(&tar.Header{
			Name:     name,
			Mode:     mode,
			Size:     int64(len(contents)),
			Typeflag: tar.TypeReg,
			ModTime:  time.Unix(1700000000, 0),
		}); err != nil {
			t.Fatalf("cannot build the archive: %v", err)
		}
		if _, err := archive.Write(contents); err != nil {
			t.Fatalf("cannot build the archive: %v", err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	if err := compressed.Close(); err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	return raw.Bytes()
}

func writeArchive(t *testing.T, directory string, contents []byte) (path, digest string) {
	t.Helper()
	path = filepath.Join(directory, "hcc-agent_9.9.9_linux_amd64.tar.gz")
	if err := os.WriteFile(path, contents, 0o644); err != nil {
		t.Fatalf("cannot write the archive: %v", err)
	}
	sum := sha256.Sum256(contents)
	return path, hex.EncodeToString(sum[:])
}

func (f *fixture) upgrader(options UpgradeOptions) *Upgrader {
	f.t.Helper()
	if options.Out == nil {
		options.Out = f.output
	}
	if options.ConnectTimeout == 0 {
		options.ConnectTimeout = time.Second
	}
	return &Upgrader{
		System:  f.system,
		Paths:   f.paths,
		Options: options,
		out:     options.Out,
	}
}

// upgradeFixture is an installation whose binary is a runnable fake agent, so
// the upgrader's "ask the binary for its version" step works.
func upgradeFixture(t *testing.T) *fixture {
	t.Helper()
	f := installedFixture(t)
	if err := os.WriteFile(f.paths.Binary(), fakeAgentBinary(t, "0.1.0"), 0o755); err != nil {
		t.Fatalf("cannot prepare the fixture binary: %v", err)
	}
	// A restart makes the agent reconnect, which the upgrader detects by the
	// configuration being rewritten.
	f.system.configAfterStart = nil
	return f
}

func TestUpgradeSwapsTheBinaryAndKeepsThePrevious(t *testing.T) {
	f := upgradeFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")},
		map[string]int64{"LICENSE": 0o644},
	))
	// A restart is what makes the agent reconnect; here that means rewriting
	// the configuration, which is the evidence the upgrader waits for.
	f.system.configAfterStart = func() { f.writeConfiguration(2) }

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest, ConnectTimeout: 5 * time.Second,
	}).Run(context.Background())
	if err != nil {
		t.Fatalf("upgrade failed: %v\n%s", err, f.output.String())
	}

	installed, readErr := os.ReadFile(f.paths.Binary())
	if readErr != nil {
		t.Fatalf("cannot read the installed binary: %v", readErr)
	}
	if !bytes.Equal(installed, fakeAgentBinary(t, "0.2.0")) {
		t.Error("the new binary was not installed")
	}
	previous, readErr := os.ReadFile(f.paths.PreviousBinary())
	if readErr != nil {
		t.Fatalf("the previous binary was not kept: %v", readErr)
	}
	if !bytes.Equal(previous, fakeAgentBinary(t, "0.1.0")) {
		t.Error("the kept binary is not the one that was replaced")
	}
	// The credential is never touched. An upgrade that required re-enrolment
	// would not be an upgrade path.
	if _, statErr := os.Stat(f.paths.Credential()); statErr != nil {
		t.Errorf("the upgrade disturbed the credential: %v", statErr)
	}
	if !strings.Contains(f.output.String(), "Upgraded to 0.2.0") {
		t.Errorf("the upgrade did not report the new version:\n%s", f.output.String())
	}
}

func TestUpgradeRefusesADigestMismatchBeforeTouchingAnything(t *testing.T) {
	f := upgradeFixture(t)
	archive, _ := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: strings.Repeat("0", 64),
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade proceeded with a digest mismatch")
	}
	if !strings.Contains(err.Error(), "does not match the digest") {
		t.Errorf("the refusal does not say what is wrong: %v", err)
	}
	// Nothing moved. "Nothing has been changed" is in the message, and it has
	// to be true.
	installed, _ := os.ReadFile(f.paths.Binary())
	if !bytes.Equal(installed, fakeAgentBinary(t, "0.1.0")) {
		t.Error("a refused upgrade replaced the binary")
	}
	if _, statErr := os.Stat(f.paths.PreviousBinary()); statErr == nil {
		t.Error("a refused upgrade moved the running binary aside")
	}
	if len(f.system.recorded()) != 0 {
		t.Errorf("a refused upgrade ran privileged operations: %v", f.system.recorded())
	}
}

func TestUpgradeRequiresAWellFormedDigest(t *testing.T) {
	f := upgradeFixture(t)
	archive, _ := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))

	for _, bad := range []string{"", "abc", strings.Repeat("z", 64)} {
		err := f.upgrader(UpgradeOptions{
			ArchivePath: archive, ExpectedSHA256: bad,
		}).Run(context.Background())
		if err == nil {
			t.Errorf("upgrade accepted %q as a digest", bad)
		}
	}
}

func TestUpgradeRollsBackWhenTheNewBinaryDoesNotReconnect(t *testing.T) {
	f := upgradeFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))
	// Nothing rewrites the configuration, so the upgraded agent never proves
	// it reconnected.
	f.system.journal = "hcc-agent[1]: msg=\"the controller rejected this agent's credential\""

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest, ConnectTimeout: 300 * time.Millisecond,
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade reported success without a reconnection")
	}
	if !strings.Contains(err.Error(), "rolled back") {
		t.Errorf("the failure does not say it rolled back: %v", err)
	}

	restored, readErr := os.ReadFile(f.paths.Binary())
	if readErr != nil {
		t.Fatalf("no binary after the rollback: %v", readErr)
	}
	if !bytes.Equal(restored, fakeAgentBinary(t, "0.1.0")) {
		t.Error("the previous binary was not restored")
	}
	calls := strings.Join(f.system.recorded(), "\n")
	// Restarted twice: once with the new binary, once with the restored one.
	if strings.Count(calls, "systemctl restart hcc-agent") != 2 {
		t.Errorf("expected two restarts (upgrade, then rollback); calls:\n%s", calls)
	}
	if !strings.Contains(f.output.String(), "restored the previous binary") {
		t.Errorf("the rollback was not reported:\n%s", f.output.String())
	}
}

func TestUpgradeRollsBackWhenTheServiceWillNotRestart(t *testing.T) {
	f := upgradeFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))
	f.system.systemctlErr["restart "+ServiceName] = errors.New("job for hcc-agent.service failed")

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest,
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade reported success with a failing restart")
	}
	restored, _ := os.ReadFile(f.paths.Binary())
	if !bytes.Equal(restored, fakeAgentBinary(t, "0.1.0")) {
		t.Error("the previous binary was not restored after a failed restart")
	}
}

func TestUpgradeRefusesAnArchiveWithoutTheAgent(t *testing.T) {
	f := upgradeFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"LICENSE": []byte("x"), "README.md": []byte("y")}, nil,
	))

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest,
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade accepted an archive with no agent in it")
	}
	if !strings.Contains(err.Error(), "no member named hcc-agent") {
		t.Errorf("the refusal does not say what is missing: %v", err)
	}
}

func TestUpgradeDryRunChangesNothing(t *testing.T) {
	f := upgradeFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))

	if err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest, DryRun: true,
	}).Run(context.Background()); err != nil {
		t.Fatalf("the dry run failed: %v\n%s", err, f.output.String())
	}

	installed, _ := os.ReadFile(f.paths.Binary())
	if !bytes.Equal(installed, fakeAgentBinary(t, "0.1.0")) {
		t.Error("the dry run replaced the binary")
	}
	if len(f.system.recorded()) != 0 {
		t.Errorf("the dry run ran privileged operations: %v", f.system.recorded())
	}
	if !strings.Contains(f.output.String(), "Nothing above has been done") {
		t.Error("the dry run did not say it had changed nothing")
	}
}

func TestUpgradeRefusesWithoutAnInstallation(t *testing.T) {
	f := newFixture(t)
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest,
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade proceeded with nothing installed")
	}
	if !strings.Contains(err.Error(), "no installation to upgrade") {
		t.Errorf("the refusal does not say what is missing: %v", err)
	}
}

// --- Archive extraction safety ---------------------------------------------

func TestExtractAgentBinaryRefusesAnEscapingMember(t *testing.T) {
	directory := t.TempDir()
	// The member is *named* so that its base name is hcc-agent, which is how
	// a naive extractor is steered into writing outside the target.
	path, _ := writeArchive(t, directory, buildArchive(t,
		map[string][]byte{"../../etc/cron.d/hcc-agent": []byte("* * * * * root id\n")}, nil,
	))

	_, err := extractAgentBinary(path)
	if err == nil {
		t.Fatal("extraction accepted a path-traversal member")
	}
	if !strings.Contains(err.Error(), "a release archive is flat") {
		t.Errorf("the refusal does not explain itself: %v", err)
	}
	// And nothing was written anywhere. The extractor never uses a path from
	// the archive, which is what makes this structural rather than a check.
	if _, statErr := os.Stat("/etc/cron.d/hcc-agent"); statErr == nil {
		t.Fatal("the extractor wrote outside the target directory")
	}
}

func TestExtractAgentBinaryRefusesALink(t *testing.T) {
	directory := t.TempDir()
	var raw bytes.Buffer
	compressed := gzip.NewWriter(&raw)
	archive := tar.NewWriter(compressed)
	if err := archive.WriteHeader(&tar.Header{
		Name:     "hcc-agent",
		Typeflag: tar.TypeSymlink,
		Linkname: "/etc/shadow",
		Mode:     0o777,
	}); err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	_ = archive.Close()
	_ = compressed.Close()
	path, _ := writeArchive(t, directory, raw.Bytes())

	_, err := extractAgentBinary(path)
	if err == nil {
		t.Fatal("extraction accepted a symlink as the agent binary")
	}
	if !strings.Contains(err.Error(), "as a link") {
		t.Errorf("the refusal does not say it was a link: %v", err)
	}
}

func TestExtractAgentBinaryRefusesSomethingThatIsNotAnArchive(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "not-an-archive.tar.gz")
	if err := os.WriteFile(path, []byte("this is not gzip"), 0o644); err != nil {
		t.Fatalf("cannot write the fixture: %v", err)
	}

	if _, err := extractAgentBinary(path); err == nil {
		t.Fatal("extraction accepted something that is not a gzip stream")
	}
}

func TestExtractAgentBinaryBoundsADecompressionBomb(t *testing.T) {
	directory := t.TempDir()
	// A tar whose header claims a huge member, inside a gzip stream that is
	// tiny. The bound is what stops a corrupt or hostile download from being
	// the machine's memory.
	var raw bytes.Buffer
	compressed := gzip.NewWriter(&raw)
	archive := tar.NewWriter(compressed)
	if err := archive.WriteHeader(&tar.Header{
		Name: "hcc-agent", Typeflag: tar.TypeReg, Mode: 0o755, Size: MaxArchiveBytes + 1024,
	}); err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	if _, err := io.CopyN(archive, zeroReader{}, MaxArchiveBytes+1024); err != nil {
		t.Fatalf("cannot build the archive: %v", err)
	}
	_ = archive.Close()
	_ = compressed.Close()
	path, _ := writeArchive(t, directory, raw.Bytes())

	_, err := extractAgentBinary(path)
	if err == nil {
		t.Fatal("extraction read an unbounded member")
	}
}

type zeroReader struct{}

func (zeroReader) Read(buffer []byte) (int, error) {
	for index := range buffer {
		buffer[index] = 0
	}
	return len(buffer), nil
}

func TestVerifyELF(t *testing.T) {
	valid := fakeELF()

	if err := verifyELF(valid); err != nil {
		t.Errorf("a valid x86-64 ELF was refused: %v", err)
	}
	if err := verifyELF([]byte("#!/bin/sh\n")); err == nil {
		t.Error("a shell script was accepted as an ELF object")
	}

	// An aarch64 binary in an archive this amd64 build is installing. Caught
	// here rather than by execve after the swap, where the service is already
	// down and systemd's "Exec format error" is three layers from the cause.
	foreign := append([]byte(nil), valid...)
	foreign[18] = 0xB7
	foreign[19] = 0x00
	if GOARCH() == "amd64" {
		if err := verifyELF(foreign); err == nil {
			t.Error("an aarch64 binary was accepted on amd64")
		} else if !strings.Contains(err.Error(), "ELF machine") {
			t.Errorf("the refusal does not name the mismatch: %v", err)
		}
	}
}

func TestDigestFileRejectsAnOversizedArchive(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "huge.tar.gz")
	file, err := os.Create(path)
	if err != nil {
		t.Fatalf("cannot create the fixture: %v", err)
	}
	// Sparse, so this costs no disk: the bound is on bytes read, not on
	// blocks allocated.
	if err := file.Truncate(MaxArchiveBytes + 1); err != nil {
		t.Fatalf("cannot size the fixture: %v", err)
	}
	_ = file.Close()

	if _, _, err := digestFile(path); err == nil {
		t.Fatal("an oversized archive was accepted")
	}
}

func TestUpgradeRefusesWithoutRoot(t *testing.T) {
	f := upgradeFixture(t)
	f.system.euid = 1000
	archive, digest := writeArchive(t, f.root, buildArchive(t,
		map[string][]byte{"hcc-agent": fakeAgentBinary(t, "0.2.0"), "LICENSE": []byte("x")}, nil,
	))

	err := f.upgrader(UpgradeOptions{
		ArchivePath: archive, ExpectedSHA256: digest,
	}).Run(context.Background())
	if err == nil {
		t.Fatal("upgrade proceeded as an ordinary user")
	}
	if !strings.Contains(err.Error(), "needs root") {
		t.Errorf("the refusal does not mention root: %v", err)
	}
}

// A regression guard with no assertion to make about behaviour: there must be
// no way to skip the digest. The trust model is that the controller, not the
// download origin, vouches for the binary — and an optional model is the one
// that is off when it matters.
func TestUpgradeOptionsOfferNoWayToSkipVerification(t *testing.T) {
	rendered := fmt.Sprintf("%+v", UpgradeOptions{})
	for _, forbidden := range []string{"Skip", "Insecure", "NoVerify", "Unsafe"} {
		if strings.Contains(rendered, forbidden) {
			t.Errorf("UpgradeOptions has a field containing %q", forbidden)
		}
	}
}
