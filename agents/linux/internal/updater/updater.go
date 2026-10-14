// Package updater applies a controller-authorized update, on this agent's
// own terms.
//
// # The agent decides, not the controller
//
// This is the whole point of the package. The controller says "you are
// authorized to install 0.1.1"; this code decides whether to, and it can
// refuse for reasons the controller cannot override:
//
//  1. **Local policy.** Automatic updates off, a channel not opted into, a
//     version pin — any of those refuses the instruction. A compromised
//     control plane cannot install a version the host's own configuration
//     forbids.
//  2. **Expiry.** An instruction has a deadline. A machine powered off for a
//     week refuses by itself, because the controller cannot reach a
//     disconnected host to withdraw anything.
//  3. **Authenticity.** The release metadata carries a publisher signature
//     this agent verifies against a key stamped into its own binary. The
//     digest it then checks comes out of that signed document. A controller
//     that has been taken over can withhold updates or offer an older one —
//     availability and version choice are its job — but it cannot produce
//     metadata this agent accepts for a binary the publisher did not build.
//  4. **Architecture and direction.** A release with no archive for this
//     platform, or a step backwards without explicit authorization, is
//     refused.
//
// There is no flag that relaxes any of these, and in particular no fallback
// to checksum-only trust: a trust model with an escape hatch is the model
// that is off when it matters.
//
// # What it does not reimplement
//
// Extraction, the ELF check, the binary swap, the restart and the rollback all
// live in internal/install's [install.Upgrader], which an operator already
// drives by hand with `hcc-agent upgrade`. This package verifies, downloads,
// and then hands over — so the controller-driven path and the operator-driven
// path install identically, and the hardened archive reader has one
// implementation rather than two.
package updater

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/install"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// MaxDownloadBytes bounds a download.
//
// The same ceiling internal/install puts on an archive it reads. Enforced
// while *reading* rather than by trusting Content-Length, because a header is
// something the download origin chooses — and the origin is explicitly not
// trusted here.
const MaxDownloadBytes = install.MaxArchiveBytes

// DownloadTimeout bounds how long a download may take.
//
// Ten minutes. A 3 MB archive on a slow domestic uplink is a minute or two;
// ten leaves room for a bad day without letting a stalled transfer hold an
// instruction open until it expires, which would look to an operator like an
// update that never started.
const DownloadTimeout = 10 * time.Minute

// Refusal is an instruction this agent declined, and why.
//
// A distinct type because a refusal is not a failure: "automatic updates are
// disabled on this host" is the agent working correctly, and reporting it as
// a failed update would make a correctly-configured fleet look broken. The
// controller records it against the job either way, with this text.
type Refusal struct {
	Reason string
}

func (r *Refusal) Error() string { return r.Reason }

// Declined marks this as a refusal rather than a failure.
//
// Satisfies the one-method interface internal/agent defines for the same
// purpose, so the session loop can tell the two apart without importing this
// package — which would let it reach the installer, and it should not.
func (r *Refusal) Declined() bool { return true }

// refuse builds a [Refusal].
func refuse(format string, arguments ...any) error {
	return &Refusal{Reason: fmt.Sprintf(format, arguments...)}
}

// IsRefusal reports whether err is a declined instruction rather than a
// failed one.
func IsRefusal(err error) bool {
	var refusal *Refusal
	return errors.As(err, &refusal)
}

// Policy is what this host allows without being asked.
//
// Read from the agent's own configuration, **not** from the instruction. The
// controller proposes; this disposes.
type Policy struct {
	// AutoUpdate is whether a controller-driven update may proceed at all.
	//
	// False by default, which is the single most important default in the
	// feature: an agent that has not been told it may update itself does not.
	AutoUpdate bool
	// Channel is the most instability this host accepts: stable, beta, alpha.
	// Empty means stable.
	Channel string
	// PinnedVersion holds this host at one version. Empty means unpinned.
	PinnedVersion string
}

// Reporter is how the updater tells the controller where it has got to.
//
// An interface so the connection is not a dependency of the state machine:
// the tests drive a recorder and assert the exact sequence of states, which
// is the behaviour that matters and is not observable through a socket.
type Reporter interface {
	Progress(ctx context.Context, state, detail string) error
	Result(ctx context.Context, state, resultingVersion, failure string) error
}

// Options is everything the updater needs, all injectable.
type Options struct {
	// TrustStore holds the publisher keys this agent accepts. A nil or empty
	// store trusts nothing, which refuses every signed instruction — correct
	// for a build with no key stamped in.
	TrustStore *release.TrustStore
	// Policy is this host's own rules.
	Policy Policy
	// Platform is this agent's GOOS/GOARCH.
	Platform string
	// InstalledVersion is what this binary reports.
	InstalledVersion string
	// Paths locates the installation.
	Paths install.Paths
	// System is the privileged-operation surface, injected for testing.
	System install.System
	// Fetch retrieves a URL. Injected so a test needs no network.
	Fetch func(ctx context.Context, url string) (io.ReadCloser, error)
	// Upgrade installs a verified archive. Injected so a test can assert the
	// updater's own behaviour without replacing a real binary.
	Upgrade func(ctx context.Context, options install.UpgradeOptions) error
	// Now is the clock, injected so expiry is a test rather than a wait.
	Now func() time.Time
	// StagingDir is where the archive is downloaded. Empty means the system
	// temporary directory.
	StagingDir string
	Logger     *slog.Logger
}

// Updater applies instructions.
type Updater struct {
	options Options
}

// New builds an Updater, filling in the real implementations for anything the
// caller did not inject.
func New(options Options) *Updater {
	if options.Fetch == nil {
		options.Fetch = httpFetch
	}
	if options.Upgrade == nil {
		options.Upgrade = runUpgrade
	}
	if options.Now == nil {
		options.Now = time.Now
	}
	if options.System == nil {
		options.System = install.LinuxSystem{}
	}
	if options.Logger == nil {
		options.Logger = slog.Default()
	}
	return &Updater{options: options}
}

// Apply runs one instruction to a conclusion.
//
// Returns nil when the update succeeded. Returns a [Refusal] when this agent
// declined — which is not a failure — and an ordinary error when an attempt
// was made and did not work.
//
// Reports progress through reporter as it goes. A reporting failure does not
// abort the update: the controller losing sight of an install is bad, and
// abandoning a half-finished install because a socket hiccuped is worse.
func (u *Updater) Apply(
	ctx context.Context, instruction protocol.UpdateInstruction, reporter Reporter,
) error {
	logger := u.options.Logger.With("instruction", instruction.InstructionID)

	// --- 1. The gates, before anything is fetched ------------------------
	//
	// Order matters: every check that can be made without touching the
	// network is made first, so a refused instruction costs nothing and a
	// compromised controller cannot make this agent download anything by
	// sending an instruction it will decline.
	target, err := u.check(instruction)
	if err != nil {
		logger.Warn("the update instruction was declined", "reason", err.Error())
		// A refusal and a failure both report `failed` with the reason, and
		// that is deliberate rather than a gap: the protocol has no
		// "declined" state, and adding one would mean a schema change, a
		// migration and a new interface state for a case the reason already
		// explains in a sentence. "Automatic updates are not enabled on this
		// host" in the history is what somebody asking "why did this host not
		// update" actually needs.
		//
		// The distinction is kept in the *return*, so a caller — the
		// connection loop — can tell a declined instruction from a broken one
		// and log accordingly.
		u.report(ctx, reporter, func() error {
			return reporter.Result(
				ctx, protocol.StateFailed, u.options.InstalledVersion, err.Error(),
			)
		})
		return err
	}

	// --- 2. Download -----------------------------------------------------
	u.report(ctx, reporter, func() error {
		return reporter.Progress(ctx, protocol.StateDownloading,
			fmt.Sprintf("fetching %s (%d bytes)", target.Archive, target.SizeBytes))
	})

	archivePath, err := u.download(ctx, target)
	if err != nil {
		logger.Error("the update download failed", "error", err)
		u.report(ctx, reporter, func() error {
			return reporter.Result(ctx, protocol.StateFailed, u.options.InstalledVersion, err.Error())
		})
		return err
	}
	defer func() {
		// Removed whatever happened. A staged archive left behind is a few
		// megabytes on a NAS and a puzzle for whoever finds it.
		if err := os.Remove(archivePath); err != nil && !os.IsNotExist(err) {
			logger.Warn("could not remove the staged archive", "path", archivePath, "error", err)
		}
	}()

	// --- 3. Install, through the same code an operator drives by hand ----
	options := install.UpgradeOptions{
		ArchivePath:    archivePath,
		ExpectedSHA256: target.SHA256,
		Out:            io.Discard,
		OnStep: func(state, detail string) {
			u.report(ctx, reporter, func() error {
				return reporter.Progress(ctx, state, detail)
			})
		},
	}
	if err := u.options.Upgrade(ctx, options); err != nil {
		logger.Error("the update failed", "error", err)
		// Whether the previous binary was restored is the upgrader's business
		// and its error says so. The state reported distinguishes them,
		// because "failed and rolled back" and "failed" need different
		// actions from an operator.
		state := protocol.StateFailed
		if strings.Contains(err.Error(), "rolled back") || strings.Contains(err.Error(), "rolling back") {
			state = protocol.StateRolledBack
		}
		u.report(ctx, reporter, func() error {
			return reporter.Result(ctx, state, u.options.InstalledVersion, err.Error())
		})
		return err
	}

	logger.Info("the update succeeded", "version", instruction.ToVersion)
	u.report(ctx, reporter, func() error {
		return reporter.Result(ctx, protocol.StateSucceeded, instruction.ToVersion, "")
	})
	return nil
}

// check runs every gate that needs no network, and returns the artifact.
//
// Split out from [Updater.Apply] so the whole decision table is testable
// without a download, a filesystem or a clock.
func (u *Updater) check(instruction protocol.UpdateInstruction) (release.Platform, error) {
	// Expiry first: the cheapest check, and the one that matters for a host
	// that has been offline.
	expires, err := time.Parse(time.RFC3339, instruction.ExpiresAt)
	if err != nil {
		return release.Platform{}, refuse(
			"the instruction's expiry %q is not an RFC 3339 timestamp", instruction.ExpiresAt,
		)
	}
	now := u.options.Now()
	if now.After(expires) {
		return release.Platform{}, refuse(
			"the instruction expired at %s and it is now %s; this host may have been "+
				"offline. The controller will plan a new one against current metadata",
			expires.Format(time.RFC3339), now.Format(time.RFC3339),
		)
	}

	// Local policy. The controller cannot override this.
	if !u.options.Policy.AutoUpdate {
		return release.Platform{}, refuse(
			"automatic updates are not enabled on this host, so a " +
				"controller-authorized update will not be applied. Run " +
				"`hcc-agent upgrade` to install one by hand",
		)
	}

	targetVersion, err := version.Parse(instruction.ToVersion)
	if err != nil {
		return release.Platform{}, refuse(
			"the instruction names version %q, which is not a semantic version",
			instruction.ToVersion,
		)
	}

	if pin := u.options.Policy.PinnedVersion; pin != "" {
		pinned, err := version.Parse(pin)
		if err != nil {
			return release.Platform{}, refuse(
				"this host is pinned to %q, which is not a semantic version", pin,
			)
		}
		if !pinned.Equal(targetVersion) {
			return release.Platform{}, refuse(
				"this host is pinned to %s and the instruction names %s", pinned, targetVersion,
			)
		}
	}

	if instruction.Platform != "" && instruction.Platform != u.options.Platform {
		return release.Platform{}, refuse(
			"the instruction was planned for %s and this agent is %s",
			instruction.Platform, u.options.Platform,
		)
	}

	// Authenticity. Everything above could be decided from the instruction
	// alone; from here the *signed* document is the authority.
	signature := []byte(instruction.Signature)
	if strings.TrimSpace(instruction.Signature) == "" {
		signature = nil
	}
	verification := release.VerifyMetadata(
		[]byte(instruction.Metadata), signature, u.options.TrustStore,
	)
	if !verification.Authentic() {
		return release.Platform{}, refuse(
			"the release metadata is not authentic (%s): %s. An automatic update "+
				"needs a publisher signature this agent trusts; there is no "+
				"checksum-only fallback",
			verification.Status, verification.Detail,
		)
	}

	manifest, err := release.ParseManifest([]byte(instruction.Metadata))
	if err != nil {
		return release.Platform{}, refuse("the signed release metadata is unusable: %v", err)
	}

	// The channel gate reads the *signed* document, not the instruction: the
	// controller chooses which version to offer, and the publisher decides
	// which channel that version is in. A controller cannot relabel a
	// pre-release as stable, because the label is inside the signature.
	entry, ok := manifest.Releases[instruction.ToVersion]
	if !ok {
		return release.Platform{}, refuse(
			"the signed metadata has no entry for %s", instruction.ToVersion,
		)
	}
	if !channelPermitted(u.options.Policy.Channel, entry.Channel) {
		return release.Platform{}, refuse(
			"%s is in the %s channel and this host accepts %s",
			instruction.ToVersion, entry.Channel, channelOrDefault(u.options.Policy.Channel),
		)
	}

	// Direction. A downgrade needs the administrator to have said so, and the
	// instruction carries that rather than the agent assuming it.
	if installed, err := version.Parse(u.options.InstalledVersion); err == nil {
		if installed.Equal(targetVersion) {
			return release.Platform{}, refuse(
				"this host already runs %s", targetVersion,
			)
		}
		if targetVersion.LessThan(installed) && !instruction.AllowDowngrade {
			return release.Platform{}, refuse(
				"installing %s over %s is a downgrade, which needs explicit "+
					"authorization from an administrator",
				targetVersion, installed,
			)
		}
	}

	artifact, ok := manifest.PlatformFor(instruction.ToVersion, u.options.Platform)
	if !ok {
		return release.Platform{}, refuse(
			"%s publishes no archive for %s", instruction.ToVersion, u.options.Platform,
		)
	}
	if len(artifact.SHA256) != 64 {
		return release.Platform{}, refuse(
			"the signed metadata's digest for %s is not a SHA-256", u.options.Platform,
		)
	}
	return artifact, nil
}

// download fetches the archive and checks its digest before returning.
//
// The digest comes from the **signed** metadata, so this is the step that
// makes the download origin untrusted: a substituted archive fails here
// whatever the mirror served and whatever headers it sent.
func (u *Updater) download(ctx context.Context, target release.Platform) (string, error) {
	directory := u.options.StagingDir
	if directory == "" {
		directory = os.TempDir()
	}
	file, err := os.CreateTemp(directory, "hcc-agent-update-*.tar.gz")
	if err != nil {
		return "", fmt.Errorf("cannot create a staging file in %s: %w", directory, err)
	}
	path := file.Name()

	cleanup := func() {
		_ = file.Close()
		_ = os.Remove(path)
	}

	fetchCtx, cancel := context.WithTimeout(ctx, DownloadTimeout)
	defer cancel()

	body, err := u.options.Fetch(fetchCtx, target.URL)
	if err != nil {
		cleanup()
		return "", fmt.Errorf("cannot download %s: %w", target.URL, err)
	}
	defer func() { _ = body.Close() }()

	// Bounded while reading rather than by trusting Content-Length: a header
	// is something the origin chooses, and the origin is not trusted. One byte
	// over the limit is a refusal rather than a truncation, so a mismatch
	// cannot be mistaken for a corrupt archive.
	digest := sha256.New()
	limited := io.LimitReader(body, MaxDownloadBytes+1)
	written, err := io.Copy(io.MultiWriter(file, digest), limited)
	if closeErr := file.Close(); closeErr != nil && err == nil {
		err = closeErr
	}
	if err != nil {
		_ = os.Remove(path)
		return "", fmt.Errorf("the download failed after %d bytes: %w", written, err)
	}
	if written > MaxDownloadBytes {
		_ = os.Remove(path)
		return "", fmt.Errorf(
			"the download exceeded %d bytes, so it is not a release archive",
			MaxDownloadBytes,
		)
	}
	if target.SizeBytes > 0 && written != target.SizeBytes {
		_ = os.Remove(path)
		return "", fmt.Errorf(
			"the download is %d bytes and the signed metadata says %d",
			written, target.SizeBytes,
		)
	}

	actual := hex.EncodeToString(digest.Sum(nil))
	if actual != strings.ToLower(target.SHA256) {
		_ = os.Remove(path)
		return "", fmt.Errorf(
			"the downloaded archive's digest is %s and the signed metadata says %s. "+
				"The download was not what the publisher signed",
			actual, target.SHA256,
		)
	}
	return path, nil
}

// report runs a reporting call and swallows its error.
//
// The controller losing sight of an install is bad; abandoning a half-finished
// install because a socket hiccuped is worse. So a reporting failure is logged
// and the update continues — and the controller's instruction expiry is what
// eventually reconciles a job whose reports were lost.
func (u *Updater) report(ctx context.Context, reporter Reporter, call func() error) {
	if reporter == nil {
		return
	}
	if err := call(); err != nil {
		u.options.Logger.Warn("could not report update progress", "error", err)
	}
	_ = ctx
}

// channelOrDefault reads an empty channel as the most stable one.
func channelOrDefault(channel string) string {
	if channel == "" {
		return "stable"
	}
	return channel
}

// channelPermitted mirrors hcc.updates.compat.permitted_channels.
//
// "The most instability I accept", so a beta subscriber still takes a stable
// release. An unrecognised channel on either side fails closed to stable,
// because a typo must not widen what a host accepts.
func channelPermitted(accepted, releaseChannel string) bool {
	order := map[string]int{"stable": 0, "beta": 1, "alpha": 2}
	limit, ok := order[channelOrDefault(accepted)]
	if !ok {
		limit = 0
	}
	level, ok := order[channelOrDefault(releaseChannel)]
	if !ok {
		// A channel this agent does not know is treated as less stable than
		// any it does, so it is refused unless the host accepts everything.
		return false
	}
	return level <= limit
}

// httpFetch is the real download.
func httpFetch(ctx context.Context, url string) (io.ReadCloser, error) {
	// https only, with the same loopback exception the rest of the agent
	// makes for a local mirror. Not because the transport protects the bytes —
	// the signed digest does that — but because an agent that would fetch over
	// plaintext is an agent somebody will point at a plaintext mirror and then
	// reason incorrectly about.
	if !strings.HasPrefix(url, "https://") &&
		!strings.HasPrefix(url, "http://127.0.0.1") &&
		!strings.HasPrefix(url, "http://localhost") &&
		!strings.HasPrefix(url, "http://[::1]") {
		return nil, fmt.Errorf("refusing to download from %q: https is required", url)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		_ = response.Body.Close()
		return nil, fmt.Errorf("the download origin answered HTTP %d", response.StatusCode)
	}
	return response.Body, nil
}

// runUpgrade is the real installation.
func runUpgrade(ctx context.Context, options install.UpgradeOptions) error {
	return install.NewUpgrader(options).Run(ctx)
}

// StagingDirFor returns a staging directory inside the agent's own
// configuration directory when it is writable, and the system temporary
// directory otherwise.
//
// Preferring the configuration directory matters on a host where /tmp is a
// small tmpfs: a 3 MB archive is fine, but the installer's own extraction
// happens beside the target binary and an operator debugging a failed update
// benefits from both being in one place. Falling back rather than failing,
// because a read-only /etc is a legitimate configuration.
func StagingDirFor(paths install.Paths) string {
	directory := paths.ConfigDir()
	if info, err := os.Stat(directory); err == nil && info.IsDir() {
		probe, err := os.CreateTemp(directory, ".hcc-write-probe-*")
		if err == nil {
			name := probe.Name()
			_ = probe.Close()
			_ = os.Remove(name)
			return directory
		}
	}
	return os.TempDir()
}
