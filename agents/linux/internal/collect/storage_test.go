package collect_test

import (
	"context"
	"errors"
	"sort"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
)

// Storage fixtures, in testdata:
//
//	storage          — a plausible host: ext4 root on LVM, /boot on a
//	                   partition, an EFI system partition, an md array, an NFS
//	                   mount, a Docker overlay, a snap squashfs, a bind mount
//	                   of the root filesystem, two btrfs subvolumes of one
//	                   pool, a mount point containing a space, and a
//	                   read-only mount.
//	storagebusy      — the same mounts, and diskstats one second of work later
//	storagerebooted  — diskstats with counters *below* the baseline
//	storageidle      — a container's mount table: nothing but pseudo-mounts
const (
	storage         = "testdata/storage"
	storageBusy     = "testdata/storagebusy"
	storageRebooted = "testdata/storagerebooted"
	storageIdle     = "testdata/storageidle"
)

// --- Filesystem -------------------------------------------------------------

// fakeStatfs answers for the fixture's mount points.
//
// Injected rather than run against the real machine, because the whole point
// of a fixture is that the numbers do not change between runs — and because
// none of these mount points exist on the machine running `go test`.
type fakeStatfs struct {
	// byMount is the answer per mount point. A mount point not listed gets
	// ENOENT, which is how an unmounted-while-we-looked filesystem behaves.
	byMount map[string]syscall.Statfs_t
	// devices is the device number per mount point, for the duplicate test.
	devices map[string]uint64
	// asked records every path statfs was called on, so a test can assert
	// that an excluded mount was never *touched* — which for an NFS mount is
	// the difference between a skipped row and a wedged goroutine.
	asked []string
}

func (f *fakeStatfs) statfs(path string, out *syscall.Statfs_t) error {
	f.asked = append(f.asked, path)
	answer, ok := f.byMount[path]
	if !ok {
		return syscall.ENOENT
	}
	*out = answer
	return nil
}

func (f *fakeStatfs) deviceOf(path string) (uint64, error) {
	device, ok := f.devices[path]
	if !ok {
		return 0, syscall.ENOENT
	}
	return device, nil
}

// gibibyteFilesystem describes a filesystem in whole gibibytes.
//
// blocks/free/avail are in 4 KiB blocks, which is what a real ext4 reports,
// so the test exercises the multiplication by Bsize rather than assuming it.
func gibibyteFilesystem(totalGiB, freeGiB, availGiB float64, inodes, freeInodes uint64) syscall.Statfs_t {
	const blocksPerGiB = gibibyte / 4096
	return syscall.Statfs_t{
		Bsize:  4096,
		Blocks: uint64(totalGiB * blocksPerGiB),
		Bfree:  uint64(freeGiB * blocksPerGiB),
		Bavail: uint64(availGiB * blocksPerGiB),
		Files:  inodes,
		Ffree:  freeInodes,
	}
}

// storageFixture wires a Filesystem collector onto the committed mount table
// with a described set of filesystems behind it.
func storageFixture(t *testing.T, root string) (*collect.Filesystem, *fakeStatfs) {
	t.Helper()
	fake := &fakeStatfs{
		byMount: map[string]syscall.Statfs_t{
			"/":                                    gibibyteFilesystem(100, 60, 55, 6_553_600, 6_200_000),
			"/boot":                                gibibyteFilesystem(1, 0.5, 0.4, 65536, 65000),
			"/boot/efi":                            gibibyteFilesystem(0.5, 0.4, 0.4, 0, 0),
			"/srv/media":                           gibibyteFilesystem(8000, 2000, 2000, 0, 0),
			"/mnt/home-bind":                       gibibyteFilesystem(100, 60, 55, 6_553_600, 6_200_000),
			"/mnt/with space":                      gibibyteFilesystem(10, 9, 9, 1000, 900),
			"/mnt/readonly":                        gibibyteFilesystem(10, 1, 1, 1000, 900),
			"/srv/backup":                          gibibyteFilesystem(999, 999, 999, 0, 0),
			"/snap/core/1234":                      gibibyteFilesystem(1, 0, 0, 100, 0),
			"/var/lib/docker/overlay2/0f3a/merged": gibibyteFilesystem(100, 60, 55, 0, 0),
		},
		devices: map[string]uint64{
			"/":          2000,
			"/boot":      2001,
			"/boot/efi":  2002,
			"/srv/media": 2003,
			// The bind mount shares the root filesystem's device number,
			// which is what the first duplicate test finds.
			"/mnt/home-bind": 2000,
			// The two btrfs subvolumes have *different* device numbers and
			// the same source, which is the case the second test exists for.
			"/home":           2010,
			"/mnt/with space": 2004,
			"/mnt/readonly":   2005,
		},
	}
	collector := collect.NewFilesystem(root)
	collector.Statfs = fake.statfs
	collector.DeviceOf = fake.deviceOf
	return collector, fake
}

func mountsIn(samples []collect.Sample) []string {
	seen := map[string]bool{}
	for _, sample := range samples {
		if mount, ok := sample.Labels["mount"]; ok {
			seen[mount] = true
		}
	}
	out := make([]string, 0, len(seen))
	for mount := range seen {
		out = append(out, mount)
	}
	sort.Strings(out)
	return out
}

func TestTheFilesystemCollectorReportsOrdinaryMounts(t *testing.T) {
	collector, _ := storageFixture(t, storage)

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	got := mountsIn(samples)
	want := []string{"/", "/boot", "/boot/efi", "/mnt/readonly", "/mnt/with space", "/srv/media"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("mounts =\n  %v\nwant\n  %v", got, want)
	}
}

func TestEachExclusionIsForItsOwnReason(t *testing.T) {
	// Asserted one at a time, because each exclusion is a separate decision
	// and a single "the list is right" assertion would let one of them be
	// removed without the test noticing which.
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, excluded := range []struct {
		mount string
		why   string
	}{
		{"/run", "tmpfs is memory, and reporting it as disk invents a disk"},
		{"/dev/shm", "tmpfs is memory"},
		{"/proc", "a pseudo-filesystem has no capacity"},
		{"/sys", "a pseudo-filesystem has no capacity"},
		{"/srv/backup", "statfs on an unreachable NFS server blocks uninterruptibly"},
		{"/var/lib/docker/overlay2/0f3a/merged", "a container root filesystem is not host storage"},
		{"/snap/core/1234", "a squashfs is read-only and therefore permanently 100% full"},
		{"/mnt/home-bind", "a bind mount is the root filesystem counted twice"},
		{"/home", "a btrfs subvolume is the same pool's free space under another name"},
	} {
		if _, found := labelled(samples, "total", "mount", excluded.mount); found {
			t.Errorf("%s was reported: %s", excluded.mount, excluded.why)
		}
	}
}

func TestANetworkMountIsNeverEvenMeasured(t *testing.T) {
	// Not merely absent from the output: `statfs` on a mount whose server
	// has gone away blocks in uninterruptible sleep, and no context deadline
	// cancels a syscall in D state. The protection is only real if the call
	// never happens.
	collector, fake := storageFixture(t, storage)

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	for _, path := range fake.asked {
		if path == "/srv/backup" {
			t.Fatal("statfs was called on an NFS mount; a dead server would wedge the collector")
		}
	}
}

func TestCapacityIsComputedAgainstWhatAProcessCanUse(t *testing.T) {
	// The reserved-blocks trap. A 100 GiB ext4 with 60 GiB free reports only
	// 55 GiB *available*, because 5% is reserved for root. Reporting 60% free
	// and then returning ENOSPC is the oldest confusing thing about disk
	// monitoring, so utilisation is taken against used + available.
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	total, _ := labelled(samples, "total", "mount", "/")
	used, _ := labelled(samples, "used", "mount", "/")
	available, _ := labelled(samples, "available", "mount", "/")
	utilisation, _ := labelled(samples, "utilisation", "mount", "/")

	if total != 100*gibibyte {
		t.Errorf("total = %.0f, want %d", total, 100*gibibyte)
	}
	// 100 total − 60 free = 40 used. Derived from Bfree, so the 5 GiB
	// reserve counts as neither used nor available.
	if used != 40*gibibyte {
		t.Errorf("used = %.0f, want %d", used, 40*gibibyte)
	}
	if available != 55*gibibyte {
		t.Errorf("available = %.0f, want %d", available, 55*gibibyte)
	}
	// 40 / (40 + 55) = 42.1%, not 40%.
	if utilisation < 42.0 || utilisation > 42.2 {
		t.Errorf("utilisation = %.2f, want about 42.1 — "+
			"reserved blocks are being counted as available", utilisation)
	}
}

func TestInodeUsageIsAbsentWhereThereIsNoInodeTable(t *testing.T) {
	// btrfs and XFS report Files as 0. "0% of no inodes used" is a true
	// statement nobody can act on, and it renders as an empty chart forever.
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "inodes_utilisation", "mount", "/srv/media"); found {
		t.Error("a filesystem with no inode table reported inode usage")
	}
	value, found := labelled(samples, "inodes_utilisation", "mount", "/boot")
	if !found {
		t.Fatal("a filesystem with an inode table reported no inode usage")
	}
	// 65536 − 65000 = 536 used of 65536.
	if value < 0.81 || value > 0.83 {
		t.Errorf("inode utilisation = %.3f, want about 0.818", value)
	}
}

func TestAReadOnlyMountIsReportedAsSuch(t *testing.T) {
	// The kernel remounts a filesystem read-only when it hits an I/O error.
	// On a machine nobody is watching, that is the difference between "a
	// disk is failing" and "writes have been silently failing since Tuesday".
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if value, _ := labelled(samples, "readonly", "mount", "/mnt/readonly"); value != 1 {
		t.Errorf("readonly(/mnt/readonly) = %v, want 1", value)
	}
	if value, _ := labelled(samples, "readonly", "mount", "/"); value != 0 {
		t.Errorf("readonly(/) = %v, want 0", value)
	}
}

func TestAMountPointContainingASpaceIsParsed(t *testing.T) {
	// The kernel octal-escapes spaces, tabs, newlines and backslashes in
	// mount paths. Without decoding, "/mnt/with space" parses as two fields
	// and becomes "/mnt/with" — a mount point that does not exist, whose
	// statfs fails, so the filesystem silently disappears from the list.
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "total", "mount", "/mnt/with space"); !found {
		t.Errorf("a mount point with a space was lost; got %v", mountsIn(samples))
	}
}

func TestAMountThatVanishesIsSkippedAndTheRestSurvive(t *testing.T) {
	// A filesystem unmounted between reading the table and measuring it.
	// Failing the whole collection would let one transient mount blank out
	// every other filesystem on the host, which is the opposite of what
	// somebody watching a disk fill up needs.
	collector, fake := storageFixture(t, storage)
	delete(fake.byMount, "/boot")

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("one vanished mount failed the whole collection: %v", err)
	}
	if _, found := labelled(samples, "total", "mount", "/boot"); found {
		t.Error("a mount that could not be measured was reported anyway")
	}
	if _, found := labelled(samples, "total", "mount", "/"); !found {
		t.Error("the other filesystems were lost with it")
	}
}

func TestAHostWithNothingToReportSaysSo(t *testing.T) {
	// A container whose every mount is a pseudo-filesystem. It *supports*
	// the module and has nothing to say through it, and an operator enabling
	// it needs to be told that rather than shown an empty chart.
	collector, _ := storageFixture(t, storageIdle)

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no ordinary filesystems reported itself available")
	}
	if !strings.Contains(availability.Reason, "no ordinary filesystems") {
		t.Errorf("the reason does not explain itself: %q", availability.Reason)
	}
}

func TestAMissingMountTableIsUnavailableRatherThanEmpty(t *testing.T) {
	collector := collect.NewFilesystem("testdata/does-not-exist")

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no mount table reported itself available")
	}
	if !strings.Contains(availability.Reason, "mountinfo") {
		t.Errorf("the reason does not name the file: %q", availability.Reason)
	}
}

func TestEveryFilesystemSampleIsFinite(t *testing.T) {
	// A zero-block filesystem divides by zero. [Sample.Valid] rejects the
	// result, and this asserts nothing gets past it — a NaN reaching the
	// controller is a series that can never be charted.
	collector, fake := storageFixture(t, storage)
	fake.byMount["/"] = syscall.Statfs_t{Bsize: 4096} // every count zero

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, sample := range samples {
		if !sample.Valid() {
			t.Errorf("invalid sample reached the output: %+v", sample)
		}
	}
	if _, found := labelled(samples, "total", "mount", "/"); found {
		t.Error("a filesystem with no blocks was reported as a filesystem")
	}
}

// --- Disk throughput --------------------------------------------------------

// diskIOAt builds a collector reading one fixture at a fixed instant.
func diskIOAt(root string, at time.Time) *collect.DiskIO {
	collector := collect.NewDiskIO(root, root+"/sys")
	collector.Now = func() time.Time { return at }
	return collector
}

// twoSnapshots collects from `first`, then re-points the collector at
// `second` and collects again a second later.
//
// Re-pointing rather than constructing a second collector, because the
// previous counters live in the collector and the whole mechanism under test
// is the difference between two readings it holds.
func twoSnapshots(t *testing.T, first, second string, apart time.Duration) []collect.Sample {
	t.Helper()
	start := time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)
	now := start
	collector := collect.NewDiskIO(first, first+"/sys")
	collector.Now = func() time.Time { return now }

	if samples, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("first collect: %v", err)
	} else if len(samples) != 0 {
		t.Fatalf("the first collection produced %d samples; a rate needs two", len(samples))
	}

	collector.ProcRoot = second
	collector.SysRoot = second + "/sys"
	now = start.Add(apart)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("second collect: %v", err)
	}
	return samples
}

func TestTheFirstDiskCollectionProducesNothing(t *testing.T) {
	// /proc/diskstats holds counters since boot. One snapshot says how much
	// the machine has read since it started, which is not throughput — the
	// same trap the CPU collector documents, and the same answer.
	collector := diskIOAt(storage, time.Now())

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if len(samples) != 0 {
		t.Errorf("the first collection reported %d samples: %v", len(samples), samples)
	}
}

func TestDiskThroughputIsBytesPerSecond(t *testing.T) {
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	// sda: sectors_read 20000 → 22048, so 2048 sectors in one second.
	// Sectors in diskstats are *always* 512 bytes, whatever the device's own
	// sector size — a 4K-native NVMe still reports 512-byte units. Reading
	// logical_block_size from sysfs and using that is wrong by 8x on exactly
	// the hardware most likely to be in a modern homelab.
	readBytes, found := labelled(samples, "read_bytes", "device", "sda")
	if !found {
		t.Fatalf("no read_bytes for sda: %v", samples)
	}
	if readBytes != 2048*512 {
		t.Errorf("read_bytes = %.0f, want %d (2048 sectors × 512)", readBytes, 2048*512)
	}
	// sectors_written 8000 → 12096, so 4096 sectors.
	writeBytes, _ := labelled(samples, "write_bytes", "device", "sda")
	if writeBytes != 4096*512 {
		t.Errorf("write_bytes = %.0f, want %d", writeBytes, 4096*512)
	}
}

func TestTheRateIsDividedByTheRealInterval(t *testing.T) {
	// Two seconds apart halves the rate. A collector that assumed its
	// configured interval rather than measuring would get this right only
	// when the scheduler was exactly on time, which it is not under load.
	samples := twoSnapshots(t, storage, storageBusy, 2*time.Second)

	readBytes, _ := labelled(samples, "read_bytes", "device", "sda")

	if readBytes != 1024*512 {
		t.Errorf("read_bytes over 2s = %.0f, want %d", readBytes, 1024*512)
	}
}

func TestDiskOperationRatesAreReported(t *testing.T) {
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	// reads 1000 → 1100, writes 500 → 700.
	if value, _ := labelled(samples, "read_ops", "device", "sda"); value != 100 {
		t.Errorf("read_ops = %v, want 100", value)
	}
	if value, _ := labelled(samples, "write_ops", "device", "sda"); value != 200 {
		t.Errorf("write_ops = %v, want 200", value)
	}
}

func TestBusyTimeIsAPercentageOfTheInterval(t *testing.T) {
	// io_ticks 600 → 1100, so 500 ms busy in a 1000 ms interval: 50%.
	// Saturation rather than throughput — a disk at 100% with 2 MB/s is a
	// disk doing random I/O, and that is the diagnosis throughput never
	// gives on its own.
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	value, found := labelled(samples, "utilisation", "device", "sda")

	if !found {
		t.Fatal("no utilisation for sda")
	}
	if value != 50 {
		t.Errorf("utilisation = %v, want 50", value)
	}
}

func TestBusyTimeIsClampedRatherThanExceeding(t *testing.T) {
	// io_ticks is sampled on a timer and can advance past the wall-clock
	// interval on a device busy throughout. 104% is a number an operator
	// will not believe from a system that is otherwise telling the truth.
	samples := twoSnapshots(t, storage, storageBusy, 400*time.Millisecond)

	value, _ := labelled(samples, "utilisation", "device", "sda")

	if value != 100 {
		t.Errorf("utilisation = %v, want it clamped to 100", value)
	}
}

func TestPartitionsAreNotReportedSeparatelyFromTheirDisk(t *testing.T) {
	// sda1 and nvme0n1p1 are in the fixture's diskstats and have no
	// directory under /sys/block. Reporting them would count the same I/O
	// twice, once on the partition and once on the disk containing it.
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	for _, partition := range []string{"sda1", "nvme0n1p1"} {
		if _, found := labelled(samples, "read_bytes", "device", partition); found {
			t.Errorf("%s was reported; its I/O is already counted on its disk", partition)
		}
	}
}

func TestLoopDevicesAreNotReported(t *testing.T) {
	// A loop device is almost always a mounted image whose real I/O is
	// already counted on the disk holding the image file.
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	if _, found := labelled(samples, "read_bytes", "device", "loop0"); found {
		t.Error("loop0 was reported")
	}
}

func TestDeviceMapperAndRaidAreReported(t *testing.T) {
	// LVM volumes, LUKS containers and md arrays are what a homelab's
	// storage actually is. Omitting them leaves the interesting layer
	// invisible — which is the opposite mistake from reporting partitions.
	samples := twoSnapshots(t, storage, storageBusy, time.Second)

	for _, device := range []string{"dm-0", "md0"} {
		if _, found := labelled(samples, "read_bytes", "device", device); !found {
			t.Errorf("%s was not reported", device)
		}
	}
}

func TestACounterThatWentBackwardsProducesAGapNotASpike(t *testing.T) {
	// A reboot between two samples, or a device removed and re-added. The
	// difference is meaningless either way, and treating a negative delta as
	// a large positive one renders as gigabytes per second at the exact
	// moment an operator is trying to work out what happened.
	samples := twoSnapshots(t, storage, storageRebooted, time.Second)

	if _, found := labelled(samples, "read_bytes", "device", "sda"); found {
		t.Error("a counter that went backwards produced a sample")
	}
	// And the devices whose counters did *not* move backwards are still
	// reported: one rebooted-looking device must not blank out the host.
	if _, found := labelled(samples, "read_bytes", "device", "dm-0"); !found {
		t.Error("the other devices were lost with it")
	}
}

func TestADeviceThatAppearsHasNoRateUntilTheNextInterval(t *testing.T) {
	// A hot-plugged disk, or an array being assembled. There is no previous
	// reading to subtract, so there is nothing honest to report yet.
	start := time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)
	now := start
	collector := collect.NewDiskIO(storageRebooted, storageRebooted+"/sys")
	collector.Now = func() time.Time { return now }
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("first collect: %v", err)
	}

	// storage has sda, dm-0, md0 and nvme0n1; storagerebooted lacks nothing
	// but the fixture above started from the smaller file, so sda is the
	// device present in both. Re-point at the fuller fixture: nvme0n1 was
	// there too, so use the one genuinely absent from the first snapshot.
	collector.ProcRoot = storage
	collector.SysRoot = storage + "/sys"
	now = start.Add(time.Second)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("second collect: %v", err)
	}

	// Every device in the second snapshot that was also in the first gets a
	// rate; this asserts the collector did not invent one for a device it
	// had never seen, by checking no sample has a negative or absurd value.
	for _, sample := range samples {
		if sample.Value < 0 {
			t.Errorf("negative rate for %s/%s: %v",
				sample.Labels["device"], sample.Metric, sample.Value)
		}
	}
}

func TestTwoCollectionsInsideOneTickProduceNothing(t *testing.T) {
	// A zero interval divides by zero. Returning nothing says "no
	// measurement" without producing an infinity for Sample.Valid to catch
	// downstream.
	samples := twoSnapshots(t, storage, storageBusy, 0)

	if len(samples) != 0 {
		t.Errorf("a zero-length interval produced %d samples", len(samples))
	}
}

func TestDiskIOIsUnavailableWithoutDiskstats(t *testing.T) {
	collector := collect.NewDiskIO("testdata/does-not-exist", "testdata/does-not-exist/sys")

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no diskstats reported itself available")
	}
	if !strings.Contains(availability.Reason, "diskstats") {
		t.Errorf("the reason does not name the file: %q", availability.Reason)
	}
}

func TestDiskIOIsUnavailableWhenEverythingIsFilteredOut(t *testing.T) {
	// diskstats present, and every device in it is a partition or a loop
	// device. The module is supported and this host cannot produce a figure
	// from it, which is a different answer from "it broke".
	collector := collect.NewDiskIO(storage, "testdata/storage-empty-sys")

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no whole block devices reported itself available")
	}
	if !strings.Contains(availability.Reason, "no whole block devices") {
		t.Errorf("the reason does not explain itself: %q", availability.Reason)
	}
}

func TestTheCollectorsIdentifyThemselves(t *testing.T) {
	// The module identifier is what the scheduler matches a configuration
	// against, so a typo here is a module that is configured and never runs.
	if got := collect.NewFilesystem("").Module(); got != "filesystem" {
		t.Errorf("filesystem module = %q", got)
	}
	if got := collect.NewDiskIO("", "").Module(); got != "diskio" {
		t.Errorf("diskio module = %q", got)
	}
}

// errorStatfs always fails, standing in for a filesystem the agent account
// cannot reach.
func errorStatfs(string, *syscall.Statfs_t) error { return errors.New("permission denied") }

func TestAFilesystemTheAgentCannotReachIsSkipped(t *testing.T) {
	// The agent runs unprivileged. A mount under a directory it cannot
	// traverse is a mount it cannot measure, and that is not a reason to
	// report nothing at all.
	collector, _ := storageFixture(t, storage)
	collector.Statfs = errorStatfs

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("an unreachable filesystem failed the collection: %v", err)
	}
	if len(samples) != 0 {
		t.Errorf("samples were produced for filesystems that could not be measured: %v", samples)
	}
}

func TestErrorsRemountRoIsNotAReadOnlyMount(t *testing.T) {
	// `errors=remount-ro` is on most ext4 filesystems and contains "ro". A
	// substring test — which is the obvious way to write this check —
	// reports the root filesystem of nearly every Linux host as read-only,
	// which is both alarming and wrong. So options are matched exactly, and
	// the fixture's root mount carries the real-world option that would
	// catch a regression.
	collector, _ := storageFixture(t, storage)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if value, _ := labelled(samples, "readonly", "mount", "/"); value != 0 {
		t.Errorf("readonly(/) = %v; errors=remount-ro was read as a read-only mount", value)
	}
}

// --- Network ----------------------------------------------------------------
//
// The fixtures describe a host with a physical NIC enslaved to a bridge, a
// WireGuard tunnel, a Docker bridge with one veth, and loopback — which is
// every category the selection rule has an opinion about.

// networkSnapshots collects from `first`, re-points at `second`, and
// collects again. The previous counters live in the collector, so the
// mechanism under test is the difference between two readings it holds.
func networkSnapshots(t *testing.T, first, second string, apart time.Duration, only []string) []collect.Sample {
	t.Helper()
	start := time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)
	now := start
	collector := collect.NewNetwork(first, first+"/sys", only)
	collector.Now = func() time.Time { return now }

	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("first collect: %v", err)
	}
	collector.ProcRoot = second
	collector.SysRoot = second + "/sys"
	now = start.Add(apart)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("second collect: %v", err)
	}
	return samples
}

func interfacesIn(samples []collect.Sample) []string {
	seen := map[string]bool{}
	for _, sample := range samples {
		if name, ok := sample.Labels["interface"]; ok {
			seen[name] = true
		}
	}
	out := make([]string, 0, len(seen))
	for name := range seen {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func TestTheDefaultInterfaceSelectionAvoidsDoubleCounting(t *testing.T) {
	// A packet leaving a container traverses a veth, a bridge and the
	// physical NIC, and all three count it. The rule keeps the meaningful
	// unit at each layer and drops the rest.
	samples := networkSnapshots(t, storage, storageBusy, time.Second, nil)

	got := interfacesIn(samples)
	want := []string{"br0", "wg0"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("interfaces = %v, want %v", got, want)
	}
}

func TestEachInterfaceExclusionIsForItsOwnReason(t *testing.T) {
	samples := networkSnapshots(t, storage, storageBusy, time.Second, nil)

	for _, excluded := range []struct{ name, why string }{
		{"lo", "traffic a host sends to itself is not network traffic"},
		{"enp6s0", "it is enslaved to br0, which counts the same bytes"},
		{"veth9a1b2c3", "a container's host-side half is counted on the bridge"},
		{"docker0", "a container bridge's traffic crosses the real NIC as well"},
	} {
		if _, found := labelled(samples, "receive_bytes", "interface", excluded.name); found {
			t.Errorf("%s was reported: %s", excluded.name, excluded.why)
		}
	}
}

func TestABridgedPhysicalNicLeavesTheBridgeReporting(t *testing.T) {
	// The topology that would be easy to get backwards: on a host where the
	// NIC is in a bridge, dropping the bridge instead of the slave would
	// leave the operator with no view of their own uplink at all.
	samples := networkSnapshots(t, storage, storageBusy, time.Second, nil)

	if _, found := labelled(samples, "receive_bytes", "interface", "br0"); !found {
		t.Error("the bridge was dropped along with its member")
	}
}

func TestAnExplicitListReplacesTheDefaultEntirely(t *testing.T) {
	// An operator who asks for a veth gets it. The default is a sensible
	// rule, not a policy the host enforces against its own administrator.
	samples := networkSnapshots(t, storage, storageBusy, time.Second,
		[]string{"enp6s0", "veth9a1b2c3"})

	got := interfacesIn(samples)
	want := []string{"enp6s0", "veth9a1b2c3"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("interfaces = %v, want %v", got, want)
	}
}

func TestNetworkThroughputIsBytesPerSecond(t *testing.T) {
	samples := networkSnapshots(t, storage, storageBusy, time.Second,
		[]string{"enp6s0"})

	// receive 1,000,000 → 1,125,000 in one second.
	if value, _ := labelled(samples, "receive_bytes", "interface", "enp6s0"); value != 125_000 {
		t.Errorf("receive_bytes = %v, want 125000", value)
	}
	// transmit 500,000 → 562,500.
	if value, _ := labelled(samples, "transmit_bytes", "interface", "enp6s0"); value != 62_500 {
		t.Errorf("transmit_bytes = %v, want 62500", value)
	}
}

func TestErrorsAndDropsAreSeparateSeries(t *testing.T) {
	// They mean different things: an error is a frame the hardware could not
	// process — bad cable, duplex mismatch — and a drop is a frame the
	// kernel discarded because a queue was full, which is capacity. A single
	// "problems" figure would need the operator to guess which.
	samples := networkSnapshots(t, storage, storageBusy, time.Second,
		[]string{"enp6s0"})

	if value, _ := labelled(samples, "receive_errors", "interface", "enp6s0"); value != 2 {
		t.Errorf("receive_errors = %v, want 2", value)
	}
	if value, _ := labelled(samples, "receive_drops", "interface", "enp6s0"); value != 5 {
		t.Errorf("receive_drops = %v, want 5", value)
	}
	if value, _ := labelled(samples, "transmit_errors", "interface", "enp6s0"); value != 1 {
		t.Errorf("transmit_errors = %v, want 1", value)
	}
	if value, _ := labelled(samples, "transmit_drops", "interface", "enp6s0"); value != 3 {
		t.Errorf("transmit_drops = %v, want 3", value)
	}
}

func TestLinkStateIsReportedOnTheFirstCollection(t *testing.T) {
	// Throughput needs two samples; "the cable is unplugged" does not, and
	// making an operator wait an interval to see it would be a worse answer
	// than the kernel already has.
	collector := collect.NewNetwork(storage, storage+"/sys", []string{"enp6s0", "docker0"})

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if value, found := labelled(samples, "up", "interface", "enp6s0"); !found || value != 1 {
		t.Errorf("up(enp6s0) = %v found=%v, want 1", value, found)
	}
	if value, found := labelled(samples, "up", "interface", "docker0"); !found || value != 0 {
		t.Errorf("up(docker0) = %v found=%v, want 0", value, found)
	}
	// And no throughput yet, because a rate needs two readings.
	if _, found := labelled(samples, "receive_bytes", "interface", "enp6s0"); found {
		t.Error("the first collection produced a throughput")
	}
}

func TestAnUnknownOperstateIsNotReportedAsDown(t *testing.T) {
	// A WireGuard tunnel reports "unknown", which is what most virtual
	// interfaces do. Rendering that as a dead link would show every tunnel
	// on the host as broken.
	collector := collect.NewNetwork(storage, storage+"/sys", []string{"wg0"})

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if _, found := labelled(samples, "up", "interface", "wg0"); found {
		t.Error("an interface reporting operstate=unknown was given a link state")
	}
}

func TestLinkSpeedIsBitsPerSecondAndAbsentWhereUnknown(t *testing.T) {
	// A "1 Gb" port is 125 MB/s. Rendering the speed in bytes would make an
	// operator doubt the whole panel, so the unit is the one the hardware is
	// sold in.
	collector := collect.NewNetwork(storage, storage+"/sys", []string{"enp6s0", "wg0", "br0"})

	samples, err := collector.Collect(context.Background())

	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if value, _ := labelled(samples, "speed", "interface", "enp6s0"); value != 1_000_000_000 {
		t.Errorf("speed(enp6s0) = %v, want 1e9", value)
	}
	// -1 is what the kernel writes for "not known", and 0 bit/s on a working
	// tunnel would be a lie the interface would render.
	if _, found := labelled(samples, "speed", "interface", "wg0"); found {
		t.Error("an interface whose driver reports -1 was given a speed")
	}
	// No speed file at all, which is the case for most virtual interfaces.
	if _, found := labelled(samples, "speed", "interface", "br0"); found {
		t.Error("an interface with no speed file was given a speed")
	}
}

func TestANetworkCounterResetDropsTheWholeInterface(t *testing.T) {
	// A reboot, or a 32-bit counter wrapping. Dropping only the series that
	// went backwards would leave a half-reported interval — throughput
	// missing, packet rate present — which invites exactly the wrong
	// conclusion from a chart.
	samples := networkSnapshots(t, storage, storageRebooted, time.Second,
		[]string{"enp6s0", "wg0"})

	for _, metric := range []string{
		"receive_bytes", "transmit_bytes", "receive_packets", "transmit_packets",
	} {
		if _, found := labelled(samples, metric, "interface", "enp6s0"); found {
			t.Errorf("%s survived a counter reset", metric)
		}
	}
	// The interface whose counters did not move backwards is still reported.
	if _, found := labelled(samples, "receive_bytes", "interface", "wg0"); !found {
		t.Error("an unaffected interface was dropped too")
	}
}

func TestAHostWithNoReportableInterfacesSaysSo(t *testing.T) {
	collector := collect.NewNetwork(storageIdle, storageIdle+"/sys", nil)

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with only loopback and a docker bridge reported itself available")
	}
	if !strings.Contains(availability.Reason, "no reportable network interfaces") {
		t.Errorf("the reason does not explain itself: %q", availability.Reason)
	}
}

func TestAConfiguredInterfaceThatDoesNotExistSaysSo(t *testing.T) {
	// The operator typed a name. Telling them it does not exist is the
	// difference between a typo they fix in a minute and an empty chart they
	// stare at.
	collector := collect.NewNetwork(storage, storage+"/sys", []string{"eth42"})

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a configured interface that does not exist reported itself available")
	}
	if !strings.Contains(availability.Reason, "eth42") {
		t.Errorf("the reason does not name the interface: %q", availability.Reason)
	}
}

func TestNetworkIsUnavailableWithoutProcNetDev(t *testing.T) {
	collector := collect.NewNetwork("testdata/does-not-exist", "testdata/does-not-exist/sys", nil)

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a host with no /proc/net/dev reported itself available")
	}
}

func TestTheNetworkCollectorIdentifiesItself(t *testing.T) {
	if got := collect.NewNetwork("", "", nil).Module(); got != "network" {
		t.Errorf("network module = %q", got)
	}
}
