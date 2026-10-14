// Package buildinfo exposes version metadata for the agent binary.
//
// Values are injected at link time by the build (see the `agent-build` target
// in the root Makefile). When they are absent — for example under `go run` or
// `go test` — the package falls back to the VCS stamps the Go toolchain embeds
// automatically, and finally to "dev". It never reports a misleading version.
package buildinfo

import (
	"fmt"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
)

// Injected via -ldflags "-X". Leave unset for development builds.
var (
	version   = ""
	commit    = ""
	buildDate = ""

	// releaseKeys is the publisher's Ed25519 public key or keys, base64,
	// comma-separated.
	//
	// Stamped at link time rather than read from a file, because this is the
	// agent's trust anchor for automatic updates: a key that lives in
	// configuration is a key the configuration can be made to change, and
	// the whole point of the detached signature is that nothing the agent
	// talks to over the network gets to decide what it will install.
	//
	// **Empty is the honest default and it refuses everything.** A
	// development build, or a build of this repository by anybody who is not
	// the publisher, trusts no key and so declines every update instruction
	// with "no publisher key is trusted by this build". That is correct: such
	// a binary genuinely cannot tell a real release from a forged one.
	releaseKeys = ""
)

// ReleaseKeys returns the publisher keys this build trusts.
//
// Empty for any build that was not stamped with one, which includes every
// development build. The caller adds whatever an operator configured; see
// config.ReleaseKeys for why that is an addition rather than a replacement.
func ReleaseKeys() []string {
	if strings.TrimSpace(releaseKeys) == "" {
		return nil
	}
	var keys []string
	for _, key := range strings.Split(releaseKeys, ",") {
		if trimmed := strings.TrimSpace(key); trimmed != "" {
			keys = append(keys, trimmed)
		}
	}
	return keys
}

// Info describes the running binary.
type Info struct {
	// Version is a semantic version for released builds, or "dev".
	Version string `json:"version"`
	// Commit is the git revision the binary was built from, if known.
	Commit string `json:"commit"`
	// BuildDate is an RFC 3339 timestamp, if known.
	BuildDate string `json:"build_date"`
	// GoVersion is the toolchain that produced the binary.
	GoVersion string `json:"go_version"`
	// Platform is the target in GOOS/GOARCH form.
	Platform string `json:"platform"`
}

var (
	once   sync.Once
	cached Info
)

// Get returns the build information for this binary.
func Get() Info {
	once.Do(func() {
		cached = Info{
			Version:   firstNonEmpty(version, vcsVersion(), "dev"),
			Commit:    firstNonEmpty(commit, vcsRevision(), "unknown"),
			BuildDate: firstNonEmpty(buildDate, vcsTime(), "unknown"),
			GoVersion: runtime.Version(),
			Platform:  fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH),
		}
	})
	return cached
}

// String renders a single-line human-readable summary.
func (i Info) String() string {
	return fmt.Sprintf(
		"hcc-agent %s (commit %s, built %s, %s, %s)",
		i.Version, i.Commit, i.BuildDate, i.GoVersion, i.Platform,
	)
}

// vcsVersion reports the module version when installed via `go install`.
func vcsVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	// "(devel)" is not useful to an operator reading a log line.
	if info.Main.Version == "" || info.Main.Version == "(devel)" {
		return ""
	}
	return info.Main.Version
}

func vcsRevision() string {
	value := buildSetting("vcs.revision")
	if len(value) > 12 {
		return value[:12]
	}
	return value
}

func vcsTime() string {
	return buildSetting("vcs.time")
}

// buildSetting reads one of the stamps the toolchain embeds when building
// inside a VCS checkout.
func buildSetting(key string) string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	for _, setting := range info.Settings {
		if setting.Key == key {
			return setting.Value
		}
	}
	return ""
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}
