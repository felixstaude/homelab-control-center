package install

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// UninstallOptions is what the operator asked for.
type UninstallOptions struct {
	// Purge also removes the configuration directory — including the agent
	// credential — and the service account.
	Purge bool
	// DryRun prints what would be removed and removes nothing.
	DryRun bool
	// Out receives the output.
	Out io.Writer
}

// Uninstaller removes an installation.
type Uninstaller struct {
	System  System
	Paths   Paths
	Options UninstallOptions
	out     io.Writer
}

// NewUninstaller builds an Uninstaller against the real machine.
func NewUninstaller(options UninstallOptions) *Uninstaller {
	if options.Out == nil {
		options.Out = os.Stdout
	}
	return &Uninstaller{
		System:  LinuxSystem{},
		Paths:   Paths{},
		Options: options,
		out:     options.Out,
	}
}

func (u *Uninstaller) printf(format string, arguments ...any) {
	fmt.Fprintf(u.out, format, arguments...)
}

// Run removes the service, the unit and the binary.
//
// # What it keeps, and why
//
// By default the configuration directory stays, credential and all. A
// credential file is the only thing standing between a reinstall and a
// re-enrolment, and somebody removing an agent to move it to a new disk, or to
// rebuild a package, does not want to go back to the interface and issue a new
// one-time credential. `--purge` removes it, and says what that costs.
//
// # What it never does
//
// It does not touch the controller. No metric, no host record and no history
// is deleted by uninstalling an agent, because the operator removing a
// monitoring agent has not asked to lose the data it collected — and because
// an agent must not be able to delete the record of what it reported.
//
// It also does not revoke anything. The credential stays valid until an
// administrator revokes it in the interface, and the output says so plainly:
// removing the agent does not untrust it, and a decommissioned disk is a copy
// of that credential.
func (u *Uninstaller) Run(ctx context.Context) error {
	if u.Options.DryRun {
		u.printf("hcc-agent — uninstall plan\nThis is a dry run. Nothing will be removed.\n\n")
	} else {
		u.printf("hcc-agent — uninstalling\n\n")
	}

	if euid := u.System.Geteuid(); euid != 0 && !u.Options.DryRun {
		return fmt.Errorf("uninstalling needs root and this process is uid %d; re-run with sudo", euid)
	}

	type action struct {
		label  string
		detail string
		run    func(ctx context.Context) error
	}
	var actions []action
	var kept []string

	// --- The service ------------------------------------------------------
	if _, err := os.Stat(u.Paths.Unit()); err == nil {
		actions = append(actions, action{
			label:  "service",
			detail: "stop and disable " + ServiceName,
			run: func(ctx context.Context) error {
				// Failure is tolerated: a unit that is already stopped, or
				// masked, or whose systemd has been restarted, must not stop
				// the rest of the removal. The files are what matter.
				if _, err := u.System.Systemctl(ctx, "disable", "--now", ServiceName); err != nil {
					u.printf("  ! %v\n", err)
				}
				return nil
			},
		})
		actions = append(actions, action{
			label:  "unit",
			detail: "remove " + u.Paths.Unit(),
			run: func(ctx context.Context) error {
				if err := removeIfExists(u.Paths.Unit()); err != nil {
					return err
				}
				_, err := u.System.Systemctl(ctx, "daemon-reload")
				return err
			},
		})
	} else {
		kept = append(kept, "no unit at "+u.Paths.Unit())
	}

	// --- The update units --------------------------------------------------
	//
	// Removed on an ordinary uninstall, not only on --purge: they are units,
	// not data, and a path unit left watching for a request file that nothing
	// will ever write is a thing somebody finds in `systemctl list-units` and
	// has to work out.
	if _, err := os.Stat(u.Paths.UpdatePathUnit()); err == nil {
		actions = append(actions, action{
			label:  "update trigger",
			detail: "stop and disable " + UpdatePathUnitName,
			run: func(ctx context.Context) error {
				if _, err := u.System.Systemctl(
					ctx, "disable", "--now", UpdatePathUnitName,
				); err != nil {
					u.printf("  ! %v\n", err)
				}
				return nil
			},
		})
	}
	for _, path := range []string{u.Paths.UpdatePathUnit(), u.Paths.UpdateUnit()} {
		if _, err := os.Stat(path); err != nil {
			continue
		}
		target := path
		actions = append(actions, action{
			label:  "update unit",
			detail: "remove " + target,
			run: func(ctx context.Context) error {
				if err := removeIfExists(target); err != nil {
					return err
				}
				_, err := u.System.Systemctl(ctx, "daemon-reload")
				return err
			},
		})
	}

	// --- The binary -------------------------------------------------------
	for _, path := range []string{u.Paths.Binary(), u.Paths.PreviousBinary()} {
		if _, err := os.Stat(path); err != nil {
			continue
		}
		target := path
		actions = append(actions, action{
			label:  "binary",
			detail: "remove " + target,
			run:    func(context.Context) error { return removeIfExists(target) },
		})
	}

	// --- The state directory ----------------------------------------------
	//
	// Removed on an ordinary uninstall too. It holds a staged archive and the
	// last update's outcome: operational state with no value once the agent
	// is gone, and nothing an operator would want kept. The credential — the
	// one thing that *is* worth keeping — lives in the configuration
	// directory, which is only removed by --purge.
	if _, err := os.Stat(u.Paths.StateDir()); err == nil {
		directory := u.Paths.StateDir()
		actions = append(actions, action{
			label:  "state",
			detail: "remove " + directory,
			run: func(context.Context) error {
				if err := os.RemoveAll(directory); err != nil {
					return fmt.Errorf("cannot remove %s: %w", directory, err)
				}
				return nil
			},
		})
	}

	// The root-owned update state, which holds the anti-rollback floor.
	//
	// Removed only by --purge, and that asymmetry with the state directory
	// above is the point: an ordinary uninstall-and-reinstall of the same
	// host should not quietly reset its floor to whatever is being installed
	// next, because "uninstall, install an old version" would then be a
	// rollback path that needs no flag. --purge already means "forget this
	// host entirely" — it removes the credential too — so forgetting the
	// floor belongs with it.
	if u.Options.Purge {
		directory := u.Paths.UpdateStateDir()
		if _, err := os.Stat(directory); err == nil {
			actions = append(actions, action{
				label:  "update state",
				detail: "remove " + directory + " including this host's anti-rollback floor",
				run: func(context.Context) error {
					if err := os.RemoveAll(directory); err != nil {
						return fmt.Errorf("cannot remove %s: %w", directory, err)
					}
					return nil
				},
			})
		}
	}

	// --- Credentials and configuration -----------------------------------
	if u.Options.Purge {
		directory := u.Paths.ConfigDir()
		if _, err := os.Stat(directory); err == nil {
			actions = append(actions, action{
				label:  "configuration",
				detail: "remove " + directory + " including the agent credential",
				run: func(context.Context) error {
					// RemoveAll here, unlike the installer's rollback: this is
					// the one path where destroying the credential is what was
					// asked for.
					if err := os.RemoveAll(directory); err != nil {
						return fmt.Errorf("cannot remove %s: %w", directory, err)
					}
					return nil
				},
			})
		}
		if _, err := u.System.LookupUser(ServiceUser); err == nil {
			actions = append(actions, action{
				label:  "service account",
				detail: "remove the " + ServiceUser + " account and group",
				run: func(ctx context.Context) error {
					return u.System.DeleteServiceAccount(ctx, ServiceUser)
				},
			})
		}
	} else {
		if _, err := os.Stat(u.Paths.Credential()); err == nil {
			kept = append(kept, u.Paths.Credential()+" — the agent credential")
		}
		if _, err := os.Stat(u.Paths.ConfigDir()); err == nil {
			kept = append(kept, u.Paths.ConfigDir()+" — configuration and state")
		}
		if _, err := u.System.LookupUser(ServiceUser); err == nil {
			kept = append(kept, "the "+ServiceUser+" system account")
		}
	}

	if len(actions) == 0 {
		u.printf("Nothing to remove: no unit, no binary%s.\n",
			map[bool]string{true: "", false: " and no configuration"}[!u.Options.Purge])
		u.reportKept(kept)
		return nil
	}

	if u.Options.DryRun {
		u.printf("Would remove, in order\n")
		for index, item := range actions {
			u.printf("  %d. %s\n", index+1, item.detail)
		}
		u.reportKept(kept)
		u.printf("\nNothing above has been done.\n")
		return nil
	}

	u.printf("Removing\n")
	var failures []string
	for _, item := range actions {
		if err := item.run(ctx); err != nil {
			u.printf("  ✗ %-20s %v\n", item.label, err)
			failures = append(failures, item.detail)
			continue
		}
		u.printf("  ✓ %-20s %s\n", item.label, item.detail)
	}

	u.reportKept(kept)

	// The reminder is unconditional, and it is the most important line in the
	// output. Removing an agent does not revoke its credential: that file is
	// only a secret for as long as nobody has a copy of it, and a
	// decommissioned disk is a copy.
	u.printf("\nRevoke this host's agent credential in the control plane.\n")
	u.printf("Removing the agent does not untrust it, and the controller keeps every\n")
	u.printf("metric this host reported — uninstalling deletes no history.\n")

	if len(failures) > 0 {
		return fmt.Errorf(
			"uninstall finished with %d problem(s):\n  - %s",
			len(failures), strings.Join(failures, "\n  - "),
		)
	}
	return nil
}

func (u *Uninstaller) reportKept(kept []string) {
	if len(kept) == 0 {
		return
	}
	u.printf("\nKept\n")
	for _, item := range kept {
		u.printf("  · %s\n", item)
	}
	if !u.Options.Purge {
		u.printf("\nPass --purge to remove the credential, the configuration and the account.\n")
	}
}

// ErrNotInstalled reports that there is nothing to act on.
var ErrNotInstalled = errors.New("this host has no agent installation")
