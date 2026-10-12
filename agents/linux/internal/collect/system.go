package collect

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Load reads the 1, 5 and 15-minute load averages from /proc/loadavg.
//
// # Load average is not a CPU percentage
//
// It is a count of processes that are runnable or in uninterruptible sleep,
// averaged over a window with an exponential decay. On an 8-core host a load
// of 8 is full utilisation and a load of 16 is a queue; on a 1-core host a
// load of 8 is a serious problem. So it has no upper bound, it is not a
// percentage, and dividing it by the core count to make one would produce a
// figure that is neither.
//
// This is why `load` is a module separate from `cpu` (ADR-048) and why its
// unit in the registry is `load` rather than `percent`: the distinction is in
// the data model, where a UI convention cannot erode it.
type Load struct {
	ProcRoot string
}

// NewLoad builds a load-average collector.
func NewLoad(procRoot string) *Load {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &Load{ProcRoot: procRoot}
}

// Module implements [Collector].
func (l *Load) Module() string { return "load" }

// Available implements [Collector].
func (l *Load) Available() Availability {
	path := filepath.Join(l.ProcRoot, "loadavg")
	if _, err := os.Stat(path); err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s is not readable on this host", path),
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
//
// One series per window, labelled — rather than three metrics — because the
// interface draws them on one chart and they are the same measurement over
// different windows.
func (l *Load) Collect(_ context.Context) ([]Sample, error) {
	path := filepath.Join(l.ProcRoot, "loadavg")
	raw, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}

	fields := strings.Fields(string(raw))
	if len(fields) < 3 {
		return nil, fmt.Errorf("%s has %d fields, expected at least 3", path, len(fields))
	}

	windows := [...]string{"1m", "5m", "15m"}
	samples := make([]Sample, 0, len(windows))
	for index, window := range windows {
		value, err := strconv.ParseFloat(fields[index], 64)
		if err != nil {
			return nil, fmt.Errorf("%s field %d (%q) is not a number: %w", path, index, fields[index], err)
		}
		if value < 0 {
			// A negative load does not exist. Refused rather than clamped,
			// because unlike a utilisation slightly over 100% this is not a
			// sampling artefact — it means the file is not /proc/loadavg.
			return nil, fmt.Errorf("%s reports a negative load (%s)", path, fields[index])
		}
		samples = append(samples, Sample{
			Metric: "average",
			Labels: map[string]string{"window": window},
			Value:  value,
		})
	}

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// Uptime reads how long the host has been running, from /proc/uptime.
//
// # This is the host's uptime, not the agent's
//
// Stated because the two are easy to confuse and the difference matters: the
// agent's own uptime is already reported on every heartbeat, and an operator
// looking at "uptime" on a host page means the machine. A host that has been
// up for two years running an agent restarted this morning has one of each,
// and reporting the wrong one would make a reboot invisible.
//
// The value is a gauge rather than a counter even though it only increases,
// because a reboot resets it and that reset *is* the signal. Counter semantics
// would record the reset as a gap and hide exactly the event worth seeing.
type Uptime struct {
	ProcRoot string
}

// NewUptime builds an uptime collector.
func NewUptime(procRoot string) *Uptime {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &Uptime{ProcRoot: procRoot}
}

// Module implements [Collector].
func (u *Uptime) Module() string { return "uptime" }

// Available implements [Collector].
func (u *Uptime) Available() Availability {
	path := filepath.Join(u.ProcRoot, "uptime")
	if _, err := os.Stat(path); err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s is not readable on this host", path),
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
func (u *Uptime) Collect(_ context.Context) ([]Sample, error) {
	path := filepath.Join(u.ProcRoot, "uptime")
	raw, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}

	fields := strings.Fields(string(raw))
	if len(fields) == 0 {
		return nil, fmt.Errorf("%s is empty", path)
	}
	// The second field is idle time summed across all CPUs, which is not
	// uptime and is not collected: on a multi-core host it exceeds the
	// uptime, and reporting it alongside would invite the comparison.
	seconds, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return nil, fmt.Errorf("%s field 0 (%q) is not a number: %w", path, fields[0], err)
	}
	if seconds < 0 {
		return nil, fmt.Errorf("%s reports a negative uptime (%s)", path, fields[0])
	}

	samples := []Sample{{Metric: "seconds", Value: seconds}}
	kept, _ := keepValid(samples)
	return kept, nil
}
