package release_test

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/release"
)

// contractPath is the committed, generated contract.
//
// Relative, and a missing file is a failure rather than a skip: a contract
// test that quietly does nothing when it cannot find its contract reports
// green while verifying nothing, which for a *signature* verifier is the worst
// possible shape of false confidence.
const contractPath = "../../../../contracts/release-signing.json"

type signingContract struct {
	Algorithm              string `json:"algorithm"`
	KeyIDLength            int    `json:"key_id_length"`
	MaxSignatureBytes      int    `json:"max_signature_bytes"`
	TrustedPublicKeyBase64 string `json:"trusted_public_key_base64"`
	TrustedKeyID           string `json:"trusted_key_id"`
	SeedNote               string `json:"seed_note"`
	Vectors                []struct {
		PayloadBase64     string `json:"payload_base64"`
		SignatureDocument string `json:"signature_document"`
		ExpectedStatus    string `json:"expected_status"`
	} `json:"vectors"`
}

func loadSigningContract(t *testing.T) signingContract {
	t.Helper()
	raw, err := os.ReadFile(contractPath)
	if err != nil {
		t.Fatalf("cannot read the signing contract at %s: %v\n"+
			"Generate it with ./scripts/contracts.sh", contractPath, err)
	}
	var document signingContract
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("the signing contract is not valid JSON: %v", err)
	}
	if len(document.Vectors) == 0 {
		t.Fatal("the signing contract has no vectors; regenerate it")
	}
	return document
}

// TestTheAgentReachesTheSameVerdictAsTheController is the test that matters.
//
// It proves the Go verifier accepts exactly what the Python signer produces
// and refuses exactly what the Python verifier refuses — rather than the two
// being self-consistently wrong in the same direction, which is what two
// independent implementations tested only against themselves would be.
func TestTheAgentReachesTheSameVerdictAsTheController(t *testing.T) {
	document := loadSigningContract(t)
	store, err := release.NewTrustStore([]string{document.TrustedPublicKeyBase64})
	if err != nil {
		t.Fatalf("the contract's trusted key does not load: %v", err)
	}

	// Every status in the contract must be one this agent can produce. A typo
	// on either side would otherwise make a vector unassertable and silently
	// pass.
	known := map[string]bool{
		string(release.StatusValid):                true,
		string(release.StatusAbsent):               true,
		string(release.StatusUntrustedKey):         true,
		string(release.StatusInvalid):              true,
		string(release.StatusMalformed):            true,
		string(release.StatusUnsupportedAlgorithm): true,
	}

	for index, vector := range document.Vectors {
		payload, err := base64.StdEncoding.DecodeString(vector.PayloadBase64)
		if err != nil {
			t.Fatalf("vector %d: payload is not base64: %v", index, err)
		}
		if !known[vector.ExpectedStatus] {
			t.Fatalf("vector %d expects status %q, which this agent has no name for",
				index, vector.ExpectedStatus)
		}

		got := release.VerifyMetadata(payload, []byte(vector.SignatureDocument), store)

		if string(got.Status) != vector.ExpectedStatus {
			t.Errorf("vector %d: agent says %q, controller says %q (detail: %s)",
				index, got.Status, vector.ExpectedStatus, got.Detail)
		}
	}
}

// TestEveryOutcomeIsExercised guards the contract itself.
//
// A vector list that drifted to only valid signatures would still pass the
// test above while proving nothing about refusals, which is where the security
// actually lives.
func TestEveryOutcomeIsExercised(t *testing.T) {
	document := loadSigningContract(t)

	seen := make(map[string]int)
	for _, vector := range document.Vectors {
		seen[vector.ExpectedStatus]++
	}

	for _, required := range []release.Status{
		release.StatusValid,
		release.StatusInvalid,
		release.StatusUntrustedKey,
		release.StatusMalformed,
		release.StatusUnsupportedAlgorithm,
	} {
		if seen[string(required)] == 0 {
			t.Errorf("no contract vector exercises %q", required)
		}
	}
}

func TestTheContractAgreesAboutTheConstants(t *testing.T) {
	document := loadSigningContract(t)

	if release.Algorithm != document.Algorithm {
		t.Errorf("agent algorithm is %q, controller's is %q",
			release.Algorithm, document.Algorithm)
	}
	if release.KeyIDLength != document.KeyIDLength {
		t.Errorf("agent KeyIDLength is %d, controller's is %d",
			release.KeyIDLength, document.KeyIDLength)
	}
	// A larger bound on one side means a document the controller refused as
	// oversized is one the agent happily parses.
	if release.MaxSignatureBytes != document.MaxSignatureBytes {
		t.Errorf("agent MaxSignatureBytes is %d, controller's is %d",
			release.MaxSignatureBytes, document.MaxSignatureBytes)
	}
}

func TestTheContractAgreesAboutTheKeyIdentifier(t *testing.T) {
	document := loadSigningContract(t)
	key, err := release.DecodePublicKey(document.TrustedPublicKeyBase64)
	if err != nil {
		t.Fatalf("the contract's key does not decode: %v", err)
	}

	// Derived from the key on both sides. If these disagreed, every signature
	// would be reported as made by an untrusted key.
	if got := release.KeyID(key); got != document.TrustedKeyID {
		t.Errorf("agent derives key id %q, controller derives %q", got, document.TrustedKeyID)
	}
}
