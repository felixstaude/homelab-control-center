package updater_test

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/updater"
)

// The contract's deterministic test key. Its private half is the ASCII bytes
// of "hcc-contract-test-key-do-not-use" — see hcc/cli/contracts.py — so the
// same key signs here and there, and a signature this agent accepts is one
// the controller's verifier accepts too.
var contractSeed = []byte("hcc-contract-test-key-do-not-use")

const (
	amd64 = "linux/amd64"
	arm64 = "linux/arm64"
)

func publicKey(t *testing.T) string {
	t.Helper()
	private := ed25519.NewKeyFromSeed(contractSeed)
	return base64.StdEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
}

func trustStore(t *testing.T) *release.TrustStore {
	t.Helper()
	store, err := release.NewTrustStore([]string{publicKey(t)})
	if err != nil {
		t.Fatalf("cannot build the trust store: %v", err)
	}
	return store
}

func sign(t *testing.T, payload []byte, seed []byte) string {
	t.Helper()
	private := ed25519.NewKeyFromSeed(seed)
	digest := sha256.Sum256(private.Public().(ed25519.PublicKey))
	document := map[string]string{
		"algorithm": "ed25519",
		"key_id":    hex.EncodeToString(digest[:])[:release.KeyIDLength],
		"signature": base64.StdEncoding.EncodeToString(ed25519.Sign(private, payload)),
	}
	encoded, err := json.Marshal(document)
	if err != nil {
		t.Fatalf("cannot encode the signature: %v", err)
	}
	return string(encoded)
}

// archive is the payload a test "downloads". Its content does not matter
// beyond its digest: the installer is injected, so nothing unpacks it.
var archive = []byte("not really a tar.gz, and it does not need to be")

func archiveDigest() string {
	sum := sha256.Sum256(archive)
	return hex.EncodeToString(sum[:])
}

type manifestOptions struct {
	channel  string
	platform string
	digest   string
	size     int64
	version  string
}

func manifestFor(t *testing.T, options manifestOptions) string {
	t.Helper()
	if options.channel == "" {
		options.channel = "stable"
	}
	if options.platform == "" {
		options.platform = amd64
	}
	if options.digest == "" {
		options.digest = archiveDigest()
	}
	if options.size == 0 {
		options.size = int64(len(archive))
	}
	if options.version == "" {
		options.version = "0.1.1"
	}
	document := map[string]any{
		"schema_version":    2,
		"recommended":       options.version,
		"minimum_supported": "0.1.0",
		"channels":          map[string]string{options.channel: options.version},
		"releases": map[string]any{
			options.version: map[string]any{
				"channel":           options.channel,
				"protocol_versions": []int{1, 2},
				"platforms": map[string]any{
					options.platform: map[string]any{
						"archive":    "hcc-agent.tar.gz",
						"url":        "https://example.com/hcc-agent.tar.gz",
						"sha256":     options.digest,
						"size_bytes": options.size,
						"tier":       "supported",
					},
				},
			},
		},
	}
	encoded, err := json.Marshal(document)
	if err != nil {
		t.Fatalf("cannot encode the manifest: %v", err)
	}
	return string(encoded)
}

type recorder struct {
	progress []string
	details  []string
	result   string
	version  string
	failure  string
	calls    int
	failWith error
}

func (r *recorder) Progress(_ context.Context, state, detail string) error {
	r.calls++
	r.progress = append(r.progress, state)
	r.details = append(r.details, detail)
	return r.failWith
}

func (r *recorder) Result(_ context.Context, state, resultingVersion, failure string) error {
	r.calls++
	r.result = state
	r.version = resultingVersion
	r.failure = failure
	return r.failWith
}

type harness struct {
	updater     *updater.Updater
	reporter    *recorder
	fetched     []string
	upgraded    []install.UpgradeOptions
	upgradeErr  error
	fetchErr    error
	fetchBody   []byte
	stagingDir  string
	seenArchive []byte
}

func newHarness(t *testing.T, mutate func(*updater.Options)) *harness {
	t.Helper()
	h := &harness{reporter: &recorder{}, fetchBody: archive, stagingDir: t.TempDir()}
	options := updater.Options{
		TrustStore:       trustStore(t),
		Policy:           updater.Policy{AutoUpdate: true, Channel: "stable"},
		Platform:         amd64,
		InstalledVersion: "0.1.0",
		StagingDir:       h.stagingDir,
		Now:              func() time.Time { return time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC) },
		Fetch: func(_ context.Context, url string) (io.ReadCloser, error) {
			h.fetched = append(h.fetched, url)
			if h.fetchErr != nil {
				return nil, h.fetchErr
			}
			return io.NopCloser(strings.NewReader(string(h.fetchBody))), nil
		},
		Upgrade: func(_ context.Context, o install.UpgradeOptions) error {
			h.upgraded = append(h.upgraded, o)
			// Read what was staged, so a test can assert the bytes handed to
			// the installer are the bytes that were verified.
			if payload, err := os.ReadFile(o.ArchivePath); err == nil {
				h.seenArchive = payload
			}
			if o.OnStep != nil {
				o.OnStep(install.StepVerifying, "digest verified")
				o.OnStep(install.StepStaged, "extracted")
				o.OnStep(install.StepInstalling, "replacing")
				o.OnStep(install.StepRestarting, "restarted")
				o.OnStep(install.StepVerifyingHealth, "waiting")
			}
			return h.upgradeErr
		},
	}
	if mutate != nil {
		mutate(&options)
	}
	h.updater = updater.New(options)
	return h
}

type instructionOptions struct {
	metadata       string
	signature      string
	version        string
	platform       string
	expiresAt      string
	allowDowngrade bool
}

func instructionFor(t *testing.T, options instructionOptions) protocol.UpdateInstruction {
	t.Helper()
	if options.metadata == "" {
		options.metadata = manifestFor(t, manifestOptions{})
	}
	if options.signature == "" {
		options.signature = sign(t, []byte(options.metadata), contractSeed)
	}
	if options.version == "" {
		options.version = "0.1.1"
	}
	if options.platform == "" {
		options.platform = amd64
	}
	if options.expiresAt == "" {
		options.expiresAt = "2026-10-14T14:00:00Z"
	}
	return protocol.UpdateInstruction{
		V:              protocol.Version,
		Type:           protocol.TypeUpdateInstruction,
		InstructionID:  "11111111-2222-3333-4444-555555555555",
		ToVersion:      options.version,
		Platform:       options.platform,
		ExpiresAt:      options.expiresAt,
		Metadata:       options.metadata,
		Signature:      options.signature,
		AllowDowngrade: options.allowDowngrade,
	}
}

// ---------------------------------------------------------------------------
// The happy path
// ---------------------------------------------------------------------------

func TestAValidInstructionIsApplied(t *testing.T) {
	h := newHarness(t, nil)

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("a valid instruction was not applied: %v", err)
	}

	if h.reporter.result != protocol.StateSucceeded {
		t.Errorf("result is %q, want succeeded (failure: %s)", h.reporter.result, h.reporter.failure)
	}
	if h.reporter.version != "0.1.1" {
		t.Errorf("reported version is %q", h.reporter.version)
	}
	if len(h.upgraded) != 1 {
		t.Fatalf("the installer was called %d times", len(h.upgraded))
	}
	if h.upgraded[0].ExpectedSHA256 != archiveDigest() {
		t.Error("the installer was not given the digest from the signed metadata")
	}
}

func TestTheReportedStatesAreInOrder(t *testing.T) {
	// The sequence the interface renders as progress. Asserted as a whole
	// because a state reported out of order is refused by the controller's
	// transition table, which would turn a working update into a failed job.
	h := newHarness(t, nil)

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("apply: %v", err)
	}

	want := []string{
		protocol.StateDownloading,
		protocol.StateVerifying,
		protocol.StateStaged,
		protocol.StateInstalling,
		protocol.StateRestarting,
		protocol.StateVerifyingHealth,
	}
	if len(h.reporter.progress) != len(want) {
		t.Fatalf("progress is %v, want %v", h.reporter.progress, want)
	}
	for index, state := range want {
		if h.reporter.progress[index] != state {
			t.Errorf("progress[%d] is %q, want %q", index, h.reporter.progress[index], state)
		}
	}
}

func TestTheInstallerReceivesExactlyWhatWasVerified(t *testing.T) {
	// The bytes whose digest matched the signed metadata are the bytes the
	// installer unpacks. A download verified and then re-fetched, or staged
	// to one path and installed from another, would make the verification
	// decorative.
	h := newHarness(t, nil)

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if string(h.seenArchive) != string(archive) {
		t.Error("the installer was handed different bytes from the ones verified")
	}
	if len(h.fetched) != 1 {
		t.Errorf("the archive was fetched %d times", len(h.fetched))
	}
}

func TestTheStagedArchiveIsRemoved(t *testing.T) {
	// A few megabytes left on a NAS, and a puzzle for whoever finds it.
	h := newHarness(t, nil)

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("apply: %v", err)
	}

	entries, err := os.ReadDir(h.stagingDir)
	if err != nil {
		t.Fatalf("cannot read the staging directory: %v", err)
	}
	if len(entries) != 0 {
		t.Errorf("the staging directory still holds %d entries", len(entries))
	}
}

// ---------------------------------------------------------------------------
// The gates. Each is tested by trying to get past it.
// ---------------------------------------------------------------------------

func TestAutomaticUpdatesAreRefusedWhenNotEnabled(t *testing.T) {
	// The single most important default: an agent that has not been told it
	// may update itself does not, whatever the controller says.
	h := newHarness(t, func(o *updater.Options) { o.Policy.AutoUpdate = false })

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "not enabled on this host") {
		t.Errorf("the refusal does not explain itself: %v", err)
	}
	if len(h.fetched) != 0 {
		t.Error("a refused instruction caused a download")
	}
	if len(h.upgraded) != 0 {
		t.Error("a refused instruction reached the installer")
	}
}

func TestAnExpiredInstructionIsRefused(t *testing.T) {
	// A machine powered off for a week refuses by itself. The controller
	// cannot reach a disconnected host to withdraw anything, so this check
	// being on the agent is what makes the expiry real.
	h := newHarness(t, func(o *updater.Options) {
		o.Now = func() time.Time { return time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC) }
	})

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "expired") {
		t.Errorf("the refusal does not mention expiry: %v", err)
	}
	if len(h.fetched) != 0 {
		t.Error("an expired instruction caused a download")
	}
}

func TestAMalformedExpiryIsRefused(t *testing.T) {
	h := newHarness(t, nil)

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{expiresAt: "next Tuesday"}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
}

func TestAnUnsignedInstructionIsRefused(t *testing.T) {
	// The whole point of the trust model. There is no checksum-only
	// fallback, and the refusal says so.
	h := newHarness(t, nil)

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{signature: " "}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "not authentic") {
		t.Errorf("the refusal does not mention authenticity: %v", err)
	}
	if !strings.Contains(err.Error(), "no") || !strings.Contains(err.Error(), "fallback") {
		t.Errorf("the refusal does not state that there is no fallback: %v", err)
	}
	if len(h.fetched) != 0 {
		t.Error("an unsigned instruction caused a download")
	}
}

func TestAnInstructionSignedByAnUntrustedKeyIsRefused(t *testing.T) {
	stranger := []byte("hcc-contract-stranger-key-nottru")
	h := newHarness(t, nil)
	metadata := manifestFor(t, manifestOptions{})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), stranger),
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "untrusted_key") {
		t.Errorf("the refusal does not name the outcome: %v", err)
	}
}

func TestTamperedMetadataIsRefused(t *testing.T) {
	// The attack the signature exists to stop: a controller substituting a
	// digest. The signature is over the original bytes, so the altered
	// document does not verify.
	h := newHarness(t, nil)
	original := manifestFor(t, manifestOptions{})
	signature := sign(t, []byte(original), contractSeed)
	tampered := strings.Replace(original, archiveDigest(), strings.Repeat("f", 64), 1)
	if tampered == original {
		t.Fatal("the test did not actually alter the metadata")
	}

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{metadata: tampered, signature: signature}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "invalid") {
		t.Errorf("the refusal does not name the outcome: %v", err)
	}
}

func TestAnEmptyTrustStoreRefusesEverything(t *testing.T) {
	// The state of a build with no key stamped in. It must refuse rather
	// than fall back to trusting the controller.
	h := newHarness(t, func(o *updater.Options) { o.TrustStore = nil })

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
}

func TestAnInstructionForAnotherPlatformIsRefused(t *testing.T) {
	h := newHarness(t, nil)

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{platform: arm64}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), arm64) || !strings.Contains(err.Error(), amd64) {
		t.Errorf("the refusal does not name both platforms: %v", err)
	}
}

func TestAReleaseWithNoArchiveForThisPlatformIsRefused(t *testing.T) {
	// The instruction's platform matches, and the signed metadata publishes
	// nothing for it. Caught from the *signed* document rather than the
	// instruction, so a controller cannot claim an archive exists.
	h := newHarness(t, nil)
	metadata := manifestFor(t, manifestOptions{platform: arm64})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "publishes no archive") {
		t.Errorf("unexpected refusal: %v", err)
	}
}

func TestAPreReleaseIsRefusedOnTheStableChannel(t *testing.T) {
	// And the channel is read from the **signed** metadata, so a controller
	// cannot relabel a pre-release as stable: the label is inside the
	// signature.
	h := newHarness(t, nil)
	metadata := manifestFor(t, manifestOptions{channel: "beta", version: "0.2.0-beta.1"})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
			version:   "0.2.0-beta.1",
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "beta channel") {
		t.Errorf("the refusal does not name the channel: %v", err)
	}
}

func TestABetaHostAcceptsABeta(t *testing.T) {
	h := newHarness(t, func(o *updater.Options) { o.Policy.Channel = "beta" })
	metadata := manifestFor(t, manifestOptions{channel: "beta", version: "0.2.0-beta.1"})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
			version:   "0.2.0-beta.1",
		}),
		h.reporter,
	)

	if err != nil {
		t.Fatalf("a beta host refused a beta: %v", err)
	}
}

func TestABetaHostStillAcceptsAStableRelease(t *testing.T) {
	// Somebody who opted into pre-releases should not stop receiving
	// ordinary ones. Mirrors permitted_channels on the controller.
	h := newHarness(t, func(o *updater.Options) { o.Policy.Channel = "beta" })

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("a beta host refused a stable release: %v", err)
	}
}

func TestAnUnknownChannelFailsClosed(t *testing.T) {
	// A typo in a stored policy must not widen what a host accepts.
	h := newHarness(t, func(o *updater.Options) { o.Policy.Channel = "nightly" })
	metadata := manifestFor(t, manifestOptions{channel: "beta", version: "0.2.0-beta.1"})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
			version:   "0.2.0-beta.1",
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("an unknown channel widened what the host accepts: %v", err)
	}
}

func TestAPinnedHostRefusesAnythingElse(t *testing.T) {
	h := newHarness(t, func(o *updater.Options) { o.Policy.PinnedVersion = "0.1.5" })

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "pinned to 0.1.5") {
		t.Errorf("the refusal does not name the pin: %v", err)
	}
}

func TestAPinnedHostAcceptsItsPin(t *testing.T) {
	h := newHarness(t, func(o *updater.Options) { o.Policy.PinnedVersion = "0.1.1" })

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("a pinned host refused its own pin: %v", err)
	}
}

func TestADowngradeIsRefusedWithoutAuthorization(t *testing.T) {
	h := newHarness(t, func(o *updater.Options) { o.InstalledVersion = "0.2.0" })
	metadata := manifestFor(t, manifestOptions{version: "0.1.1"})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "downgrade") {
		t.Errorf("the refusal does not say it is a downgrade: %v", err)
	}
}

func TestAnAuthorizedDowngradeIsApplied(t *testing.T) {
	h := newHarness(t, func(o *updater.Options) { o.InstalledVersion = "0.2.0" })
	metadata := manifestFor(t, manifestOptions{version: "0.1.1"})

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:       metadata,
			signature:      sign(t, []byte(metadata), contractSeed),
			allowDowngrade: true,
		}),
		h.reporter,
	)

	if err != nil {
		t.Fatalf("an authorized downgrade was refused: %v", err)
	}
}

func TestInstallingTheRunningVersionIsRefused(t *testing.T) {
	// Not a failure and not worth a restart. The controller plans against the
	// version a host reported, which can be stale by the time the
	// instruction arrives.
	h := newHarness(t, func(o *updater.Options) { o.InstalledVersion = "0.1.1" })

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "already runs") {
		t.Errorf("unexpected refusal: %v", err)
	}
}

func TestAVersionThatIsNotAVersionIsRefused(t *testing.T) {
	h := newHarness(t, nil)

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{version: "latest"}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
}

func TestUnusableSignedMetadataIsRefused(t *testing.T) {
	// Correctly signed, and not a manifest. The signature says the publisher
	// produced these bytes; it does not say they mean anything.
	h := newHarness(t, nil)
	metadata := `{"not": "a manifest"}`

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
		}),
		h.reporter,
	)

	if !updater.IsRefusal(err) {
		t.Fatalf("expected a refusal, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// The download
// ---------------------------------------------------------------------------

func TestADigestMismatchFailsAndInstallsNothing(t *testing.T) {
	// The step that makes the download origin untrusted: a substituted
	// archive fails here whatever the mirror served.
	h := newHarness(t, nil)
	// Exactly the same length, one byte different — so the size check cannot
	// fire first and this isolates the digest. Written by flipping a byte
	// rather than by typing a string of the right length, which is how the
	// first version of this test ended up one byte short and asserting the
	// wrong refusal.
	substituted := append([]byte(nil), archive...)
	substituted[0] ^= 0xFF
	h.fetchBody = substituted

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("a substituted archive was accepted")
	}
	if len(substituted) != len(archive) {
		t.Fatalf("the substitute is %d bytes and the original is %d", len(substituted), len(archive))
	}
	if updater.IsRefusal(err) {
		t.Error("a digest mismatch is a failure, not a refusal")
	}
	if !strings.Contains(err.Error(), "not what the publisher signed") {
		t.Errorf("the error does not explain itself: %v", err)
	}
	if len(h.upgraded) != 0 {
		t.Error("a mismatched archive reached the installer")
	}
	if h.reporter.result != protocol.StateFailed {
		t.Errorf("result is %q", h.reporter.result)
	}
}

func TestASizeMismatchIsRefusedBeforeTheDigest(t *testing.T) {
	// A length that disagrees with the signed metadata is reported as such,
	// rather than as a digest mismatch — which would send somebody looking
	// for tampering when the download was truncated.
	h := newHarness(t, nil)
	h.fetchBody = archive[:10]

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("a truncated archive was accepted")
	}
	if !strings.Contains(err.Error(), "bytes and the signed metadata says") {
		t.Errorf("the error does not name the size disagreement: %v", err)
	}
}

func TestAFailedDownloadIsReportedAndInstallsNothing(t *testing.T) {
	h := newHarness(t, nil)
	h.fetchErr = errors.New("the origin is unreachable")

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("a failed download was treated as success")
	}
	if len(h.upgraded) != 0 {
		t.Error("the installer ran after a failed download")
	}
	if h.reporter.result != protocol.StateFailed {
		t.Errorf("result is %q", h.reporter.result)
	}
	if !strings.Contains(h.reporter.failure, "unreachable") {
		t.Errorf("the reported failure does not carry the reason: %q", h.reporter.failure)
	}
}

func TestAnOversizedDownloadIsRefused(t *testing.T) {
	// Bounded while reading rather than by trusting Content-Length, because
	// the origin chooses the header and the origin is not trusted.
	h := newHarness(t, nil)
	h.fetchBody = make([]byte, updater.MaxDownloadBytes+1)

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("an oversized download was accepted")
	}
	if !strings.Contains(err.Error(), "exceeded") {
		t.Errorf("the error does not mention the bound: %v", err)
	}
}

func TestADownloadProgressReportComesBeforeTheDownload(t *testing.T) {
	// So an interface shows "downloading" while it is happening rather than
	// after it finished. Simulated progress — a bar that moves on a timer —
	// is worse than none, because somebody will believe it.
	var progressWhenFetchBegan []string
	reported := &recorder{}
	h := newHarness(t, func(o *updater.Options) {
		fetch := o.Fetch
		o.Fetch = func(ctx context.Context, url string) (io.ReadCloser, error) {
			// Snapshotted inside the fetch, so this observes the order rather
			// than inferring it from the final list.
			progressWhenFetchBegan = append([]string(nil), reported.progress...)
			return fetch(ctx, url)
		}
	})

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), reported); err != nil {
		t.Fatalf("apply: %v", err)
	}

	if len(progressWhenFetchBegan) != 1 || progressWhenFetchBegan[0] != protocol.StateDownloading {
		t.Errorf("progress when the fetch began was %v, want [downloading]", progressWhenFetchBegan)
	}
}

// ---------------------------------------------------------------------------
// Installation failures
// ---------------------------------------------------------------------------

func TestAFailedInstallIsReported(t *testing.T) {
	h := newHarness(t, nil)
	h.upgradeErr = errors.New("the new binary did not reconnect")

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("a failed install was treated as success")
	}
	if h.reporter.result != protocol.StateFailed {
		t.Errorf("result is %q, want failed", h.reporter.result)
	}
	// The version reported is what is still running, not the target: a job
	// claiming the new version while the host runs the old one is the one
	// outcome nobody can act on.
	if h.reporter.version != "0.1.0" {
		t.Errorf("reported version is %q, want the installed one", h.reporter.version)
	}
}

func TestARolledBackInstallIsReportedAsSuch(t *testing.T) {
	// "Failed" and "failed and rolled back" need different actions from an
	// operator: the second means the service is running again.
	h := newHarness(t, nil)
	h.upgradeErr = errors.New("health check failed; rolled back to the previous binary")

	err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter)

	if err == nil {
		t.Fatal("expected an error")
	}
	if h.reporter.result != protocol.StateRolledBack {
		t.Errorf("result is %q, want rolled_back", h.reporter.result)
	}
}

func TestAReportingFailureDoesNotAbortTheUpdate(t *testing.T) {
	// The controller losing sight of an install is bad. Abandoning a
	// half-finished install because a socket hiccuped is worse.
	h := newHarness(t, nil)
	h.reporter.failWith = errors.New("the socket is gone")

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), h.reporter); err != nil {
		t.Fatalf("a reporting failure aborted the update: %v", err)
	}
	if len(h.upgraded) != 1 {
		t.Error("the install did not happen")
	}
}

func TestANilReporterIsTolerated(t *testing.T) {
	// `hcc-agent upgrade` run by hand has no controller to report to.
	h := newHarness(t, nil)

	if err := h.updater.Apply(context.Background(), instructionFor(t, instructionOptions{}), nil); err != nil {
		t.Fatalf("apply with no reporter: %v", err)
	}
}

// ---------------------------------------------------------------------------
// The real fetcher's own rule
// ---------------------------------------------------------------------------

func TestTheRealFetcherRefusesPlaintext(t *testing.T) {
	// Not because the transport protects the bytes — the signed digest does
	// that — but because an agent that would fetch over plaintext is an agent
	// somebody will point at a plaintext mirror and then reason incorrectly
	// about.
	real := updater.New(updater.Options{TrustStore: trustStore(t)})
	_ = real

	// Exercised through a fresh updater with no Fetch injected, so the real
	// one is in place, and an instruction whose metadata names an http URL.
	metadata := strings.Replace(
		manifestFor(t, manifestOptions{}),
		"https://example.com/hcc-agent.tar.gz",
		"http://example.com/hcc-agent.tar.gz",
		1,
	)
	h := newHarness(t, func(o *updater.Options) { o.Fetch = nil })

	err := h.updater.Apply(
		context.Background(),
		instructionFor(t, instructionOptions{
			metadata:  metadata,
			signature: sign(t, []byte(metadata), contractSeed),
		}),
		h.reporter,
	)

	if err == nil {
		t.Fatal("a plaintext download was attempted")
	}
	if !strings.Contains(err.Error(), "https is required") {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestStagingDirFallsBackToTemp(t *testing.T) {
	// A read-only /etc is a legitimate configuration, and an update must not
	// fail because of it.
	directory := updater.StagingDirFor(install.Paths{Root: "/nonexistent-root-for-a-test"})

	if directory != os.TempDir() {
		t.Errorf("staging directory is %q, want the system temporary directory", directory)
	}
}

func TestStagingDirPrefersTheConfigurationDirectory(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(root+install.DefaultConfigDir, 0o755); err != nil {
		t.Fatalf("cannot create the configuration directory: %v", err)
	}

	directory := updater.StagingDirFor(install.Paths{Root: root})

	if directory != root+install.DefaultConfigDir {
		t.Errorf("staging directory is %q, want %q", directory, root+install.DefaultConfigDir)
	}
}

func TestRefusalsAreDistinguishableFromFailures(t *testing.T) {
	// The connection loop branches on this: a declined instruction is the
	// agent working correctly and should not be logged as an error.
	if updater.IsRefusal(errors.New("an ordinary error")) {
		t.Error("an ordinary error was reported as a refusal")
	}
	if updater.IsRefusal(nil) {
		t.Error("nil was reported as a refusal")
	}
	wrapped := fmt.Errorf("context: %w", &updater.Refusal{Reason: "because"})
	if !updater.IsRefusal(wrapped) {
		t.Error("a wrapped refusal was not recognised")
	}
}
