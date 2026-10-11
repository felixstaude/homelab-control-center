package buildinfo_test

import (
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
)

func TestGetNeverReturnsEmptyFields(t *testing.T) {
	t.Parallel()

	info := buildinfo.Get()

	// Under `go test` no ldflags are set, so every field must come from a
	// fallback. An empty field would render as "hcc-agent  (commit , ...)".
	if info.Version == "" {
		t.Error("Version must fall back to a non-empty value")
	}
	if info.Commit == "" {
		t.Error("Commit must fall back to a non-empty value")
	}
	if info.BuildDate == "" {
		t.Error("BuildDate must fall back to a non-empty value")
	}
	if info.GoVersion == "" {
		t.Error("GoVersion must be populated from the runtime")
	}
	if info.Platform == "" {
		t.Error("Platform must be populated from the runtime")
	}
}

func TestGetReportsDevForUnstampedBuilds(t *testing.T) {
	t.Parallel()

	// Must not claim a release version it was not built as.
	if version := buildinfo.Get().Version; version != "dev" {
		t.Errorf("Version = %q, want \"dev\" for a test build", version)
	}
}

func TestPlatformIsGOOSSlashGOARCH(t *testing.T) {
	t.Parallel()

	if platform := buildinfo.Get().Platform; !strings.Contains(platform, "/") {
		t.Errorf("Platform = %q, want GOOS/GOARCH form", platform)
	}
}

func TestStringIncludesEveryField(t *testing.T) {
	t.Parallel()

	info := buildinfo.Get()
	rendered := info.String()

	if !strings.HasPrefix(rendered, "hcc-agent ") {
		t.Errorf("String() should start with the binary name, got: %s", rendered)
	}
	for _, field := range []string{info.Version, info.Commit, info.GoVersion, info.Platform} {
		if !strings.Contains(rendered, field) {
			t.Errorf("String() omits %q: %s", field, rendered)
		}
	}
}

func TestGetIsStableAcrossCalls(t *testing.T) {
	t.Parallel()

	if buildinfo.Get() != buildinfo.Get() {
		t.Error("Get() must return a cached, stable value")
	}
}
