package version_test

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/version"
)

// contractPath is the committed, generated contract file.
//
// Relative, from internal/version up to the repository root — the same
// convention internal/monitoring's contract test uses, and for the same
// reason: the test must *fail* when the file is missing rather than skip. A
// contract test that quietly does nothing when it cannot find its contract is
// worse than none, because it reports green.
const contractPath = "../../../../contracts/version-ordering.json"

type contract struct {
	MaxLength int `json:"max_length"`
	Versions  []struct {
		Raw          string   `json:"raw"`
		Major        int      `json:"major"`
		Minor        int      `json:"minor"`
		Patch        int      `json:"patch"`
		Prerelease   []string `json:"prerelease"`
		Build        *string  `json:"build"`
		IsPrerelease bool     `json:"is_prerelease"`
	} `json:"versions"`
	Comparisons []struct {
		Left    string `json:"left"`
		Right   string `json:"right"`
		Compare int    `json:"compare"`
	} `json:"comparisons"`
	Rejections []string `json:"rejections"`
	Steps      []struct {
		Installed string `json:"installed"`
		Target    string `json:"target"`
		Step      string `json:"step"`
	} `json:"steps"`
}

func load(t *testing.T) contract {
	t.Helper()
	raw, err := os.ReadFile(contractPath)
	if err != nil {
		t.Fatalf("cannot read the version contract at %s: %v\n"+
			"Generate it with ./scripts/contracts.sh", contractPath, err)
	}
	var document contract
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("the version contract is not valid JSON: %v", err)
	}
	if len(document.Comparisons) == 0 || len(document.Versions) == 0 {
		t.Fatalf("the version contract is empty; regenerate it")
	}
	return document
}

// TestTheContractParsesTheSameWay checks the decomposition, not just the
// ordering: an implementation that read 0.10.0 as major 0, minor 1 would still
// order most pairs correctly and be wrong about what the version *is*.
func TestTheContractParsesTheSameWay(t *testing.T) {
	document := load(t)

	for _, expected := range document.Versions {
		parsed, err := version.Parse(expected.Raw)
		if err != nil {
			t.Errorf("%q: the controller parses this and the agent does not: %v", expected.Raw, err)
			continue
		}
		if parsed.Major != expected.Major ||
			parsed.Minor != expected.Minor ||
			parsed.Patch != expected.Patch {
			t.Errorf("%q: agent reads %d.%d.%d, controller reads %d.%d.%d",
				expected.Raw,
				parsed.Major, parsed.Minor, parsed.Patch,
				expected.Major, expected.Minor, expected.Patch)
		}
		if len(parsed.Prerelease) != len(expected.Prerelease) {
			t.Errorf("%q: agent reads %d pre-release identifiers, controller reads %d",
				expected.Raw, len(parsed.Prerelease), len(expected.Prerelease))
			continue
		}
		for index, identifier := range expected.Prerelease {
			if parsed.Prerelease[index] != identifier {
				t.Errorf("%q: pre-release identifier %d is %q for the agent and %q for the controller",
					expected.Raw, index, parsed.Prerelease[index], identifier)
			}
		}
		expectedBuild := ""
		if expected.Build != nil {
			expectedBuild = *expected.Build
		}
		if parsed.Build != expectedBuild {
			t.Errorf("%q: agent reads build %q, controller reads %q",
				expected.Raw, parsed.Build, expectedBuild)
		}
		if parsed.IsPrerelease() != expected.IsPrerelease {
			t.Errorf("%q: agent says is-prerelease=%v, controller says %v",
				expected.Raw, parsed.IsPrerelease(), expected.IsPrerelease)
		}
	}
}

// TestTheContractOrdersTheSameWay is the one that matters.
//
// Every pair, in both directions, including each version against itself —
// which pins reflexivity and the equal-precedence cases that a sorted list
// would hide behind an arbitrary stable order.
func TestTheContractOrdersTheSameWay(t *testing.T) {
	document := load(t)

	for _, expected := range document.Comparisons {
		left, err := version.Parse(expected.Left)
		if err != nil {
			t.Fatalf("%q does not parse: %v", expected.Left, err)
		}
		right, err := version.Parse(expected.Right)
		if err != nil {
			t.Fatalf("%q does not parse: %v", expected.Right, err)
		}
		if got := left.Compare(right); got != expected.Compare {
			t.Errorf("Compare(%q, %q) = %d for the agent, %d for the controller",
				expected.Left, expected.Right, got, expected.Compare)
		}
	}
}

// TestTheHelpersAgreeWithCompare guards against a helper that inverts the
// comparison it delegates to — the kind of error a direct Compare test cannot
// see, and the kind that silently offers a downgrade as an upgrade.
func TestTheHelpersAgreeWithCompare(t *testing.T) {
	document := load(t)

	for _, expected := range document.Comparisons {
		left := version.MustParse(expected.Left)
		right := version.MustParse(expected.Right)

		if left.LessThan(right) != (expected.Compare < 0) {
			t.Errorf("%q.LessThan(%q) disagrees with the contract", expected.Left, expected.Right)
		}
		if left.GreaterThan(right) != (expected.Compare > 0) {
			t.Errorf("%q.GreaterThan(%q) disagrees with the contract", expected.Left, expected.Right)
		}
		if left.Equal(right) != (expected.Compare == 0) {
			t.Errorf("%q.Equal(%q) disagrees with the contract", expected.Left, expected.Right)
		}
	}
}

// TestTheContractRefusesTheSameStrings covers the other half of agreement.
//
// Two parsers that order the same versions identically can still disagree
// about what a version *is* — and an agent that accepted "v1.2.3" where the
// controller refused it would act on metadata the controller never validated.
func TestTheContractRefusesTheSameStrings(t *testing.T) {
	document := load(t)

	for _, rejected := range document.Rejections {
		if parsed, err := version.Parse(rejected); err == nil {
			t.Errorf("the controller refuses %q and the agent parsed it as %q",
				rejected, parsed)
		}
	}
}

// TestTheContractAgreesAboutStepKind pins what an automatic-update policy
// decides on. "Patch releases only" has to mean the same thing in the
// controller that plans the update and the agent that refuses it.
func TestTheContractAgreesAboutStepKind(t *testing.T) {
	document := load(t)

	for _, expected := range document.Steps {
		installed := version.MustParse(expected.Installed)
		target := version.MustParse(expected.Target)

		if got := version.Step(installed, target); got != expected.Step {
			t.Errorf("Step(%q, %q) = %q for the agent, %q for the controller",
				expected.Installed, expected.Target, got, expected.Step)
		}
	}
}

func TestTheContractAgreesAboutTheLengthBound(t *testing.T) {
	document := load(t)

	// Not cosmetic: a longer bound on one side means a value the controller
	// refused as malformed is one the agent happily parses.
	if version.MaxLength != document.MaxLength {
		t.Errorf("agent MaxLength is %d, controller's is %d",
			version.MaxLength, document.MaxLength)
	}
}

// TestTheRepositoryVersionParses is the smallest possible end-to-end check:
// whatever the build stamps into this binary has to be orderable, or every
// comparison against it is already broken.
func TestTheRepositoryVersionParses(t *testing.T) {
	raw, err := os.ReadFile("../../../../VERSION")
	if err != nil {
		t.Fatalf("cannot read VERSION: %v", err)
	}
	parsed, err := version.Parse(string(raw))
	if err != nil {
		t.Fatalf("the repository's own VERSION does not parse: %v", err)
	}
	if parsed.Major != 0 || parsed.Minor != 1 {
		t.Logf("VERSION is now %s", parsed)
	}
}
