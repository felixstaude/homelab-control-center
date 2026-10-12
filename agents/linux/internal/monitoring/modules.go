// Package monitoring is the agent's half of the monitoring contract: which
// modules it can collect, what a configuration document looks like, and
// whether a given one is usable on this host.
//
// # The registry is a mirror, and the drift is tested rather than trusted
//
// The authoritative registry is the controller's
// (apps/server/hcc/monitoring/modules.py). This file is a second copy, which
// is a real risk and is managed rather than accepted:
// contracts/monitoring-modules.json is generated from the controller's
// registry and committed, TestRegistryMatchesTheContract reads it and
// compares, and `scripts/contracts.sh --check` fails in CI if the committed
// file is stale. So a module added on one side and not the other is a failing
// test rather than a host that silently never collects.
//
// A shared definition would be better and is not available: these are two
// independently deployed programs in two languages, and the code-generation
// step that would remove the duplication does not exist yet (the same
// situation internal/protocol is in, and documented there).
//
// # Nothing here enables anything
//
// A descriptor describes what a module *can* do. Whether it runs comes from
// the configuration document, which the controller resolves and the agent
// applies without merging — so there is no default in this file that could
// turn collection on, and no code path that invents one.
//
// # What "supported" means, and what it does not
//
// [Supported] answers "is this module in this agent's registry". It does not
// answer "can this host collect it" — a swapless host supports the swap module
// and cannot produce a swap figure from it. That second question is the
// capability report, which belongs to the collectors, and conflating the two
// would make an agent refuse a configuration it should have accepted and then
// reported as unavailable.
package monitoring

import "sort"

// RegistryVersion is the version of the catalogue this agent was built with.
//
// Distinct from protocol.Version: the protocol is the frame format and the
// registry is the catalogue, and they move for different reasons. Tying them
// together would mean adding a module forced every agent in the fleet to be
// upgraded before it could connect.
const RegistryVersion = 1

// DocumentVersion is the configuration document shape this agent understands.
//
// A document declaring a higher one is refused: the agent cannot know what a
// field it has never heard of means, and guessing is how an agent comes to
// collect something nobody asked for.
const DocumentVersion = 1

// Global interval bounds. The controller sends its own in every document and
// those are authoritative; these are the floor and ceiling this build will
// accept even if a document claims otherwise, so a compromised controller
// cannot instruct an agent to sample a thousand times a second.
const (
	MinIntervalSeconds = 1
	MaxIntervalSeconds = 30 * 24 * 60 * 60
)

// ScheduleKind values this agent understands.
const (
	KindInterval = "interval"
	KindCalendar = "calendar"
	KindEvent    = "event"
	KindManual   = "manual"
)

// Descriptor is what the agent needs to know about one module in order to
// validate a configuration for it.
//
// Deliberately smaller than the controller's descriptor: the agent has no use
// for a human-readable description or for the metric catalogue, which exist so
// an interface can render them. Copying fields nothing reads would be more
// surface for the mirror to drift on.
type Descriptor struct {
	ID                 string
	Name               string
	ScheduleKinds      []string
	MinIntervalSeconds int
	// DataSchemaVersion is the version of the metric payload this module
	// produces. Sent with telemetry so the controller can interpret a payload
	// from an agent older than itself.
	DataSchemaVersion int
}

// Accepts reports whether this module permits a scheduling kind.
func (d Descriptor) Accepts(kind string) bool {
	for _, permitted := range d.ScheduleKinds {
		if permitted == kind {
			return true
		}
	}
	return false
}

// intervalOnly is every module in this phase: all five are samplers, and a
// sampler on a calendar is a measurement taken at a time chosen for human
// convenience rather than for the signal.
var intervalOnly = []string{KindInterval}

// registry is every module this agent knows, in the controller's order.
var registry = []Descriptor{
	{
		ID:                 "cpu",
		Name:               "CPU utilisation",
		ScheduleKinds:      intervalOnly,
		MinIntervalSeconds: 1,
		DataSchemaVersion:  1,
	},
	{
		ID:                 "memory",
		Name:               "Memory",
		ScheduleKinds:      intervalOnly,
		MinIntervalSeconds: 1,
		DataSchemaVersion:  1,
	},
	{
		ID:                 "swap",
		Name:               "Swap",
		ScheduleKinds:      intervalOnly,
		MinIntervalSeconds: 1,
		DataSchemaVersion:  1,
	},
	{
		// Five seconds, not one: the kernel recomputes /proc/loadavg on a
		// 5-second timer, so sampling faster returns the same number and
		// stores it twice.
		ID:                 "load",
		Name:               "Load average",
		ScheduleKinds:      intervalOnly,
		MinIntervalSeconds: 5,
		DataSchemaVersion:  1,
	},
	{
		// A minute: uptime is a clock, so sampling it often tells you only
		// what arithmetic already would.
		ID:                 "uptime",
		Name:               "System uptime",
		ScheduleKinds:      intervalOnly,
		MinIntervalSeconds: 60,
		DataSchemaVersion:  1,
	},
}

// Modules returns every module this agent knows, in catalogue order.
//
// A copy, so a caller cannot mutate the registry — which, being a package
// variable, would otherwise let one goroutine change the interval floor for
// every later validation in the process.
func Modules() []Descriptor {
	out := make([]Descriptor, len(registry))
	copy(out, registry)
	return out
}

// ModuleIDs returns every known module identifier, sorted.
func ModuleIDs() []string {
	ids := make([]string, 0, len(registry))
	for _, module := range registry {
		ids = append(ids, module.ID)
	}
	sort.Strings(ids)
	return ids
}

// Lookup returns a module's descriptor.
func Lookup(id string) (Descriptor, bool) {
	for _, module := range registry {
		if module.ID == id {
			return module, true
		}
	}
	return Descriptor{}, false
}

// Supported reports whether this agent knows a module at all.
//
// See the package comment for why this is not the same question as "can this
// host collect it".
func Supported(id string) bool {
	_, found := Lookup(id)
	return found
}
