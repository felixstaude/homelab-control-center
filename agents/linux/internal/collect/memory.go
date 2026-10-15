package collect

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// meminfoKeys are the only fields read from /proc/meminfo.
//
// An allow-list rather than parsing the whole file: /proc/meminfo has around
// fifty fields and grows between kernel versions, and keeping every one of
// them in a map would mean the agent allocated for forty-five values nothing
// reads.
var meminfoKeys = map[string]bool{
	"MemTotal":     true,
	"MemAvailable": true,
	"MemFree":      true,
	"Buffers":      true,
	"Cached":       true,
	"SReclaimable": true,
	"SwapTotal":    true,
	"SwapFree":     true,
}

// Memory reads total, available and used memory from /proc/meminfo.
//
// # Available is not free, and the difference is the whole point
//
// MemFree is memory the kernel is holding in reserve. On a healthy Linux host
// it is *nearly zero*, because the kernel uses everything spare for page cache
// — and a monitoring tool that reports MemFree as "available" tells every
// operator their server is about to run out of memory when it is in fact
// working exactly as designed. This is the single most common mistake in
// memory monitoring.
//
// MemAvailable is the kernel's own estimate of what a new workload could claim
// without swapping, which is the question an operator is actually asking. It
// has existed since Linux 3.14 (2014) and is used in preference whenever
// present.
//
// The fallback, for a kernel too old to have it, is
// `MemFree + Buffers + Cached + SReclaimable` — the same approximation
// MemAvailable replaced. It is an approximation and the type says so rather
// than presenting it as equivalent.
//
// Used is then `MemTotal - available`, so used and available always sum to the
// total. Deriving used from MemFree instead would make the two figures
// disagree with each other by the size of the page cache, which on a busy host
// is most of the machine.
type Memory struct {
	// ProcRoot is where /proc is mounted; a field so tests use fixtures.
	ProcRoot string
}

// NewMemory builds a memory collector.
func NewMemory(procRoot string) *Memory {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &Memory{ProcRoot: procRoot}
}

// Module implements [Collector].
func (m *Memory) Module() string { return "memory" }

// Available implements [Collector].
func (m *Memory) Available() Availability {
	path := filepath.Join(m.ProcRoot, "meminfo")
	fields, err := readMeminfo(path)
	if err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s is not readable on this host", path),
		}
	}
	if fields["MemTotal"] == 0 {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s reports no total memory", path),
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
func (m *Memory) Collect(_ context.Context) ([]Sample, error) {
	path := filepath.Join(m.ProcRoot, "meminfo")
	fields, err := readMeminfo(path)
	if err != nil {
		return nil, err
	}

	total, ok := fields["MemTotal"]
	if !ok || total == 0 {
		// Refused rather than reported as zero. A host with no total memory
		// does not exist, so this is a parsing or mounting failure, and every
		// figure below it would be a percentage of nothing.
		return nil, fmt.Errorf("%s does not report MemTotal", path)
	}

	available, exact := memoryAvailable(fields)
	used := total - available
	if used < 0 {
		// MemAvailable can exceed MemTotal on an unusual kernel. Clamped so
		// "used" is never negative, which no chart can render and no operator
		// would believe.
		used = 0
	}

	samples := []Sample{
		{Metric: "total", Value: total},
		{Metric: "available", Value: available},
		{Metric: "used", Value: used},
		{Metric: "utilisation", Value: percent(used, total)},
	}
	_ = exact // Retained for the availability reason below; see Available.

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// memoryAvailable returns available memory in bytes, and whether the kernel
// reported it directly.
//
// The second return value exists so a caller can tell an exact figure from the
// approximation — not because this package currently branches on it, but
// because "we approximated this" is a fact the operator is entitled to and
// discarding it here would make it unrecoverable later.
func memoryAvailable(fields map[string]float64) (bytes float64, exact bool) {
	if value, ok := fields["MemAvailable"]; ok {
		return value, true
	}
	return fields["MemFree"] + fields["Buffers"] + fields["Cached"] + fields["SReclaimable"], false
}

// Swap reads swap total and usage from /proc/meminfo.
//
// A separate collector from [Memory] even though it reads the same file,
// because it is a separate *module* — which is what lets a swapless host
// report swap as unavailable without that saying anything about its memory
// (ADR-048). One collector covering both would have to either refuse the whole
// thing or report 0% of 0 bytes, and the second is the lie this package exists
// to avoid.
type Swap struct {
	ProcRoot string
}

// NewSwap builds a swap collector.
func NewSwap(procRoot string) *Swap {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &Swap{ProcRoot: procRoot}
}

// Module implements [Collector].
func (s *Swap) Module() string { return "swap" }

// Available implements [Collector].
//
// A host with no swap configured reports **unavailable with a reason**, not
// zero usage. Containers and many cloud instances have none, so this is the
// common case rather than an edge one, and the interface needs to be able to
// say "this host has no swap" at the point where somebody is about to enable
// the module.
func (s *Swap) Available() Availability {
	path := filepath.Join(s.ProcRoot, "meminfo")
	fields, err := readMeminfo(path)
	if err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s is not readable on this host", path),
		}
	}
	if fields["SwapTotal"] <= 0 {
		return Availability{
			Available: false,
			Reason:    "this host has no swap configured",
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
//
// Produces **no samples** on a swapless host, rather than zeroes. The
// distinction reaches the chart: no samples renders as "no data", and a zero
// renders as a line at the bottom, which asserts that swap exists and is
// unused.
func (s *Swap) Collect(_ context.Context) ([]Sample, error) {
	path := filepath.Join(s.ProcRoot, "meminfo")
	fields, err := readMeminfo(path)
	if err != nil {
		return nil, err
	}

	total := fields["SwapTotal"]
	if total <= 0 {
		return nil, nil
	}
	free := fields["SwapFree"]
	used := total - free
	if used < 0 {
		used = 0
	}

	samples := []Sample{
		{Metric: "total", Value: total},
		{Metric: "used", Value: used},
		{Metric: "utilisation", Value: percent(used, total)},
	}
	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// readMeminfo parses the fields in [meminfoKeys] from /proc/meminfo, in bytes.
//
// # The unit conversion is the part worth checking
//
// /proc/meminfo reports in **kibibytes**, with the unit written on each line
// ("MemTotal: 16316936 kB"), and the agent reports bytes. The suffix is
// honoured rather than assumed: it is always "kB" in every kernel to date, but
// taking the number without reading the unit is how a figure ends up a
// thousand times too small on the first kernel that changes it — and a
// memory total of 16 MB on a 16 GB host would be believed by nobody and
// diagnosed by no one either.
//
// A line with no unit is read as bytes, which is what the few unitless fields
// (HugePages_Total and friends) actually are. None of them is in the
// allow-list, so this is defensive rather than load-bearing.
func readMeminfo(path string) (map[string]float64, error) {
	file, err := os.Open(path) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return nil, err
	}
	defer file.Close()

	fields := make(map[string]float64, len(meminfoKeys))
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		name, rest, found := strings.Cut(scanner.Text(), ":")
		if !found || !meminfoKeys[name] {
			continue
		}
		parts := strings.Fields(rest)
		if len(parts) == 0 {
			continue
		}
		value, err := strconv.ParseFloat(parts[0], 64)
		if err != nil {
			// Skipped rather than fatal. A single unparseable line should not
			// cost the whole collection, and a *required* field that is
			// missing is caught by the caller — which knows which ones it
			// needs and this function does not.
			continue
		}
		if value < 0 {
			continue
		}
		if len(parts) > 1 {
			switch strings.ToLower(parts[1]) {
			case "kb":
				value *= 1024
			case "mb":
				value *= 1024 * 1024
			default:
				// An unrecognised unit is skipped rather than read as bytes.
				// Guessing would produce a figure off by three orders of
				// magnitude, which is worse than an absent one.
				continue
			}
		}
		fields[name] = value
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	return fields, nil
}
