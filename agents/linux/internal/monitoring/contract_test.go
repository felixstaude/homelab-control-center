package monitoring_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/monitoring"
)

// contractPath is the committed, generated contract file.
//
// Relative, from internal/monitoring up to the repository root. A relative
// path rather than an environment variable or a build flag: the test must fail
// when the file is missing, not skip — a contract test that silently does
// nothing when it cannot find its contract is worse than no contract test,
// because it reports green.
const contractPath = "../../../../contracts/monitoring-modules.json"

type contractDocument struct {
	RegistryVersion    int `json:"registry_version"`
	DocumentVersion    int `json:"document_version"`
	MinIntervalSeconds int `json:"min_interval_seconds"`
	MaxIntervalSeconds int `json:"max_interval_seconds"`
	Modules            []struct {
		ID                     string   `json:"id"`
		Name                   string   `json:"name"`
		Platform               string   `json:"platform"`
		ScheduleKinds          []string `json:"schedule_kinds"`
		MinIntervalSeconds     int      `json:"min_interval_seconds"`
		DefaultIntervalSeconds int      `json:"default_interval_seconds"`
		DataSchemaVersion      int      `json:"data_schema_version"`
		RequiresPrivileges     bool     `json:"requires_privileges"`
		AllowsDuplicateRuns    bool     `json:"allows_duplicate_runs"`
	} `json:"modules"`
}

func loadContract(t *testing.T) contractDocument {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(contractPath))
	if err != nil {
		t.Fatalf(
			"cannot read the module contract at %s: %v\n"+
				"Generate it with ./scripts/contracts.sh from the repository root.",
			contractPath, err,
		)
	}
	var document contractDocument
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("the module contract is not valid JSON: %v", err)
	}
	return document
}

// TestRegistryMatchesTheContract is why having two registries is acceptable.
//
// The controller's registry is authoritative and this agent's is a mirror.
// Three hand-maintained copies of one table would drift, so the contract file
// is generated from the controller and committed, and this asserts the mirror
// still agrees. A module added on one side and not the other fails here rather
// than becoming a host that silently never collects.
func TestRegistryMatchesTheContract(t *testing.T) {
	contract := loadContract(t)

	if contract.RegistryVersion != monitoring.RegistryVersion {
		t.Errorf(
			"registry version: contract has %d, agent has %d",
			contract.RegistryVersion, monitoring.RegistryVersion,
		)
	}
	if contract.DocumentVersion != monitoring.DocumentVersion {
		t.Errorf(
			"document version: contract has %d, agent has %d",
			contract.DocumentVersion, monitoring.DocumentVersion,
		)
	}
	if contract.MinIntervalSeconds != monitoring.MinIntervalSeconds {
		t.Errorf(
			"global interval floor: contract has %d, agent has %d",
			contract.MinIntervalSeconds, monitoring.MinIntervalSeconds,
		)
	}
	if contract.MaxIntervalSeconds != monitoring.MaxIntervalSeconds {
		t.Errorf(
			"global interval ceiling: contract has %d, agent has %d",
			contract.MaxIntervalSeconds, monitoring.MaxIntervalSeconds,
		)
	}

	agentModules := monitoring.Modules()
	if len(agentModules) != len(contract.Modules) {
		t.Fatalf(
			"module count: contract has %d, agent has %d — the mirror is out of date",
			len(contract.Modules), len(agentModules),
		)
	}

	// Order as well as content: the contract's order is the catalogue's, which
	// the interface renders in, and an agent that reordered it would be
	// reporting a different catalogue.
	for index, expected := range contract.Modules {
		actual := agentModules[index]
		if actual.ID != expected.ID {
			t.Errorf("module %d: contract has %q, agent has %q", index, expected.ID, actual.ID)
			continue
		}
		if actual.Name != expected.Name {
			t.Errorf("%s name: contract has %q, agent has %q", expected.ID, expected.Name, actual.Name)
		}
		if actual.MinIntervalSeconds != expected.MinIntervalSeconds {
			t.Errorf(
				"%s interval floor: contract has %d, agent has %d",
				expected.ID, expected.MinIntervalSeconds, actual.MinIntervalSeconds,
			)
		}
		if actual.DataSchemaVersion != expected.DataSchemaVersion {
			t.Errorf(
				"%s data schema version: contract has %d, agent has %d",
				expected.ID, expected.DataSchemaVersion, actual.DataSchemaVersion,
			)
		}
		if len(actual.ScheduleKinds) != len(expected.ScheduleKinds) {
			t.Errorf(
				"%s schedule kinds: contract has %v, agent has %v",
				expected.ID, expected.ScheduleKinds, actual.ScheduleKinds,
			)
			continue
		}
		for kindIndex, kind := range expected.ScheduleKinds {
			if actual.ScheduleKinds[kindIndex] != kind {
				t.Errorf(
					"%s schedule kinds: contract has %v, agent has %v",
					expected.ID, expected.ScheduleKinds, actual.ScheduleKinds,
				)
				break
			}
		}
	}
}

// TestNoModuleInThisPhaseNeedsPrivileges pins the claim that the agent never
// needs root to collect.
//
// Asserted against the contract rather than against the agent's own registry,
// because the agent deliberately does not carry this field — it has no use for
// it — so the controller's declaration is the only copy and this is the only
// place it is checked on the agent side.
func TestNoModuleInThisPhaseNeedsPrivileges(t *testing.T) {
	for _, module := range loadContract(t).Modules {
		if module.RequiresPrivileges {
			t.Errorf(
				"module %s declares that it needs privileges; no module in this phase should",
				module.ID,
			)
		}
	}
}

// TestEveryContractModuleIsSupported is the mirror of the count check above,
// stated as the property that actually matters.
//
// A module in the contract that this agent does not know is a module an
// operator can enable in the interface and that will then never collect — with
// the agent reporting the configuration as applied, because an unknown
// *disabled* module is accepted and an unknown *enabled* one is rejected. Both
// are correct behaviours and neither is what the operator wanted.
func TestEveryContractModuleIsSupported(t *testing.T) {
	for _, module := range loadContract(t).Modules {
		if !monitoring.Supported(module.ID) {
			t.Errorf("the controller offers module %q and this agent does not have it", module.ID)
		}
	}
}
