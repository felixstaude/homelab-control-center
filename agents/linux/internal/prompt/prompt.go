// Package prompt reads a secret from the controlling terminal without echoing
// it.
//
// # Why this exists rather than reading stdin
//
// The enrollment credential has to get from a person's clipboard into one
// HTTPS request without passing through any channel that keeps a copy. Every
// obvious alternative keeps one:
//
//	--token XYZ            /proc/<pid>/cmdline, readable by every local user
//	HCC_…_TOKEN=XYZ cmd    /proc/<pid>/environ, and inherited by children
//	heredoc into a file    shell history, and a file somebody has to remember
//	                       to shred
//	echo XYZ | hcc-agent   shell history again
//
// A prompt on the controlling terminal with echo disabled keeps none. The
// bytes go from the terminal driver into this process's memory and into a TLS
// session.
//
// # /dev/tty, not os.Stdin
//
// Reading /dev/tty rather than stdin is deliberate and is not merely
// defensive. It keeps working when the command is part of a pipeline, and it
// makes the prompt impossible to satisfy from a redirected file — which is the
// behaviour you want from something about to mint a long-lived credential.
// `hcc-agent install < credential.txt` does **not** silently work; it reports
// that there is no terminal and names the non-interactive flag, so the
// operator chooses the automation path deliberately rather than discovering it
// by accident.
package prompt

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"unsafe"
)

// ErrNoTerminal reports that there is no controlling terminal to prompt on.
//
// Its own error because the right response is specific: use --token-file with
// --non-interactive. A generic "cannot open /dev/tty" would send an operator
// looking at device permissions.
var ErrNoTerminal = errors.New("no controlling terminal")

// MaxSecretBytes bounds one line of input.
//
// An enrollment credential is 43 characters. The allowance is for whitespace
// and a terminal that wraps; the bound is what stops a paste of a megabyte
// from being this process's memory profile.
const MaxSecretBytes = 4096

// Available reports whether a secret can be read interactively.
//
// Checked separately from [ReadSecret] so the installer can refuse at
// preflight — before creating a user and placing a binary — rather than
// discovering it at step six with half an installation on disk.
func Available() bool {
	file, err := os.OpenFile("/dev/tty", os.O_RDWR, 0)
	if err != nil {
		return false
	}
	defer file.Close()
	return isTerminal(int(file.Fd()))
}

// ReadSecret writes label to the terminal and reads one line from it with echo
// disabled.
//
// The returned string is trimmed of surrounding whitespace, because a value
// pasted into a terminal routinely arrives with a trailing newline or a
// leading space and neither is part of the credential.
func ReadSecret(label string) (string, error) {
	terminal, err := os.OpenFile("/dev/tty", os.O_RDWR, 0)
	if err != nil {
		return "", fmt.Errorf("%w: %w", ErrNoTerminal, err)
	}
	defer terminal.Close()

	descriptor := int(terminal.Fd())
	if !isTerminal(descriptor) {
		return "", ErrNoTerminal
	}

	restore, err := disableEcho(descriptor)
	if err != nil {
		return "", fmt.Errorf("cannot turn off terminal echo: %w", err)
	}

	// A terminal left with echo disabled is a terminal the operator cannot
	// type into, and Ctrl-C during a prompt is the single most likely way to
	// leave it that way. The handler restores the terminal and then exits:
	// re-raising into the default disposition would be tidier and risks a
	// race against the deferred restore below.
	interrupted := make(chan os.Signal, 1)
	signal.Notify(interrupted, syscall.SIGINT, syscall.SIGTERM)
	done := make(chan struct{})
	go func() {
		select {
		case <-interrupted:
			restore()
			fmt.Fprintln(terminal)
			os.Exit(130) // 128 + SIGINT, the shell convention
		case <-done:
		}
	}()
	defer func() {
		close(done)
		signal.Stop(interrupted)
		restore()
		// The newline the terminal did not echo when the operator pressed
		// return. Without it the next line of output starts beside the
		// prompt.
		fmt.Fprintln(terminal)
	}()

	if _, err := fmt.Fprint(terminal, label); err != nil {
		return "", fmt.Errorf("cannot write the prompt: %w", err)
	}

	// Canonical mode is left on, so the terminal driver handles backspace and
	// delivers a whole line — which is what makes correcting a mistyped paste
	// possible at all.
	reader := bufio.NewReaderSize(terminal, MaxSecretBytes)
	line, err := reader.ReadString('\n')
	if err != nil && line == "" {
		return "", fmt.Errorf("cannot read from the terminal: %w", err)
	}
	if len(line) > MaxSecretBytes {
		return "", fmt.Errorf("input is longer than %d bytes", MaxSecretBytes)
	}

	secret := strings.TrimSpace(line)
	if secret == "" {
		return "", errors.New("nothing was entered")
	}
	return secret, nil
}

// isTerminal reports whether the descriptor is a terminal, by asking for its
// attributes and seeing whether the kernel objects.
func isTerminal(descriptor int) bool {
	var attributes syscall.Termios
	return ioctl(descriptor, syscall.TCGETS, unsafe.Pointer(&attributes)) == nil
}

// disableEcho turns off character echo and returns a function restoring the
// previous state.
//
// Only ECHO is cleared. ICANON stays, so the driver still assembles lines and
// handles backspace, and ISIG stays, so Ctrl-C still raises SIGINT — a prompt
// that swallowed the interrupt would be a prompt an operator cannot escape.
func disableEcho(descriptor int) (func(), error) {
	var original syscall.Termios
	if err := ioctl(descriptor, syscall.TCGETS, unsafe.Pointer(&original)); err != nil {
		return nil, err
	}

	quiet := original
	quiet.Lflag &^= syscall.ECHO
	if err := ioctl(descriptor, syscall.TCSETS, unsafe.Pointer(&quiet)); err != nil {
		return nil, err
	}

	restored := false
	return func() {
		if restored {
			return
		}
		restored = true
		// Deliberately ignored. This runs on the way out, including from the
		// signal handler, and a failure to restore a terminal that may
		// already be gone must not mask whatever is actually happening.
		_ = ioctl(descriptor, syscall.TCSETS, unsafe.Pointer(&original))
	}, nil
}

func ioctl(descriptor int, request uint, argument unsafe.Pointer) error {
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL, uintptr(descriptor), uintptr(request), uintptr(argument),
	)
	if errno != 0 {
		return errno
	}
	return nil
}
