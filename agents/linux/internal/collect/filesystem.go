package collect

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

// MaxMounts bounds how many filesystems one host may report.
//
// Not a limit on the machine — a container host with fifty volumes is real —
// but on cardinality, for the same reason [MaxCores] exists. Sixty-four is
// far above any host this is for and low enough that a pathological mount
// table cannot make the agent produce thousands of series. Above it, the
// mounts that sort first are reported and the rest are dropped with a
// counted reason, so `/` is never the one that falls off the end.
const MaxMounts = 64

// MaxMountPathChars is the longest mount point that can be a label.
//
// The controller refuses a label value over 64 characters
// (hcc.agents.protocol.MAX_LABEL_VALUE_LENGTH), so a longer path cannot be
// stored. It is **skipped**, not truncated: two different mounts truncated to
// the same 64 characters would become one series, silently averaging two
// filesystems together. A skipped mount is visible; a merged one is not.
const MaxMountPathChars = 64

// Filesystem reports capacity and inode usage per mounted filesystem.
//
// # This is filesystem capacity, and it is not disk capacity
//
// `statfs` answers "how much does this filesystem hold and how much is left",
// which is what an operator asking about disk space means. It is not the size
// of the block device: a filesystem is smaller than its partition by its own
// metadata and by the reserved-blocks percentage, and on LVM or btrfs it can
// span several devices and be larger than any one of them. The two numbers
// are related by the storage stack and are not interchangeable, and this
// module reports only the first. Throughput per device is [DiskIO].
//
// # What is deliberately not reported
//
// Every exclusion below is a decision, and each one is the difference between
// a useful list and a list nobody reads:
//
//   - **Pseudo-filesystems** — proc, sysfs, cgroup, devpts and the rest.
//     They have no capacity, and statfs on them returns zeros that render as
//     a 0-byte filesystem.
//   - **tmpfs and ramfs.** They are memory. Reporting /run as a disk is how
//     a host with 16 GB of RAM appears to have an 8 GB disk nobody can find,
//     and the memory module already reports memory.
//   - **overlay.** On a host running containers these are container root
//     filesystems — dozens of them, each named by a content hash, each
//     reporting the *host's* free space. They would swamp the list with
//     copies of one number.
//   - **squashfs.** Read-only by construction, therefore permanently 100%
//     full, therefore permanently alarming and never actionable.
//   - **Network filesystems** — nfs, cifs, fuse.sshfs and friends. This one
//     is not about noise: `statfs` on a mount whose server has gone away
//     blocks in uninterruptible sleep, and no context deadline can cancel a
//     syscall in D state. A collector that walked a dead NFS mount would
//     wedge a goroutine until the mount was forcibly unmounted. Excluding
//     them is the only protection that actually works.
//   - **Duplicates of a filesystem already reported.** A bind mount is the
//     same filesystem at a second path and a btrfs subvolume is the same
//     pool's free space under another name, so reporting both double-counts
//     the capacity. The shortest path wins, which keeps `/` rather than
//     `/mnt/root-bind`.
//
// # Two duplicate tests, because one does not catch both cases
//
// A bind mount shares its **device number**, so `stat` finds it. A btrfs
// subvolume does not: the kernel gives each subvolume its own anonymous
// device number, and a machine with `/` and `/home` as two subvolumes of one
// pool reports the pool's free space twice with no way to tell from statfs
// that it is the same pool. Found by running this against a real Fedora
// laptop and seeing `/` and `/home` report byte-identical figures.
//
// The second test is the **source device** from mountinfo, which is
// `/dev/dm-1` for both of those subvolumes and differs for two genuinely
// separate filesystems. It is applied only when the source is an absolute
// path, because a source that is not a device path — `pool/dataset`,
// `server:/export`, `none` — is a name whose uniqueness nothing guarantees,
// and deduplicating on `none` would collapse every such mount into one.
//
// # Known limitation: ZFS datasets
//
// ZFS sources are `pool/dataset`, not device paths, so neither test catches
// them and a host with five datasets reports the pool's free space five
// times. Stated rather than worked around: nothing in this system sums
// capacity across mounts, so the cost is a repeated figure in a list rather
// than a wrong total, and the alternative is a ZFS-specific ioctl in a
// collector that otherwise reads two pseudo-files.
type Filesystem struct {
	// ProcRoot is where /proc is mounted. A field so tests read a committed
	// mountinfo fixture rather than the build machine's mount table.
	ProcRoot string

	// Statfs is the syscall, injected so a test can describe a filesystem
	// that does not exist on the machine running it — a full one, one with
	// no inodes left, one that fails with EACCES. Nil means the real
	// syscall.
	Statfs func(path string, out *syscall.Statfs_t) error

	// DeviceOf returns the device number a path lives on, for the duplicate
	// check. Injected for the same reason. Nil means the real stat.
	DeviceOf func(path string) (uint64, error)
}

// excludedFilesystemTypes is matched exactly against mountinfo's fstype
// field. See the type comment for why each is here.
var excludedFilesystemTypes = map[string]bool{
	// No capacity to report.
	"autofs": true, "bpf": true, "binfmt_misc": true, "cgroup": true,
	"cgroup2": true, "configfs": true, "debugfs": true, "devpts": true,
	"devtmpfs": true, "efivarfs": true, "fuse.gvfsd-fuse": true,
	"fusectl": true, "hugetlbfs": true, "mqueue": true, "nsfs": true,
	"proc": true, "pstore": true, "ramfs": true, "rpc_pipefs": true,
	"securityfs": true, "selinuxfs": true, "sysfs": true, "tracefs": true,
	// Memory, not disk.
	"tmpfs": true,
	// Container root filesystems, and read-only images.
	"overlay": true, "squashfs": true,
	// statfs on an unreachable server blocks uninterruptibly.
	"nfs": true, "nfs4": true, "cifs": true, "smb3": true, "smbfs": true,
	"fuse.sshfs": true, "fuse.rclone": true, "9p": true, "ceph": true,
	"glusterfs": true, "afs": true, "lustre": true,
}

// NewFilesystem builds a filesystem collector.
func NewFilesystem(procRoot string) *Filesystem {
	if procRoot == "" {
		procRoot = DefaultProcRoot
	}
	return &Filesystem{ProcRoot: procRoot}
}

// Module implements [Collector].
func (f *Filesystem) Module() string { return "filesystem" }

// Available implements [Collector].
//
// Unavailable when the mount table cannot be read, and — separately —
// when it can be read and contains nothing worth reporting. The second is
// the case a container hits: a sandbox whose every mount is an excluded
// pseudo-filesystem supports this module and has nothing to say through it,
// and an operator enabling it needs to be told that rather than shown an
// empty chart.
func (f *Filesystem) Available() Availability {
	path := f.mountinfoPath()
	mounts, err := f.mounts()
	if err != nil {
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s cannot be read, so this host's filesystems cannot be listed", path,
			),
		}
	}
	if len(mounts) == 0 {
		return Availability{
			Available: false,
			Reason: "this host has no ordinary filesystems mounted — only pseudo-filesystems, " +
				"memory-backed mounts and network mounts, none of which report usable capacity",
		}
	}
	return Availability{Available: true}
}

// Collect implements [Collector].
//
// A mount that cannot be measured is skipped and the rest are still
// reported. The alternative — failing the whole collection — would let one
// filesystem in a transient state blank out every other filesystem on the
// host, which is the opposite of what somebody watching a disk fill up
// needs.
func (f *Filesystem) Collect(_ context.Context) ([]Sample, error) {
	mounts, err := f.mounts()
	if err != nil {
		return nil, err
	}

	statfs := f.Statfs
	if statfs == nil {
		statfs = syscall.Statfs
	}

	samples := make([]Sample, 0, len(mounts)*6)
	for _, mount := range mounts {
		var stat syscall.Statfs_t
		if err := statfs(mount.point, &stat); err != nil {
			// Unmounted between reading the table and measuring it, or a
			// permission the agent does not have. Either way this mount has
			// nothing to report and the others still do.
			continue
		}
		// Blocks is in units of Bsize, which is the filesystem's *optimal
		// transfer* block size and not a sector. Multiplying by 512 — a
		// mistake easy to make from the diskstats code next door — would
		// understate every filesystem by a factor of eight.
		blockSize := uint64(stat.Bsize) //nolint:gosec,unconvert // Bsize is int64 on amd64, int32 elsewhere
		if blockSize == 0 || stat.Blocks == 0 {
			continue
		}
		total := stat.Blocks * blockSize
		// Bavail, not Bfree. Bfree includes the blocks reserved for root —
		// 5% by default on ext4 — which an ordinary process cannot use. A
		// filesystem reporting 5% free that returns ENOSPC is the oldest
		// confusing thing about disk monitoring.
		available := stat.Bavail * blockSize
		// Used is total minus *Bfree*, so used + free + reserved = total and
		// the reserved blocks are accounted as neither used nor available.
		// Deriving used as `total - available` instead would silently count
		// root's reserve as used, which overstates usage on every fresh ext4
		// filesystem by 5%.
		used := (stat.Blocks - stat.Bfree) * blockSize

		labels := map[string]string{"mount": mount.point}
		samples = append(samples,
			Sample{Metric: "total", Labels: labels, Value: float64(total)},
			Sample{Metric: "used", Labels: labels, Value: float64(used)},
			Sample{Metric: "available", Labels: labels, Value: float64(available)},
			// Against `total - reserved`, which is what a process can
			// actually fill: a filesystem at "100%" by this measure is one
			// that returns ENOSPC, which is the moment an operator cares
			// about.
			Sample{
				Metric: "utilisation",
				Labels: labels,
				Value:  percent(float64(used), float64(used+available)),
			},
			Sample{Metric: "readonly", Labels: labels, Value: boolValue(mount.readOnly)},
		)
		if stat.Files > 0 {
			// Absent rather than zero on a filesystem with no inode table —
			// btrfs and XFS report Files as 0, and 0% inodes used on a
			// filesystem that has no inode limit is a true statement nobody
			// can act on and an empty chart forever.
			samples = append(samples, Sample{
				Metric: "inodes_utilisation",
				Labels: labels,
				Value:  percent(float64(stat.Files-stat.Ffree), float64(stat.Files)),
			})
		}
	}

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// boolValue renders a flag as the 0/1 a numeric series can hold.
func boolValue(value bool) float64 {
	if value {
		return 1
	}
	return 0
}

// mountEntry is one usable filesystem.
type mountEntry struct {
	point  string
	fstype string
	// source is mountinfo's device field: `/dev/dm-1`, `pool/dataset`,
	// `none`. Used for the second duplicate test; see the type comment.
	source   string
	readOnly bool
}

func (f *Filesystem) mountinfoPath() string {
	return filepath.Join(f.ProcRoot, "self", "mountinfo")
}

// mounts reads the mount table and returns the filesystems worth measuring.
//
// /proc/self/mountinfo rather than /proc/mounts or `df`. mountinfo is
// field-delimited with an explicit variable-length section, so it parses
// unambiguously; /proc/mounts is the legacy format and omits the mount id;
// `df` is a human-readable table whose columns move between implementations
// and whose output this project does not parse on principle.
func (f *Filesystem) mounts() ([]mountEntry, error) {
	path := f.mountinfoPath()
	file, err := os.Open(path) //nolint:gosec // a path under the configured proc root
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	defer file.Close()

	deviceOf := f.DeviceOf
	if deviceOf == nil {
		deviceOf = realDeviceOf
	}

	candidates := make([]mountEntry, 0, 16)
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		entry, ok := parseMountinfoLine(scanner.Text())
		if !ok {
			continue
		}
		if excludedFilesystemTypes[entry.fstype] {
			continue
		}
		// A fuse filesystem this list has not been taught about. Refused
		// rather than measured, because the dangerous ones — network mounts
		// through fuse — are indistinguishable from the harmless ones by
		// name, and a blocking statfs costs more than a missing row.
		if strings.HasPrefix(entry.fstype, "fuse.") || entry.fstype == "fuse" {
			continue
		}
		if len(entry.point) > MaxMountPathChars {
			continue
		}
		candidates = append(candidates, entry)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}

	// Shortest path first, then alphabetical. This is what makes the
	// duplicate rule deterministic and sensible: of `/` and a bind mount at
	// `/mnt/root`, the one kept is `/`.
	sort.SliceStable(candidates, func(left, right int) bool {
		if len(candidates[left].point) != len(candidates[right].point) {
			return len(candidates[left].point) < len(candidates[right].point)
		}
		return candidates[left].point < candidates[right].point
	})

	seenDevice := make(map[uint64]bool, len(candidates))
	seenSource := make(map[string]bool, len(candidates))
	kept := make([]mountEntry, 0, len(candidates))
	for _, entry := range candidates {
		if len(kept) >= MaxMounts {
			break
		}
		device, err := deviceOf(entry.point)
		if err != nil {
			// Cannot stat it — unmounted, or a directory the agent account
			// cannot traverse. Skipped rather than reported, because a mount
			// whose device number is unknown cannot be de-duplicated and
			// might be a second view of one already counted.
			continue
		}
		if seenDevice[device] {
			continue
		}
		// Only when the source names a device. See the type comment: a
		// source that is not an absolute path is a name whose uniqueness
		// nothing guarantees, and `none` appears on many unrelated mounts.
		bySource := strings.HasPrefix(entry.source, "/")
		if bySource && seenSource[entry.source] {
			continue
		}
		seenDevice[device] = true
		if bySource {
			seenSource[entry.source] = true
		}
		kept = append(kept, entry)
	}
	return kept, nil
}

// realDeviceOf is the production [Filesystem.DeviceOf].
func realDeviceOf(path string) (uint64, error) {
	var stat syscall.Stat_t
	if err := syscall.Stat(path, &stat); err != nil {
		return 0, err
	}
	return uint64(stat.Dev), nil //nolint:unconvert // Dev is uint64 on amd64 and uint32 on 32-bit
}

// parseMountinfoLine reads one line of /proc/self/mountinfo.
//
// The format, from proc(5):
//
//	36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue
//	 0  1   2     3     4            5       6 7    8         9                 10
//
// Fields 6 onwards are **optional and variable in number**, terminated by a
// single "-". That separator is the only reliable way to find the filesystem
// type: counting from the left breaks on a mount with two optional fields,
// and counting from the right breaks on a mount whose super-options contain
// no spaces in some kernels and do in others. Splitting on the separator is
// what the format is designed for.
func parseMountinfoLine(line string) (mountEntry, bool) {
	before, after, found := strings.Cut(line, " - ")
	if !found {
		return mountEntry{}, false
	}
	left := strings.Fields(before)
	right := strings.Fields(after)
	if len(left) < 6 || len(right) < 1 {
		return mountEntry{}, false
	}

	// Octal escapes, because a mount point may contain a space, a tab, a
	// newline or a backslash and the kernel escapes all four. Without this a
	// mount at "/mnt/my disk" parses as two fields and becomes "/mnt/my".
	point := unescapeMountPath(left[4])

	// **Both** option fields, and the second one is the one that matters.
	//
	// Field 5 is the per-mount flags, which is what `mount -o ro` sets and
	// what a read-only bind mount carries. The field after the source is the
	// **super** options, which is where MS_RDONLY on the superblock shows —
	// and that is what the kernel sets when it remounts a filesystem
	// read-only after an I/O error.
	//
	// So checking only field 5 catches the deliberate case and misses the
	// failure, which is the case this metric exists for. Found by a fixture
	// line written the way the kernel actually renders that state.
	source := ""
	superOptions := ""
	if len(right) > 1 {
		source = unescapeMountPath(right[1])
	}
	if len(right) > 2 {
		superOptions = right[2]
	}
	readOnly := hasOption(left[5], "ro") || hasOption(superOptions, "ro")

	return mountEntry{point: point, fstype: right[0], source: source, readOnly: readOnly}, true
}

// hasOption reports whether a comma-separated mount option list contains an
// exact option.
//
// Exact, not a substring: "ro" is a substring of "relatime", "errors=remount-ro"
// and "rootcontext=…", and a substring test would report most of the mount
// table as read-only.
func hasOption(list, wanted string) bool {
	for _, option := range strings.Split(list, ",") {
		if option == wanted {
			return true
		}
	}
	return false
}

// unescapeMountPath decodes the kernel's octal escaping of mount paths.
func unescapeMountPath(raw string) string {
	if !strings.Contains(raw, `\`) {
		return raw
	}
	var out strings.Builder
	out.Grow(len(raw))
	for index := 0; index < len(raw); index++ {
		if raw[index] != '\\' || index+3 >= len(raw) {
			out.WriteByte(raw[index])
			continue
		}
		value, err := strconv.ParseUint(raw[index+1:index+4], 8, 8)
		if err != nil {
			out.WriteByte(raw[index])
			continue
		}
		out.WriteByte(byte(value))
		index += 3
	}
	return out.String()
}
