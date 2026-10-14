// Package release verifies and reads signed agent release metadata.
//
// It mirrors apps/server/hcc/updates/signing.py and the schema-v2 manifest in
// apps/server/hcc/agents/releases.py. The duplication is deliberate and the
// same as elsewhere in this agent: two independently deployed programs that
// have to agree, with contracts/release-signing.json keeping them in step.
//
// # Why the agent verifies at all
//
// Because the controller is not trusted to say what the bytes are.
//
// Before update management, the controller vouched for the digest: an
// administrator read it inside an authenticated TLS session and typed it into
// `hcc-agent upgrade --sha256`. A human in that loop is what made it safe.
// Remove the human — which is what an automatic update does — and the property
// that remains is "the control plane can install arbitrary code as root on
// every monitored machine", and that is the one outcome a fleet update
// mechanism must not create.
//
// So the publisher signs the release metadata, this agent carries the trusted
// public key stamped in at build time, and the digest it checks comes from the
// *signed* document. A compromised controller can still refuse to offer
// updates, or offer an older one — availability and version choice are its job
// — but it cannot produce metadata this agent accepts for a binary the
// publisher did not build.
//
// # There is no fallback
//
// When authenticity is required and no valid signature exists, the update is
// refused. There is no flag that downgrades to checksum-only trust, because a
// trust model with an escape hatch is the model that is off when it matters.
// `hcc-agent upgrade --from <file> --sha256 <digest>` remains, unsigned and
// deliberately: it has a human in it who got the digest from an authenticated
// session, and it is the recovery path when metadata is unavailable.
package release

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// Algorithm is the only signature algorithm accepted.
//
// One value, not a negotiated set. A verifier that accepts whatever the
// document names accepts the weakest thing it knows, which is how a downgrade
// attack gets its foothold. Adding a second algorithm means a verifier that
// accepts exactly two, decided here rather than by the document.
const Algorithm = "ed25519"

// KeyIDLength is how many hex characters of the key digest identify a key.
//
// It selects a key; it does not authorise one. An unknown identifier is
// reported as untrusted rather than as a bad signature, because those call for
// different actions from an operator.
const KeyIDLength = 16

// MaxManifestBytes and MaxSignatureBytes bound what will be parsed.
//
// Enforced here as well as on the controller, and not for symmetry: these
// documents arrive over a socket from something this agent does not fully
// trust, and an unbounded parse is a memory exhaustion attack against every
// host in the fleet at once.
const (
	MaxManifestBytes  = 256 * 1024
	MaxSignatureBytes = 8 * 1024
)

// Status is the outcome of verifying release metadata.
//
// A closed set of named outcomes rather than a boolean, because they call for
// different actions. "Not valid" would collapse "nobody signed this" — a
// missing release step — with "somebody signed this badly", which is an attack.
type Status string

const (
	// StatusValid means a trusted key signed these exact bytes.
	StatusValid Status = "valid"
	// StatusAbsent means no signature accompanied the metadata.
	StatusAbsent Status = "absent"
	// StatusUntrustedKey means a well-formed signature by a key this agent
	// does not trust.
	StatusUntrustedKey Status = "untrusted_key"
	// StatusInvalid means a trusted key is named and the signature does not
	// verify: either the document changed after signing, or it was relabelled.
	StatusInvalid Status = "invalid"
	// StatusMalformed means the signature document is not readable as one.
	StatusMalformed Status = "malformed"
	// StatusUnsupportedAlgorithm means an algorithm this agent does not
	// implement.
	StatusUnsupportedAlgorithm Status = "unsupported_algorithm"
)

// Authentic reports whether a status establishes publisher authenticity.
//
// Exactly one does. A single function rather than a comparison at each call
// site, so there is one place where "is this trusted" is decided and a new
// status cannot land on the trusted side of an `!= StatusInvalid` test.
func (s Status) Authentic() bool { return s == StatusValid }

// Verification is what verifying found.
type Verification struct {
	Status Status
	// KeyID is the key the signature named, when it was well-formed enough to
	// say so.
	KeyID string
	// Detail is a sentence for an operator's log. Never key material.
	Detail string
}

// Authentic reports whether this verification establishes authenticity.
func (v Verification) Authentic() bool { return v.Status.Authentic() }

// TrustStore holds the public keys this agent accepts.
//
// Explicit and finite. There is no mode in which an unknown key is accepted
// and no trust-on-first-use: an update mechanism that learned its keys from
// the thing it is verifying would be verifying nothing.
type TrustStore struct {
	keys map[string]ed25519.PublicKey
}

// NewTrustStore builds a store from base64 raw Ed25519 public keys.
//
// Raw rather than PEM: the value has to survive being stamped into this binary
// by `-ldflags -X`, which means one line with nothing shell-significant in it,
// and base64 of 32 bytes is 44 characters of exactly that.
//
// An empty or whitespace-only entry is skipped rather than rejected, because
// the build stamps an empty string when no key is configured and that is the
// normal state of a development build — it yields a store that trusts nothing,
// which is the correct outcome, not a startup failure.
func NewTrustStore(encodedKeys []string) (*TrustStore, error) {
	store := &TrustStore{keys: make(map[string]ed25519.PublicKey, len(encodedKeys))}
	for _, encoded := range encodedKeys {
		trimmed := strings.TrimSpace(encoded)
		if trimmed == "" {
			continue
		}
		raw, err := DecodePublicKey(trimmed)
		if err != nil {
			return nil, err
		}
		store.keys[KeyID(raw)] = raw
	}
	return store, nil
}

// Len reports how many keys are trusted.
func (t *TrustStore) Len() int {
	if t == nil {
		return 0
	}
	return len(t.keys)
}

// KeyIDs returns the trusted identifiers, sorted for stable output.
func (t *TrustStore) KeyIDs() []string {
	if t == nil {
		return nil
	}
	identifiers := make([]string, 0, len(t.keys))
	for identifier := range t.keys {
		identifiers = append(identifiers, identifier)
	}
	sort.Strings(identifiers)
	return identifiers
}

// DecodePublicKey decodes a base64 raw Ed25519 public key.
func DecodePublicKey(encoded string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil {
		return nil, fmt.Errorf("the release signing key is not valid base64: %w", err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf(
			"the release signing key decodes to %d bytes; an Ed25519 key is %d",
			len(raw), ed25519.PublicKeySize,
		)
	}
	return ed25519.PublicKey(raw), nil
}

// KeyID is the short identifier for a public key.
//
// Derived from the key rather than assigned, so two parties naming the same
// key agree without coordinating and a key cannot be given a misleading name.
func KeyID(public ed25519.PublicKey) string {
	digest := sha256.Sum256(public)
	return hex.EncodeToString(digest[:])[:KeyIDLength]
}

type signatureDocument struct {
	Algorithm string `json:"algorithm"`
	KeyID     string `json:"key_id"`
	Signature string `json:"signature"`
}

// Verify checks signatureDocument over payload.
//
// payload must be the bytes that were signed, not a re-serialisation of them.
// Two JSON libraries disagree about key order, integer rendering and non-ASCII
// escaping, so a signature over "the document as I would write it" fails
// whenever the writer and reader differ — and the failure looks exactly like
// tampering. This agent already carries that lesson for the configuration
// snapshot's checksum.
func (t *TrustStore) Verify(payload, signatureDoc []byte) Verification {
	if len(signatureDoc) > MaxSignatureBytes {
		return Verification{
			Status: StatusMalformed,
			Detail: fmt.Sprintf(
				"the signature is %d bytes, over the %d byte limit",
				len(signatureDoc), MaxSignatureBytes,
			),
		}
	}
	var document signatureDocument
	if err := json.Unmarshal(signatureDoc, &document); err != nil {
		return Verification{
			Status: StatusMalformed,
			Detail: fmt.Sprintf("the signature is not valid JSON: %v", err),
		}
	}
	if document.Algorithm == "" {
		return Verification{Status: StatusMalformed, Detail: "the signature names no algorithm"}
	}
	if document.KeyID == "" {
		return Verification{Status: StatusMalformed, Detail: "the signature names no key"}
	}
	if document.Signature == "" {
		return Verification{Status: StatusMalformed, Detail: "the signature is empty"}
	}
	if document.Algorithm != Algorithm {
		return Verification{
			Status: StatusUnsupportedAlgorithm,
			KeyID:  document.KeyID,
			Detail: fmt.Sprintf(
				"the signature names algorithm %q; this agent implements %s only",
				document.Algorithm, Algorithm,
			),
		}
	}
	raw, err := base64.StdEncoding.DecodeString(document.Signature)
	if err != nil {
		return Verification{
			Status: StatusMalformed,
			Detail: fmt.Sprintf("the signature is not valid base64: %v", err),
		}
	}
	if len(raw) != ed25519.SignatureSize {
		return Verification{
			Status: StatusMalformed,
			Detail: fmt.Sprintf(
				"the signature decodes to %d bytes; an Ed25519 signature is %d",
				len(raw), ed25519.SignatureSize,
			),
		}
	}

	key, ok := t.keys[document.KeyID]
	if !ok {
		known := "none configured"
		if identifiers := t.KeyIDs(); len(identifiers) > 0 {
			known = strings.Join(identifiers, ", ")
		}
		return Verification{
			Status: StatusUntrustedKey,
			KeyID:  document.KeyID,
			Detail: fmt.Sprintf(
				"the metadata was signed by key %s, which this agent does not trust. "+
					"Trusted keys: %s.", document.KeyID, known,
			),
		}
	}
	if !ed25519.Verify(key, payload, raw) {
		return Verification{
			Status: StatusInvalid,
			KeyID:  document.KeyID,
			Detail: "the signature does not verify; the metadata is not what was signed",
		}
	}
	return Verification{Status: StatusValid, KeyID: document.KeyID}
}

// VerifyMetadata verifies, reporting absence distinctly from failure.
//
// A nil signature is absence, which is the state of this project today:
// nothing has been published and nothing has been signed. The caller decides
// whether that is acceptable — it is not, for an automatic update.
func VerifyMetadata(payload, signatureDoc []byte, store *TrustStore) Verification {
	if signatureDoc == nil {
		return Verification{
			Status: StatusAbsent,
			Detail: "no signature accompanies the release metadata, so its " +
				"authenticity cannot be established",
		}
	}
	if store == nil {
		store = &TrustStore{}
	}
	return store.Verify(payload, signatureDoc)
}

// ---------------------------------------------------------------------------
// The manifest
// ---------------------------------------------------------------------------

// Platform is one architecture's archive for one version.
type Platform struct {
	Archive   string `json:"archive"`
	URL       string `json:"url"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
	Tier      string `json:"tier"`
}

// Entry is one release: its platforms, and what it needs to run.
//
// Every compatibility field is optional, and absent means no constraint —
// which is what a schema-v1 release declared, and so the only honest reading
// of one.
type Entry struct {
	Channel          string              `json:"channel"`
	ReleasedAt       string              `json:"released_at"`
	NotesURL         string              `json:"notes_url"`
	ProtocolVersions []int               `json:"protocol_versions"`
	MinController    string              `json:"min_controller"`
	SchemaRequire    string              `json:"schema_requirement"`
	Platforms        map[string]Platform `json:"platforms"`
}

// Manifest is the release metadata document.
type Manifest struct {
	SchemaVersion    int               `json:"schema_version"`
	GeneratedAt      string            `json:"generated_at"`
	Recommended      string            `json:"recommended"`
	MinimumSupported string            `json:"minimum_supported"`
	Channels         map[string]string `json:"channels"`
	Releases         map[string]Entry  `json:"releases"`
}

// ErrManifest reports metadata that exists and cannot be used.
var ErrManifest = errors.New("unusable release metadata")

// ParseManifest reads and checks a schema-v2 manifest.
//
// Schema v1 is not accepted here, and that is deliberate rather than an
// omission: v1 carries no channel and no compatibility fields, so an update
// planned from it would be planned from nothing. The controller upconverts v1
// for the install-command path, where the fields are not needed; an update
// instruction always carries v2.
func ParseManifest(payload []byte) (Manifest, error) {
	if len(payload) > MaxManifestBytes {
		return Manifest{}, fmt.Errorf(
			"%w: %d bytes, over the %d byte limit", ErrManifest, len(payload), MaxManifestBytes,
		)
	}
	var manifest Manifest
	decoder := json.NewDecoder(strings.NewReader(string(payload)))
	// Unknown fields are refused rather than ignored. A field this agent does
	// not understand in a document it is about to install from means the two
	// sides disagree about the format, and proceeding on the parts that did
	// parse is how an agent acts on half a decision.
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return Manifest{}, fmt.Errorf("%w: %v", ErrManifest, err)
	}
	if manifest.SchemaVersion != 2 {
		return Manifest{}, fmt.Errorf(
			"%w: schema_version is %d; this agent reads 2",
			ErrManifest, manifest.SchemaVersion,
		)
	}
	if len(manifest.Releases) == 0 {
		return Manifest{}, fmt.Errorf("%w: it lists no releases", ErrManifest)
	}
	for raw, entry := range manifest.Releases {
		if _, err := version.Parse(raw); err != nil {
			return Manifest{}, fmt.Errorf("%w: release key %q: %v", ErrManifest, raw, err)
		}
		if len(entry.Platforms) == 0 {
			return Manifest{}, fmt.Errorf("%w: release %q lists no platforms", ErrManifest, raw)
		}
	}
	for channel, raw := range manifest.Channels {
		if _, ok := manifest.Releases[raw]; !ok {
			return Manifest{}, fmt.Errorf(
				"%w: channel %q names version %q, which has no release entry",
				ErrManifest, channel, raw,
			)
		}
	}
	return manifest, nil
}

// PlatformFor returns the archive for a version and platform.
//
// platform is in GOOS/GOARCH form, matching buildinfo.Info.Platform, so the
// agent asks about itself without a translation table that could be wrong.
func (m Manifest) PlatformFor(versionText, platform string) (Platform, bool) {
	entry, ok := m.Releases[versionText]
	if !ok {
		return Platform{}, false
	}
	found, ok := entry.Platforms[platform]
	return found, ok
}
