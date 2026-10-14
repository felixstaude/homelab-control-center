package release_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
)

const validManifest = `{
  "schema_version": 2,
  "generated_at": "2026-10-14T12:00:00+00:00",
  "recommended": "0.1.1",
  "minimum_supported": "0.1.0",
  "channels": {"stable": "0.1.1"},
  "releases": {
    "0.1.0": {
      "channel": "stable",
      "platforms": {
        "linux/amd64": {
          "archive": "hcc-agent_0.1.0_linux_amd64.tar.gz",
          "url": "https://example.com/a.tar.gz",
          "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "size_bytes": 3000000,
          "tier": "supported"
        }
      }
    },
    "0.1.1": {
      "channel": "stable",
      "released_at": "2026-10-14T12:00:00+00:00",
      "notes_url": "https://example.com/notes",
      "protocol_versions": [1, 2],
      "min_controller": "0.1.0",
      "schema_requirement": "0010",
      "platforms": {
        "linux/amd64": {
          "archive": "hcc-agent_0.1.1_linux_amd64.tar.gz",
          "url": "https://example.com/b.tar.gz",
          "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "size_bytes": 3100000,
          "tier": "supported"
        },
        "linux/arm64": {
          "archive": "hcc-agent_0.1.1_linux_arm64.tar.gz",
          "url": "https://example.com/c.tar.gz",
          "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
          "size_bytes": 2900000,
          "tier": "supported"
        }
      }
    }
  }
}`

func TestParseManifestReadsAV2Document(t *testing.T) {
	manifest, err := release.ParseManifest([]byte(validManifest))
	if err != nil {
		t.Fatalf("a valid manifest did not parse: %v", err)
	}

	if manifest.Recommended != "0.1.1" {
		t.Errorf("recommended is %q", manifest.Recommended)
	}
	if manifest.Channels["stable"] != "0.1.1" {
		t.Errorf("stable channel is %q", manifest.Channels["stable"])
	}
	entry := manifest.Releases["0.1.1"]
	if len(entry.ProtocolVersions) != 2 || entry.ProtocolVersions[0] != 1 {
		t.Errorf("protocol versions are %v", entry.ProtocolVersions)
	}
	if entry.MinController != "0.1.0" || entry.SchemaRequire != "0010" {
		t.Errorf("compatibility fields are %q / %q", entry.MinController, entry.SchemaRequire)
	}
}

func TestPlatformForAsksAboutThisHost(t *testing.T) {
	manifest, err := release.ParseManifest([]byte(validManifest))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}

	// GOOS/GOARCH form, matching buildinfo.Info.Platform — so the agent asks
	// about itself without a translation table that could be wrong.
	platform, ok := manifest.PlatformFor("0.1.1", "linux/arm64")
	if !ok {
		t.Fatal("linux/arm64 is in the manifest and PlatformFor did not find it")
	}
	if platform.SizeBytes != 2900000 {
		t.Errorf("size is %d", platform.SizeBytes)
	}

	if _, ok := manifest.PlatformFor("0.1.1", "linux/riscv64"); ok {
		t.Error("an architecture the release does not publish was reported as available")
	}
	if _, ok := manifest.PlatformFor("9.9.9", "linux/amd64"); ok {
		t.Error("a version with no entry was reported as available")
	}
}

func TestParseManifestRefusals(t *testing.T) {
	cases := []struct {
		name     string
		document string
		fragment string
	}{
		{
			name:     "schema v1 is not read here",
			document: `{"schema_version": 1, "recommended": "0.1.0", "minimum_supported": "0.1.0", "releases": {}}`,
			fragment: "this agent reads 2",
		},
		{
			name:     "no releases",
			document: `{"schema_version": 2, "recommended": "0.1.0", "minimum_supported": "0.1.0", "releases": {}}`,
			fragment: "lists no releases",
		},
		{
			name: "a release key that is not a version",
			document: `{"schema_version": 2, "recommended": "latest", "minimum_supported": "0.1.0",
			  "releases": {"latest": {"platforms": {"linux/amd64": {"archive": "a", "url": "u", "sha256": "d", "size_bytes": 1, "tier": "supported"}}}}}`,
			fragment: "release key",
		},
		{
			name: "a release with no platforms",
			document: `{"schema_version": 2, "recommended": "0.1.0", "minimum_supported": "0.1.0",
			  "releases": {"0.1.0": {"platforms": {}}}}`,
			fragment: "lists no platforms",
		},
		{
			name: "a channel naming a missing release",
			document: `{"schema_version": 2, "recommended": "0.1.0", "minimum_supported": "0.1.0",
			  "channels": {"beta": "9.9.9"},
			  "releases": {"0.1.0": {"platforms": {"linux/amd64": {"archive": "a", "url": "u", "sha256": "d", "size_bytes": 1, "tier": "supported"}}}}}`,
			fragment: "no release entry",
		},
		{
			name:     "not JSON",
			document: `{nope`,
			fragment: "unusable release metadata",
		},
		{
			name: "an unknown field",
			document: `{"schema_version": 2, "recommended": "0.1.0", "minimum_supported": "0.1.0",
			  "surprise": true,
			  "releases": {"0.1.0": {"platforms": {"linux/amd64": {"archive": "a", "url": "u", "sha256": "d", "size_bytes": 1, "tier": "supported"}}}}}`,
			fragment: "surprise",
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			_, err := release.ParseManifest([]byte(testCase.document))
			if err == nil {
				t.Fatal("expected a refusal")
			}
			if !errors.Is(err, release.ErrManifest) {
				t.Errorf("error does not wrap ErrManifest: %v", err)
			}
			if !strings.Contains(err.Error(), testCase.fragment) {
				t.Errorf("error %q does not mention %q", err, testCase.fragment)
			}
		})
	}
}

// TestAnUnknownFieldIsRefused is called out separately because the reasoning
// is not obvious: ignoring it would be the friendlier default.
//
// A field this agent does not understand, in a document it is about to install
// a root binary from, means the two sides disagree about the format. Acting on
// the parts that did parse is how an agent applies half a decision — and the
// half it ignored could be the one that said "this is a pre-release" or "this
// needs a newer controller".
func TestAnUnknownFieldIsRefused(t *testing.T) {
	document := strings.Replace(
		validManifest, `"schema_version": 2,`, `"schema_version": 2, "trust_me": true,`, 1,
	)

	if _, err := release.ParseManifest([]byte(document)); err == nil {
		t.Fatal("an unrecognised field was accepted")
	}
}

func TestAnOversizedManifestIsRefusedBeforeParsing(t *testing.T) {
	// The bound is enforced on the agent as well as the controller, and not
	// for symmetry: this document arrives over a socket from something the
	// agent does not fully trust.
	huge := []byte("{" + strings.Repeat("a", release.MaxManifestBytes+1))

	_, err := release.ParseManifest(huge)
	if err == nil || !strings.Contains(err.Error(), "over the") {
		t.Fatalf("an oversized manifest was not refused by size: %v", err)
	}
}

func TestATrustStoreSkipsAnEmptyKey(t *testing.T) {
	// The build stamps an empty string when no key is configured, which is the
	// normal state of a development build. That must yield a store trusting
	// nothing — the correct outcome — rather than a startup failure.
	store, err := release.NewTrustStore([]string{"", "   "})
	if err != nil {
		t.Fatalf("an unconfigured key should not be an error: %v", err)
	}
	if store.Len() != 0 {
		t.Errorf("store holds %d keys", store.Len())
	}
	if store.KeyIDs() != nil && len(store.KeyIDs()) != 0 {
		t.Errorf("store reports key ids %v", store.KeyIDs())
	}
}

func TestATrustStoreRefusesAMalformedKey(t *testing.T) {
	for _, encoded := range []string{"not base64!!", "AAAA", "bm90IDMyIGJ5dGVz"} {
		if _, err := release.NewTrustStore([]string{encoded}); err == nil {
			t.Errorf("%q was accepted as a signing key", encoded)
		}
	}
}

func TestAnAbsentSignatureIsAbsent(t *testing.T) {
	// Distinguished from failure, because the two call for different actions
	// and this repository is currently in the first state: nothing published,
	// nothing signed.
	result := release.VerifyMetadata([]byte("{}"), nil, nil)

	if result.Status != release.StatusAbsent {
		t.Errorf("status is %q", result.Status)
	}
	if result.Authentic() {
		t.Error("an absent signature was treated as authentic")
	}
}

func TestOnlyValidIsAuthentic(t *testing.T) {
	// Asserted across every status, so adding one cannot land on the trusted
	// side by default.
	all := []release.Status{
		release.StatusValid,
		release.StatusAbsent,
		release.StatusUntrustedKey,
		release.StatusInvalid,
		release.StatusMalformed,
		release.StatusUnsupportedAlgorithm,
	}
	authentic := 0
	for _, status := range all {
		if status.Authentic() {
			authentic++
			if status != release.StatusValid {
				t.Errorf("%q reports itself authentic", status)
			}
		}
	}
	if authentic != 1 {
		t.Errorf("%d statuses report themselves authentic, expected 1", authentic)
	}
}

func TestANilStoreTrustsNothing(t *testing.T) {
	// A nil store is what an unconfigured agent has. It must refuse rather
	// than panic, and refuse as "untrusted key" rather than "valid".
	result := release.VerifyMetadata(
		[]byte("{}"),
		[]byte(`{"algorithm":"ed25519","key_id":"abcdef0123456789","signature":"`+
			strings.Repeat("A", 86)+`=="}`),
		nil,
	)

	if result.Status != release.StatusUntrustedKey {
		t.Errorf("status is %q (detail: %s)", result.Status, result.Detail)
	}
}
