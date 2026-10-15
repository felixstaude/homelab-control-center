package collect

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// MaxInterfaces bounds how many interfaces one host may report.
//
// Cardinality, as with [MaxCores], [MaxMounts] and [MaxDevices]. The default
// selection below already excludes the thing that would blow this — a Docker
// host's `veth` pairs, of which there is one per running container — so the
// bound is the backstop rather than the mechanism.
const MaxInterfaces = 32

// Network reports throughput, packet rates, errors and link state per
// interface.
//
// # These are rates, computed here, and the first collection reports nothing
//
// /proc/net/dev holds counters since boot, so a single snapshot is a lifetime
// total rather than a throughput. Same reasoning as [CPU] and [DiskIO]: the
// rate comes from the difference between two samples, the first collection
// after a start produces nothing, and a counter that went backwards produces
// a gap rather than a spike.
//
// Counter *wrap* deserves its own sentence, because on a 32-bit kernel these
// are 32-bit counters and a gigabit link wraps 4 GiB in about 34 seconds. A
// wrap is indistinguishable from a reboot from two samples alone, and both
// are handled the same way — the interval produces nothing. Losing one
// interval every 34 seconds would be bad; the agent does not support 32-bit
// kernels for this, and on 64-bit the counters wrap after about 4,700 years.
//
// # Which interfaces, and why not all of them
//
// The hard constraint is **not double-counting**. A packet leaving a
// container traverses a `veth`, a bridge and the physical NIC, and is
// counted by all three. Reporting every interface and letting somebody add
// them up produces a number three times the truth.
//
// The default selection is therefore by exclusion, in this order:
//
//   - **Loopback.** Traffic a host sends to itself is not network traffic in
//     any sense an operator means.
//   - **Anything enslaved to a master.** A NIC in a bridge or a bond has its
//     traffic counted on the master too, and the master is the meaningful
//     unit. This is also what makes a bridged VM host come out right: the
//     physical NIC is the slave and is dropped, `br0` is kept.
//   - **Container and virtualisation plumbing by name** — `veth*`,
//     `docker0`, `br-*`, `cni*`, `flannel*`, `cali*`, `virbr*`, `vnet*`.
//     These are the interfaces whose traffic is already counted elsewhere
//     and whose names are generated, so they are both redundant and the
//     cardinality risk.
//
// Everything else is kept, which is the part that matters: a physical NIC, a
// container's `eth0`, a WireGuard tunnel, a bond, a VLAN and a
// deliberately-created bridge all survive.
//
// # The selection is configurable, and an explicit list replaces the rule
//
// [Network.Only], from HCC_AGENT_NETWORK_INTERFACES, names exactly the
// interfaces to report. When it is set, none of the above applies — an
// operator who asks for `veth1234` gets it. A local setting rather than part
// of the controller's configuration document, for the same reason
// [CPU.IncludePerCore] is: this is a decision about one host's topology
// rather than about the fleet, and module options do not exist in the
// document model yet.
type Network struct {
	// ProcRoot is where /proc is mounted; tests point it at a fixture.
	ProcRoot string
	// SysRoot is where /sys is mounted, for link state, speed and the
	// master check. Tests point it at a fixture tree.
	SysRoot string
	// Only, when non-empty, is the exact set of interfaces to report and
	// replaces the default selection entirely.
	Only []string
	// Now is the clock, injected so a test can state the interval exactly.
	Now func() time.Time

	mu       sync.Mutex
	previous map[string]netCounters
	taken    time.Time
}

// excludedInterfacePrefixes are the generated names whose traffic is already
// counted on something else. See the type comment.
var excludedInterfacePrefixes = []string{
	"veth", "docker", "br-", "cni", "flannel", "cali", "virbr", "vnet",
	// systemd-nspawn and LXC host-side halves.
	"vb-", "vz-",
}

// netCounters is the subset of /proc/net/dev this reads.
type netCounters struct {
	receiveBytes    float64
	receivePackets  float64
	receiveErrors   float64
	receiveDrops    float64
	transmitBytes   float64
	transmitPackets float64
	transmitErrors  float64
	transmitDrops   float64
}

// NewNetwork builds a network collector.
func NewNetwork(procRoot, sysRoot string, only []string) *Network {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	if sysRoot == "" {
		sysRoot = DefaultSysRoot
	}
	return &Network{ProcRoot: procRoot, SysRoot: sysRoot, Only: only, Now: time.Now}
}

// Module implements [Collector].
func (n *Network) Module() string { return "network" }

// Available implements [Collector].
func (n *Network) Available() Availability {
	path := n.devPath()
	counters, err := n.read()
	if err != nil {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s is not readable, so network activity cannot be measured", path,
			),
		}
	}
	if len(counters) == 0 {
		if len(n.Only) > 0 {
			return Availability{
				Available: false,
				Reason: fmt.Sprintf(
					"none of the configured interfaces (%s) exist on this host",
					strings.Join(n.Only, ", "),
				),
			}
		}
		return Availability{
			Available: false,
			Reason: "this host has no reportable network interfaces — only loopback, " +
				"bridge members and container plumbing, whose traffic is counted elsewhere",
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
func (n *Network) Collect(_ context.Context) ([]Sample, error) {
	current, err := n.read()
	if err != nil {
		return nil, err
	}
	clock := n.Now
	if clock == nil {
		clock = time.Now
	}
	now := clock()

	n.mu.Lock()
	previous := n.previous
	taken := n.taken
	n.previous = current
	n.taken = now
	n.mu.Unlock()

	samples := make([]Sample, 0, len(current)*10)

	// Link state and speed come from the current snapshot rather than from a
	// delta, so they are reported on the very first collection — which is
	// what makes "the cable is unplugged" visible immediately rather than
	// one interval later.
	for name := range current {
		labels := map[string]string{"interface": name}
		if up, known := n.operationalState(name); known {
			samples = append(samples, Sample{Metric: "up", Labels: labels, Value: boolValue(up)})
		}
		if speed, known := n.linkSpeed(name); known {
			samples = append(samples, Sample{Metric: "speed", Labels: labels, Value: speed})
		}
	}

	if previous != nil {
		if elapsed := now.Sub(taken).Seconds(); elapsed > 0 {
			samples = append(samples, rateSamples(previous, current, elapsed)...)
		}
	}

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// rateSamples turns two counter snapshots into per-second rates.
func rateSamples(previous, current map[string]netCounters, elapsed float64) []Sample {
	samples := make([]Sample, 0, len(current)*8)
	for name, after := range current {
		before, had := previous[name]
		if !had {
			// An interface that was not there last time: a tunnel brought
			// up, a cable plugged into a hot-plug NIC. No delta to take.
			continue
		}
		labels := map[string]string{"interface": name}
		pairs := []struct {
			metric        string
			before, after float64
		}{
			{"receive_bytes", before.receiveBytes, after.receiveBytes},
			{"transmit_bytes", before.transmitBytes, after.transmitBytes},
			{"receive_packets", before.receivePackets, after.receivePackets},
			{"transmit_packets", before.transmitPackets, after.transmitPackets},
			{"receive_errors", before.receiveErrors, after.receiveErrors},
			{"transmit_errors", before.transmitErrors, after.transmitErrors},
			{"receive_drops", before.receiveDrops, after.receiveDrops},
			{"transmit_drops", before.transmitDrops, after.transmitDrops},
		}
		// Computed for the whole interface before any is appended, so a
		// counter reset drops *all* of its series rather than leaving a
		// half-reported interval where bytes are absent and packets are not.
		// Mixed reporting is worse than a gap: a chart with throughput
		// missing and packet rate present invites exactly the wrong
		// conclusion.
		values := make([]float64, 0, len(pairs))
		reset := false
		for _, pair := range pairs {
			value, ok := rate(pair.before, pair.after, elapsed)
			if !ok {
				reset = true
				break
			}
			values = append(values, value)
		}
		if reset {
			continue
		}
		for index, pair := range pairs {
			samples = append(samples, Sample{
				Metric: pair.metric, Labels: labels, Value: values[index],
			})
		}
	}
	return samples
}

func (n *Network) devPath() string {
	return filepath.Join(n.ProcRoot, "net", "dev")
}

// read parses /proc/net/dev, keeping only the interfaces this host reports.
//
// The format is two header lines and then `name: <16 numbers>`. The name is
// right-aligned in a fixed-width field and the colon may therefore be
// adjacent to the first number on a long name — `wlp0s20f3:       0` splits
// fine, but a 15-character name produces `verylongname0:0`. Splitting on the
// colon first rather than on whitespace is what makes that parse.
func (n *Network) read() (map[string]netCounters, error) {
	path := n.devPath()
	file, err := os.Open(path) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	defer file.Close()

	only := map[string]bool{}
	for _, name := range n.Only {
		if trimmed := strings.TrimSpace(name); trimmed != "" {
			only[trimmed] = true
		}
	}

	counters := make(map[string]netCounters, 8)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		name, rest, found := strings.Cut(line, ":")
		if !found {
			// A header line. Skipped rather than counted, because the header
			// itself contains a colon only in "Inter-|   Receive" — which it
			// does not, so this is how both header lines are dropped.
			continue
		}
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		if len(only) > 0 {
			if !only[name] {
				continue
			}
		} else if !n.reportable(name) {
			continue
		}
		if len(counters) >= MaxInterfaces {
			continue
		}
		fields := strings.Fields(rest)
		if len(fields) < 16 {
			continue
		}
		values := make([]float64, 16)
		malformed := false
		for index := range values {
			value, err := strconv.ParseFloat(fields[index], 64)
			if err != nil || value < 0 {
				malformed = true
				break
			}
			values[index] = value
		}
		if malformed {
			continue
		}
		counters[name] = netCounters{
			receiveBytes:    values[0],
			receivePackets:  values[1],
			receiveErrors:   values[2],
			receiveDrops:    values[3],
			transmitBytes:   values[8],
			transmitPackets: values[9],
			transmitErrors:  values[10],
			transmitDrops:   values[11],
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	return counters, nil
}

// reportable applies the default selection. See the type comment.
func (n *Network) reportable(name string) bool {
	if name == "lo" {
		return false
	}
	if strings.ContainsAny(name, "/\\") {
		// Not a thing /proc/net/dev produces, and the name is about to be
		// used in a sysfs path.
		return false
	}
	for _, prefix := range excludedInterfacePrefixes {
		if strings.HasPrefix(name, prefix) {
			return false
		}
	}
	// Enslaved to a bridge or a bond: its traffic is counted on the master,
	// which is the meaningful unit.
	if _, err := os.Lstat(filepath.Join(n.SysRoot, "class", "net", name, "master")); err == nil {
		return false
	}
	return true
}

// operationalState reads /sys/class/net/<name>/operstate.
//
// Returns known=false for "unknown", which is what a tunnel or a virtual
// interface reports and which is not the same as "down". Reporting unknown
// as down would show every WireGuard tunnel as a dead link.
func (n *Network) operationalState(name string) (up bool, known bool) {
	raw, err := os.ReadFile(filepath.Join(n.SysRoot, "class", "net", name, "operstate")) //nolint:gosec // a name from /proc/net/dev, checked for separators
	if err != nil {
		return false, false
	}
	switch strings.TrimSpace(string(raw)) {
	case "up":
		return true, true
	case "down", "lowerlayerdown":
		return false, true
	default:
		// "unknown", "notpresent", "testing", "dormant".
		return false, false
	}
}

// linkSpeed reads /sys/class/net/<name>/speed, in bits per second.
//
// Absent rather than zero where the driver does not report it. Reading this
// file on a down interface or a wireless one returns EINVAL, and on a
// virtual one it does not exist — "0 bit/s" on a working Wi-Fi link would be
// a number the interface would render and nobody could explain.
func (n *Network) linkSpeed(name string) (bitsPerSecond float64, known bool) {
	raw, err := os.ReadFile(filepath.Join(n.SysRoot, "class", "net", name, "speed")) //nolint:gosec // a name from /proc/net/dev, checked for separators
	if err != nil {
		return 0, false
	}
	megabits, err := strconv.ParseFloat(strings.TrimSpace(string(raw)), 64)
	if err != nil || megabits <= 0 {
		// -1 is what the kernel writes for "not known", which is common on a
		// link that is down.
		return 0, false
	}
	return megabits * 1_000_000, true
}
