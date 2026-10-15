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

// DiskstatsSectorBytes is the unit of the sector counters in
// /proc/diskstats.
//
// **Always 512, on every device, regardless of the device's own sector
// size.** A 4K-native NVMe reports its throughput in 512-byte units here, and
// the kernel documentation (Documentation/admin-guide/iostats.rst) says so.
// Reading the device's logical_block_size from sysfs and using *that* is the
// obvious thing to do and is wrong by a factor of eight on exactly the
// hardware most likely to be in a modern homelab.
const DiskstatsSectorBytes = 512

// MaxDevices bounds how many block devices one host may report.
//
// Cardinality, as with [MaxCores] and [MaxMounts]. A machine with more than
// 64 whole block devices exists; a machine this product is for does not.
const MaxDevices = 64

// DiskIO reports throughput, operation rates and busy time per block device.
//
// # These are rates, computed here, and the first collection reports nothing
//
// /proc/diskstats holds monotonic counters since boot. A single snapshot says
// how much the machine has read since it started, which is not what anybody
// means by "disk throughput" — the same trap [CPU] documents at length, and
// for the same reason the rate is computed from the difference between two
// samples and the first collection after a start produces nothing.
//
// The division by elapsed time happens **here** rather than in the
// controller, and that is deliberate. The agent is the only component that
// knows both samples came from the same boot: it holds the previous counter
// and can tell a counter that went backwards — a reboot, a device
// re-enumerated — from one that advanced. The controller, given two counters
// from either side of a reboot, could only subtract them and produce a
// negative rate or a spike. Recorded as ADR-065.
//
// # Which devices, and why not all of them
//
//   - **Whole disks only.** A device is reported when /sys/block/<name>
//     exists, which is true for sda and dm-0 and false for sda1. Including
//     partitions would report the same I/O twice — once on the partition and
//     once on the disk containing it.
//   - **No loop, ram or zram devices.** A loop device is almost always a
//     mounted image whose real I/O is already counted on the disk holding
//     the image file; ram and zram are memory.
//   - **Device-mapper and md are included.** LVM volumes, LUKS containers
//     and RAID arrays are what a homelab's storage actually is, and omitting
//     them would leave the interesting layer invisible.
//
// # Stacked devices report the same bytes at every layer
//
// A write to an ext4 filesystem on an LVM volume on a LUKS container on a
// partition appears in the counters of dm-1, dm-0 and sda. That is the
// kernel telling the truth about three devices, and it means **summing
// across devices double-counts**. Nothing in this system sums them: there is
// no aggregate metric, and the interface charts per device. Stated here
// because the obvious next feature — "total disk throughput" — would be
// wrong by however many layers the host has.
//
// # A renamed device starts a new series
//
// Series identity is the device name, and the kernel does not promise sda is
// the same disk across reboots. A rename ends one series and begins another,
// which is visible in the interface as a chart that stops and a new one that
// starts — honest, and better than silently continuing a line with another
// disk's numbers. /dev/disk/by-id would be stable and is a symlink farm
// whose names are 40 characters of model and serial, over the controller's
// label-length bound and unreadable in a legend.
type DiskIO struct {
	// ProcRoot is where /proc is mounted; tests point it at a fixture.
	ProcRoot string
	// SysRoot is where /sys is mounted, used to tell a whole disk from a
	// partition. Tests point it at a fixture directory.
	SysRoot string
	// Now is the clock, injected so a test can state the interval exactly
	// rather than sleeping for it.
	Now func() time.Time

	mu       sync.Mutex
	previous map[string]diskCounters
	taken    time.Time
}

// DefaultSysRoot is where the kernel's device model is mounted.
const DefaultSysRoot = "/sys"

// diskCounters is the subset of /proc/diskstats this reads.
type diskCounters struct {
	readsCompleted  float64
	sectorsRead     float64
	writesCompleted float64
	sectorsWritten  float64
	// millisecondsBusy is field 10 (`io_ticks`): the time at least one
	// request was in flight. Not the sum of per-request service times, which
	// is field 4 plus field 8 and exceeds the wall clock on any device that
	// handles requests in parallel.
	millisecondsBusy float64
}

// NewDiskIO builds a disk throughput collector.
func NewDiskIO(procRoot, sysRoot string) *DiskIO {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	if sysRoot == "" {
		sysRoot = DefaultSysRoot
	}
	return &DiskIO{ProcRoot: procRoot, SysRoot: sysRoot, Now: time.Now}
}

// Module implements [Collector].
func (d *DiskIO) Module() string { return "diskio" }

// Available implements [Collector].
func (d *DiskIO) Available() Availability {
	path := d.diskstatsPath()
	if _, err := os.Stat(path); err != nil {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s is not readable, so block device activity cannot be measured", path,
			),
		}
	}
	counters, err := d.read()
	if err != nil {
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s cannot be parsed: %v", path, err),
		}
	}
	if len(counters) == 0 {
		return Availability{
			Available: false,
			Reason: "this host exposes no whole block devices — only partitions, loop " +
				"devices or memory-backed devices, none of which are reported separately",
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
//
// Returns an empty slice with no error on the first call: see the type
// comment for why one snapshot of a counter cannot produce a rate.
func (d *DiskIO) Collect(_ context.Context) ([]Sample, error) {
	current, err := d.read()
	if err != nil {
		return nil, err
	}
	clock := d.Now
	if clock == nil {
		clock = time.Now
	}
	now := clock()

	d.mu.Lock()
	previous := d.previous
	taken := d.taken
	d.previous = current
	d.taken = now
	d.mu.Unlock()

	if previous == nil {
		return nil, nil
	}
	elapsed := now.Sub(taken).Seconds()
	if elapsed <= 0 {
		// Two collections inside one clock tick, or a clock that went
		// backwards. Neither produces a meaningful rate, and dividing by it
		// produces an infinity that [Sample.Valid] would reject anyway —
		// returning nothing says the same thing without the detour.
		return nil, nil
	}

	samples := make([]Sample, 0, len(current)*5)
	for name, after := range current {
		before, had := previous[name]
		if !had {
			// A device that was not there last time: hot-plugged, or an
			// array assembled. No delta to take, so no sample; the next
			// interval has one.
			continue
		}
		readBytes, ok := rate(before.sectorsRead, after.sectorsRead, elapsed)
		if !ok {
			// A counter went backwards. On a block device that means the
			// device was removed and re-added, or the machine rebooted
			// between samples. The difference is meaningless, so the
			// interval produces nothing rather than a spike — the same rule
			// the CPU collector applies, and for the same reason.
			continue
		}
		writeBytes, ok := rate(before.sectorsWritten, after.sectorsWritten, elapsed)
		if !ok {
			continue
		}
		readOps, ok := rate(before.readsCompleted, after.readsCompleted, elapsed)
		if !ok {
			continue
		}
		writeOps, ok := rate(before.writesCompleted, after.writesCompleted, elapsed)
		if !ok {
			continue
		}
		busy, ok := rate(before.millisecondsBusy, after.millisecondsBusy, elapsed)
		if !ok {
			continue
		}

		labels := map[string]string{"device": name}
		samples = append(samples,
			Sample{
				Metric: "read_bytes", Labels: labels,
				Value: readBytes * DiskstatsSectorBytes,
			},
			Sample{
				Metric: "write_bytes", Labels: labels,
				Value: writeBytes * DiskstatsSectorBytes,
			},
			Sample{Metric: "read_ops", Labels: labels, Value: readOps},
			Sample{Metric: "write_ops", Labels: labels, Value: writeOps},
			// busy is milliseconds-busy per second, which is already a
			// per-mille figure: 1000 ms of busy in 1 s is 100%. Clamped by
			// `percent`, because io_ticks is sampled on a timer and can
			// exceed the interval by a tick on a device that was busy
			// throughout.
			Sample{
				Metric: "utilisation", Labels: labels,
				Value: percent(busy, 1000),
			},
		)
	}

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// rate returns the per-second change between two counter readings.
//
// Reports false when the counter went backwards, which is the signal that
// the two readings are not comparable. Equal readings are fine and mean the
// device was idle.
func rate(before, after, elapsedSeconds float64) (float64, bool) {
	if after < before {
		return 0, false
	}
	return (after - before) / elapsedSeconds, true
}

func (d *DiskIO) diskstatsPath() string {
	return filepath.Join(d.ProcRoot, "diskstats")
}

// read parses /proc/diskstats, keeping only whole block devices.
func (d *DiskIO) read() (map[string]diskCounters, error) {
	path := d.diskstatsPath()
	file, err := os.Open(path) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	defer file.Close()

	counters := make(map[string]diskCounters, 8)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		// major minor name then at least the eleven classic statistics.
		// Kernels since 4.18 add discard fields and since 5.5 flush fields;
		// extra trailing fields are ignored rather than refused, because a
		// newer kernel is not a parse error.
		if len(fields) < 14 {
			continue
		}
		name := fields[2]
		if !d.isWholeDisk(name) {
			continue
		}
		if len(counters) >= MaxDevices {
			continue
		}
		values := make([]float64, 11)
		malformed := false
		for index := range values {
			value, err := strconv.ParseFloat(fields[3+index], 64)
			if err != nil || value < 0 {
				malformed = true
				break
			}
			values[index] = value
		}
		if malformed {
			// One unparseable line is skipped rather than failing the file.
			// diskstats is written per device by different drivers, and a
			// single misbehaving one must not blank out every disk.
			continue
		}
		counters[name] = diskCounters{
			readsCompleted:   values[0],
			sectorsRead:      values[2],
			writesCompleted:  values[4],
			sectorsWritten:   values[6],
			millisecondsBusy: values[9],
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	return counters, nil
}

// isWholeDisk reports whether a diskstats name is a disk rather than one of
// its partitions.
//
// /sys/block holds a directory per whole device and a partition lives
// *inside* its disk's directory, so the test is exactly "does /sys/block/
// <name> exist". Checking the name instead — a trailing digit, a `p1` suffix
// — gets nvme0n1 wrong in one direction and dm-0 wrong in the other.
func (d *DiskIO) isWholeDisk(name string) bool {
	switch {
	case strings.HasPrefix(name, "loop"),
		strings.HasPrefix(name, "ram"),
		strings.HasPrefix(name, "zram"):
		return false
	}
	// A name with a path separator would escape /sys/block. diskstats never
	// produces one, which is why this is a guard rather than a parse.
	if strings.ContainsAny(name, "/\\") {
		return false
	}
	info, err := os.Stat(filepath.Join(d.SysRoot, "block", name))
	return err == nil && info.IsDir()
}
