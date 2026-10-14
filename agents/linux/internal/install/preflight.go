package install

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/credentials"
)

// Check is one preflight finding.
type Check struct {
	// Name is the short label an operator scans for.
	Name string
	// Detail is what was found. Always present, including for a pass: "systemd
	// is present" is less useful than "systemd is present at
	// /run/systemd/system".
	Detail string
	// Fatal marks a finding that stops the installation. A non-fatal finding
	// is an advisory — something worth saying that is not a reason to refuse.
	Fatal bool
	// Fix is the command or action that resolves a fatal finding. Empty when
	// there is nothing the operator can usefully do.
	Fix string
}

// Preflight is the result of checking the environment.
type Preflight struct {
	// Passed are the checks that found nothing wrong, in the order run.
	Passed []Check
	// Problems are the fatal findings. A non-empty slice means the
	// installation must not start.
	Problems []Check
	// Advisories are non-fatal findings worth printing.
	Advisories []Check
}

// OK reports whether the installation may proceed.
func (p Preflight) OK() bool { return len(p.Problems) == 0 }

// Error renders the problems as one error, or nil when there are none.
func (p Preflight) Error() error {
	if p.OK() {
		return nil
	}
	var builder strings.Builder
	if len(p.Problems) == 1 {
		builder.WriteString("cannot install: ")
		builder.WriteString(p.Problems[0].Detail)
		if p.Problems[0].Fix != "" {
			builder.WriteString("\n  fix: ")
			builder.WriteString(p.Problems[0].Fix)
		}
		return errors.New(builder.String())
	}
	builder.WriteString("cannot install:")
	for _, problem := range p.Problems {
		builder.WriteString("\n  - ")
		builder.WriteString(problem.Detail)
		if problem.Fix != "" {
			builder.WriteString("\n    fix: ")
			builder.WriteString(problem.Fix)
		}
	}
	return errors.New(builder.String())
}

// minimumFreeBytes is how much space the installer insists on having.
//
// The binary is about 7 MB and the configuration is a kilobyte, so 32 MB is
// generous. The check exists because a full /usr is a failure three steps
// later, after a user has been created and a credential spent, and
// "no space left on device" from a rename is a worse message than this one.
const minimumFreeBytes = 32 << 20

// RunPreflight checks that an installation can succeed, changing nothing.
//
// Everything that can be known before any side effect is checked here, in one
// pass, so the operator fixes their machine once rather than discovering the
// next problem after each failure. The ordering of the checks is the ordering
// of the output; the ordering of *failures* does not matter, because all of
// them are reported.
func RunPreflight(
	ctx context.Context, system System, paths Paths, options Options,
) Preflight {
	var result Preflight

	add := func(check Check) {
		switch {
		case check.Fatal:
			result.Problems = append(result.Problems, check)
		case check.Fix != "" || strings.HasPrefix(check.Detail, "note:"):
			result.Advisories = append(result.Advisories, check)
		default:
			result.Passed = append(result.Passed, check)
		}
	}

	// --- Operating system -------------------------------------------------
	if GOOS() != "linux" {
		add(Check{
			Name:   "operating system",
			Detail: fmt.Sprintf("this agent is a Linux program and this is %s", GOOS()),
			Fatal:  true,
		})
		// Nothing below is meaningful off Linux.
		return result
	}
	add(Check{Name: "operating system", Detail: "Linux"})

	// --- Privilege --------------------------------------------------------
	if euid := system.Geteuid(); euid != 0 {
		add(Check{
			Name:   "privileges",
			Detail: fmt.Sprintf("installation needs root and this process is uid %d", euid),
			Fatal:  true,
			Fix:    "re-run with sudo",
		})
	} else {
		add(Check{Name: "privileges", Detail: "running as root"})
	}

	// --- systemd ----------------------------------------------------------
	if !system.SystemdPresent() {
		add(Check{
			Name: "service manager",
			Detail: "systemd is not running on this machine (" +
				SystemdRuntimeDir + " does not exist)",
			Fatal: true,
			Fix: "this installer only manages systemd services; " +
				"see agents/linux/deploy/README.md to run the agent another way",
		})
	} else {
		add(Check{Name: "service manager", Detail: "systemd at " + SystemdRuntimeDir})
	}

	// --- Architecture -----------------------------------------------------
	// An advisory, not a refusal. If this binary is running then its
	// architecture is executable here by definition; what is worth saying is
	// that a native build exists and would be faster.
	machine := system.MachineArchitecture()
	if native, ok := nativeArchitecture(machine); ok && native != GOARCH() {
		add(Check{
			Name: "architecture",
			Detail: fmt.Sprintf(
				"note: this is the linux/%s build and this machine is %s; "+
					"the linux/%s build is the native one",
				GOARCH(), machine, native,
			),
			Fix: fmt.Sprintf("download hcc-agent_<version>_linux_%s.tar.gz instead", native),
		})
	} else {
		add(Check{
			Name:   "architecture",
			Detail: fmt.Sprintf("linux/%s build on %s", GOARCH(), orUnknown(machine)),
		})
	}

	// --- /proc ------------------------------------------------------------
	// The four files that are the entire data source for every module that
	// exists. Checked by reading them, because a /proc mounted with
	// subset=pid is present, listable, and missing exactly these.
	missing := unreadableProcFiles(options.ProcRoot)
	if len(missing) > 0 {
		add(Check{
			Name:   "/proc",
			Detail: fmt.Sprintf("cannot read %s", strings.Join(missing, ", ")),
			Fatal:  true,
			Fix: "the agent has nothing to measure without these; " +
				"check whether /proc is mounted with hidepid or subset=pid",
		})
	} else {
		add(Check{Name: "/proc", Detail: "all four kernel files readable"})
	}

	// --- Target paths -----------------------------------------------------
	for _, candidate := range []struct {
		name string
		path string
	}{
		{"binary directory", filepath.Dir(paths.Binary())},
		{"unit directory", filepath.Dir(paths.Unit())},
	} {
		switch problem := inspectTargetDirectory(candidate.path); {
		case problem != "":
			add(Check{
				Name:   candidate.name,
				Detail: problem,
				Fatal:  true,
				Fix:    fmt.Sprintf("create %s as a real directory", candidate.path),
			})
		default:
			add(Check{Name: candidate.name, Detail: candidate.path + " is writable"})
		}
	}

	// A symlink where the binary goes is the classic way to make an installer
	// write somewhere it did not intend. Refused rather than followed.
	if info, err := os.Lstat(paths.Binary()); err == nil && info.Mode()&os.ModeSymlink != 0 {
		add(Check{
			Name:   "binary path",
			Detail: paths.Binary() + " is a symbolic link",
			Fatal:  true,
			Fix:    "remove the link; the installer will not write through one",
		})
	}

	// --- Free space -------------------------------------------------------
	for _, path := range uniqueExistingAncestors(
		filepath.Dir(paths.Binary()), filepath.Dir(paths.ConfigDir()),
	) {
		free, err := freeBytes(path)
		switch {
		case err != nil:
			add(Check{
				Name:   "free space",
				Detail: fmt.Sprintf("note: cannot determine free space on %s: %v", path, err),
			})
		case free < minimumFreeBytes:
			add(Check{
				Name: "free space",
				Detail: fmt.Sprintf(
					"%s has %s free, and the installation needs at least %s",
					path, humanBytes(free), humanBytes(minimumFreeBytes),
				),
				Fatal: true,
				Fix:   "free some space and re-run",
			})
		default:
			add(Check{
				Name:   "free space",
				Detail: fmt.Sprintf("%s has %s free", path, humanBytes(free)),
			})
		}
	}

	// --- An existing installation ----------------------------------------
	//
	// Reported here and *refused* by the caller, which is the only place that
	// knows whether the installation is complete or merely half-finished. A
	// fatal check here would have made a surviving credential — the thing
	// that lets a failed run be retried without spending a second one-time
	// credential — into the reason the retry could not happen.
	if _, err := credentials.Load(paths.Credential()); err == nil {
		if options.Force {
			add(Check{
				Name: "existing installation",
				Detail: "note: --force given; the existing credential will be replaced " +
					"and the running agent disconnected",
			})
		} else {
			add(Check{
				Name:   "existing installation",
				Detail: "this host already holds an agent credential",
			})
		}
	} else if !errors.Is(err, credentials.ErrNotEnrolled) {
		// A credential file that exists and is unusable. Reported rather than
		// overwritten: it may be a mode the operator can fix, and silently
		// replacing it would also silently revoke whatever is using it.
		add(Check{
			Name:   "existing installation",
			Detail: fmt.Sprintf("the credential file is unusable: %v", err),
			Fatal:  !options.Force,
			Fix:    "fix the file, or pass --force to replace it",
		})
	}

	// --- A terminal to prompt on -----------------------------------------
	// Here rather than at the point of use, so a non-interactive invocation
	// without a token file fails before a user is created.
	if options.TokenSource == nil {
		add(Check{
			Name:   "credential source",
			Detail: "no credential source was configured",
			Fatal:  true,
		})
	} else if problem := options.TokenSource.Available(); problem != "" {
		add(Check{Name: "credential source", Detail: problem, Fatal: true,
			Fix: "pass --token-file with a file containing the enrollment credential, " +
				"together with --non-interactive"})
	} else {
		add(Check{Name: "credential source", Detail: options.TokenSource.Describe()})
	}

	return result
}

// procFiles are the kernel files every module that exists today reads.
var procFiles = []string{"stat", "meminfo", "loadavg", "uptime"}

func unreadableProcFiles(root string) []string {
	if root == "" {
		root = collect.DefaultProcRoot
	}
	var missing []string
	for _, name := range procFiles {
		path := filepath.Join(root, name)
		file, err := os.Open(path) //nolint:gosec // a fixed set of names under /proc
		if err != nil {
			missing = append(missing, path)
			continue
		}
		// Opened *and* read: a /proc file can open and return nothing under
		// some sandboxes, and an empty /proc/stat is as useless as a missing
		// one.
		buffer := make([]byte, 1)
		read, err := file.Read(buffer)
		file.Close()
		if err != nil || read == 0 {
			missing = append(missing, path)
		}
	}
	return missing
}

// inspectTargetDirectory returns a human description of why a directory cannot
// be installed into, or "" when it can.
func inspectTargetDirectory(path string) string {
	info, err := os.Lstat(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		// Created by the installer. /usr/local/bin is absent on a minimal
		// image and creating it is ordinary, not a problem.
		return ""
	case err != nil:
		return fmt.Sprintf("cannot inspect %s: %v", path, err)
	case info.Mode()&os.ModeSymlink != 0:
		// A symlinked /usr/local/bin is normal on systems with a merged /usr,
		// so this resolves it and checks the target rather than refusing.
		resolved, err := filepath.EvalSymlinks(path)
		if err != nil {
			return fmt.Sprintf("%s is a symbolic link that does not resolve: %v", path, err)
		}
		return inspectTargetDirectory(resolved)
	case !info.IsDir():
		return fmt.Sprintf("%s exists and is not a directory", path)
	}
	return ""
}

// uniqueExistingAncestors returns, for each path, the nearest existing
// directory at or above it, with duplicates removed.
//
// Free space is a property of a filesystem, and the directory the installer
// will create does not exist yet — so the check has to walk up to something
// that does.
func uniqueExistingAncestors(paths ...string) []string {
	seen := make(map[string]bool, len(paths))
	var result []string
	for _, path := range paths {
		current := path
		for {
			if _, err := os.Stat(current); err == nil {
				break
			}
			parent := filepath.Dir(current)
			if parent == current {
				break
			}
			current = parent
		}
		if !seen[current] {
			seen[current] = true
			result = append(result, current)
		}
	}
	return result
}

func freeBytes(path string) (uint64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	// Bavail, not Bfree: Bfree includes the blocks reserved for root, and
	// although the installer runs as root, counting reserve as headroom is
	// how an installation fills a filesystem to the point where nothing else
	// on the machine can write.
	return stat.Bavail * uint64(stat.Bsize), nil
}

func humanBytes(value uint64) string {
	const unit = 1024
	if value < unit {
		return fmt.Sprintf("%d B", value)
	}
	suffixes := []string{"KiB", "MiB", "GiB", "TiB"}
	size := float64(value)
	index := -1
	for size >= unit && index < len(suffixes)-1 {
		size /= unit
		index++
	}
	return fmt.Sprintf("%.1f %s", size, suffixes[index])
}

// nativeArchitecture maps `uname -m` onto a Go architecture.
//
// Only the three the project builds for. An unrecognised machine string is
// reported as unknown rather than guessed at: the advisory this feeds exists
// to tell somebody they have the wrong download, and a wrong guess would tell
// them that falsely.
func nativeArchitecture(machine string) (string, bool) {
	switch machine {
	case "x86_64", "amd64":
		return "amd64", true
	case "aarch64", "arm64":
		return "arm64", true
	case "armv6l", "armv7l", "armv8l", "arm":
		return "arm", true
	default:
		return "", false
	}
}

func orUnknown(value string) string {
	if strings.TrimSpace(value) == "" {
		return "an unrecognised machine type"
	}
	return value
}
