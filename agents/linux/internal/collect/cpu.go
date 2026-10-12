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
)

// MaxCores bounds how many per-core series one host may produce.
//
// Not a limit on the machine — a 256-core host is real — but on *cardinality*.
// Per-core CPU at a one-second interval on a 256-core host is 256 series at
// 1 Hz from one module on one host, which is more than the rest of this
// phase's modules produce together. The bound is high enough that no realistic
// host hits it and low enough that a nonsense /proc/stat cannot make the agent
// allocate without limit; above it, the aggregate is still reported and the
// per-core series are dropped with a counted reason.
const MaxCores = 512

// CPU reads aggregate and per-core utilisation from /proc/stat.
//
// # Utilisation cannot be computed from one sample
//
// /proc/stat does not report utilisation. It reports **monotonic counters** of
// jiffies spent in each state since boot, so a single snapshot says only how
// the machine has been used since it started — which is not what anybody means
// by "CPU usage". Utilisation is the ratio of non-idle to total *change*
// between two snapshots, and the first collection after a start therefore has
// nothing to report.
//
// This is stated at length because the shortcut is tempting and wrong: taking
// `(total - idle) / total` from one snapshot produces a plausible-looking
// number that is the host's lifetime average, moves almost not at all, and
// would make every chart in the product a flat line nobody could explain.
//
// # Counter resets produce a gap, not a spike
//
// A counter that has gone *backwards* since the previous sample means the host
// rebooted (or, on an unusual platform, a register wrapped). The difference is
// meaningless either way, so the interval produces no sample and the baseline
// is replaced. The alternative — treating a negative delta as a large positive
// one — renders as several thousand percent utilisation at the exact moment an
// operator is trying to work out what happened during a reboot.
type CPU struct {
	// ProcRoot is where /proc is mounted. A field so tests read committed
	// fixtures rather than the build machine's live counters, which change
	// between runs and would make an assertion impossible.
	ProcRoot string

	// IncludePerCore reports a series per logical CPU as well as the
	// aggregate. Off by default: it multiplies this module's cardinality by
	// the core count, which is a cost the operator should opt into rather
	// than discover.
	IncludePerCore bool

	mu       sync.Mutex
	previous map[string]cpuTimes
}

// cpuTimes is one line of /proc/stat, in jiffies.
//
// Every field is kept even though only the idle ones and the total are used,
// because the total has to be the sum of *all* of them. Summing a subset is
// the other common way to get this wrong: omitting `steal` on a virtualised
// host makes utilisation read high by however much the hypervisor took.
type cpuTimes struct {
	user      float64
	nice      float64
	system    float64
	idle      float64
	iowait    float64
	irq       float64
	softirq   float64
	steal     float64
	guest     float64
	guestNice float64
}

// total is every jiffy the kernel accounted for.
//
// guest and guestNice are deliberately **excluded**: the kernel already counts
// guest time inside user, and guestNice inside nice, so adding them again
// double-counts and makes utilisation read low on a host running virtual
// machines. This is documented in proc(5) and is easy to miss.
func (t cpuTimes) total() float64 {
	return t.user + t.nice + t.system + t.idle + t.iowait + t.irq + t.softirq + t.steal
}

// idleTotal is time the CPU was not doing work.
//
// iowait counts as idle: the CPU was waiting for a device and was available
// for another runnable process. Counting it as busy is a common choice and
// makes a host doing heavy I/O look CPU-bound when it is not — the opposite of
// the diagnosis an operator needs.
func (t cpuTimes) idleTotal() float64 {
	return t.idle + t.iowait
}

// NewCPU builds a CPU collector.
func NewCPU(procRoot string, includePerCore bool) *CPU {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &CPU{ProcRoot: procRoot, IncludePerCore: includePerCore}
}

// Module implements [Collector].
func (c *CPU) Module() string { return "cpu" }

// Available implements [Collector].
//
// /proc/stat exists on every Linux kernel, including inside a container, so
// this is effectively always true — but it is checked rather than assumed,
// because a chroot or an unusual sandbox without /proc mounted is a real
// configuration and the operator should be told that rather than shown an
// empty chart.
func (c *CPU) Available() Availability {
	path := filepath.Join(c.ProcRoot, "stat")
	if _, err := os.Stat(path); err != nil {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s is not readable, so CPU time cannot be measured on this host", path,
			),
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
//
// Returns an empty slice with no error on the first call: see the type comment
// for why one snapshot cannot produce a utilisation.
func (c *CPU) Collect(_ context.Context) ([]Sample, error) {
	current, err := c.read()
	if err != nil {
		return nil, err
	}

	c.mu.Lock()
	previous := c.previous
	c.previous = current
	c.mu.Unlock()

	samples := make([]Sample, 0, len(current)+1)

	// Reported from the current snapshot rather than from a delta: the core
	// count is a fact about the host, not a rate, so it is available
	// immediately and on every collection.
	if cores := len(current) - 1; cores > 0 {
		samples = append(samples, Sample{Metric: "logical_cpus", Value: float64(cores)})
	}

	if previous == nil {
		// No baseline. Not an error — "not yet" is not a failure, and
		// reporting it as one would make every agent start with a collector
		// error in the interface.
		kept, _ := keepValid(samples)
		sortSamples(kept)
		return kept, nil
	}

	if aggregate, ok := utilisation(previous["cpu"], current["cpu"]); ok {
		samples = append(samples, Sample{Metric: "utilisation", Value: aggregate})
	}

	if c.IncludePerCore {
		cores := 0
		for name, now := range current {
			if name == "cpu" {
				continue
			}
			if cores >= MaxCores {
				continue
			}
			before, had := previous[name]
			if !had {
				// A core that was not in the previous snapshot — a hot-plugged
				// CPU, or a container's cpuset widening. No delta to take, so
				// no sample; the next interval has one.
				continue
			}
			if value, ok := utilisation(before, now); ok {
				samples = append(samples, Sample{
					Metric: "core_utilisation",
					Labels: map[string]string{"core": strings.TrimPrefix(name, "cpu")},
					Value:  value,
				})
				cores++
			}
		}
	}

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// utilisation computes the percentage of non-idle time between two snapshots.
//
// Returns false when there is nothing meaningful to report: a zero-length
// interval (two collections within the same jiffy) or a counter that went
// backwards.
func utilisation(before, after cpuTimes) (float64, bool) {
	totalDelta := after.total() - before.total()
	idleDelta := after.idleTotal() - before.idleTotal()

	switch {
	case totalDelta <= 0:
		// Zero means two samples inside one jiffy, which happens if something
		// collects twice in quick succession. Negative means the counters
		// reset — a reboot — and the difference is meaningless.
		return 0, false
	case idleDelta < 0:
		// Total moved forward and idle moved back. Not a reboot; more likely a
		// partially-read /proc/stat or a platform oddity. Either way the ratio
		// would be above one and the interval is not trustworthy.
		return 0, false
	}
	return percent(totalDelta-idleDelta, totalDelta), true
}

// read parses /proc/stat into per-CPU counters.
//
// Keyed by the line's own label — "cpu" for the aggregate, "cpu0", "cpu1" for
// each logical CPU — rather than by index, because a container's cpuset can
// expose a non-contiguous set and indexing by position would mislabel them.
func (c *CPU) read() (map[string]cpuTimes, error) {
	path := filepath.Join(c.ProcRoot, "stat")
	file, err := os.Open(path) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	defer file.Close()

	times := make(map[string]cpuTimes, 8)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 5 || !strings.HasPrefix(fields[0], "cpu") {
			// Every other line of /proc/stat — intr, ctxt, btime, processes —
			// is skipped rather than parsed. Fewer than five fields is a
			// truncated line, which is refused for the whole file below only
			// if it leaves no usable CPU line at all.
			continue
		}
		parsed, err := parseCPUTimes(fields[1:])
		if err != nil {
			return nil, fmt.Errorf("cannot parse %s line %q: %w", path, fields[0], err)
		}
		times[fields[0]] = parsed
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	if _, ok := times["cpu"]; !ok {
		// Without the aggregate line the file is not /proc/stat. Refused
		// rather than worked around, because a file this far from expected is
		// more likely a mount problem than a kernel this agent should support.
		return nil, fmt.Errorf("%s has no aggregate cpu line", path)
	}
	return times, nil
}

// parseCPUTimes reads the jiffy counters from one /proc/stat cpu line.
//
// Tolerant of a *short* line and strict about a malformed one. The field count
// has grown across kernel versions — steal arrived in 2.6.11, guest in 2.6.24,
// guest_nice in 2.6.33 — so a kernel older than this agent produces fewer
// fields, and refusing that would make the agent unusable on a host it could
// otherwise monitor. A field that is present and not a number is a different
// matter and is an error: it means the file is not what it claims to be.
func parseCPUTimes(fields []string) (cpuTimes, error) {
	values := make([]float64, 10)
	for index := range values {
		if index >= len(fields) {
			break
		}
		value, err := strconv.ParseFloat(fields[index], 64)
		if err != nil {
			return cpuTimes{}, fmt.Errorf("field %d (%q) is not a number: %w", index, fields[index], err)
		}
		if value < 0 {
			return cpuTimes{}, fmt.Errorf("field %d is negative (%s)", index, fields[index])
		}
		values[index] = value
	}
	return cpuTimes{
		user:      values[0],
		nice:      values[1],
		system:    values[2],
		idle:      values[3],
		iowait:    values[4],
		irq:       values[5],
		softirq:   values[6],
		steal:     values[7],
		guest:     values[8],
		guestNice: values[9],
	}, nil
}
