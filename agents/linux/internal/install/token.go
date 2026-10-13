package install

import (
	"fmt"
	"os"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/config"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/prompt"
)

// TokenSource yields the one-time enrollment credential, once.
//
// An interface with three implementations rather than a string on [Options],
// for one reason that matters: the credential must not exist as a value
// anywhere until the moment it is used. A struct field would be a place a
// future log line, a `%+v`, or a crash report could print it.
//
// [TokenSource.Available] is separate from [TokenSource.Read] so the installer
// can refuse at preflight — before a user is created and a binary placed —
// rather than discovering at step six that there is no terminal to prompt on.
type TokenSource interface {
	// Available returns "" when a credential can be obtained, or a human
	// description of why not.
	Available() string
	// Describe says where the credential will come from. Printed by
	// --dry-run, so it must never include the credential itself.
	Describe() string
	// Read obtains the credential.
	Read() (string, error)
}

// PromptSource reads the credential from the controlling terminal.
//
// The default, because it is the only channel that keeps no copy: not in argv,
// not in the environment, not in shell history, not in a file somebody has to
// remember to shred.
type PromptSource struct{}

func (PromptSource) Available() string {
	if prompt.Available() {
		return ""
	}
	return "there is no terminal to prompt on, and no --token-file was given"
}

func (PromptSource) Describe() string {
	return "an interactive prompt on this terminal, with input hidden"
}

func (PromptSource) Read() (string, error) {
	return prompt.ReadSecret("Enrolment credential (paste; input is hidden): ")
}

// FileSource reads the credential from a file.
//
// The automation path. Ansible, cloud-init and the like write the file with
// mode 0600, run the installer, and delete it. The *path* is a flag because a
// path is not a secret; the credential never is.
type FileSource struct{ Path string }

func (f FileSource) Available() string {
	info, err := os.Lstat(f.Path)
	switch {
	case err != nil:
		return fmt.Sprintf("cannot read the credential file %q: %v", f.Path, err)
	case info.Mode()&os.ModeSymlink != 0:
		return fmt.Sprintf(
			"%q is a symbolic link; the installer cannot tell whose file it would read", f.Path,
		)
	case info.Mode().Perm()&0o077 != 0:
		return fmt.Sprintf(
			"%q has permissions %#o; it must not be group- or world-readable (chmod 600)",
			f.Path, info.Mode().Perm(),
		)
	}
	return ""
}

func (f FileSource) Describe() string { return "the file " + f.Path }

func (f FileSource) Read() (string, error) {
	return config.ReadEnrollmentCredentialFile(f.Path)
}

// EnvironmentSource reads the credential from the environment, through the
// same two variables `hcc-agent enroll` has always honoured.
//
// Documented as second-best and supported anyway: a process's environment is
// readable through /proc/<pid>/environ and is inherited by every child, so it
// is strictly worse than a file or a prompt. It exists because an operator
// pasting a fifteen-minute credential into a terminal should not have to
// create a file first, and because removing a working path would break
// whatever already depends on it.
type EnvironmentSource struct {
	// LookupEnv is injected so tests do not mutate the process environment.
	LookupEnv func(string) (string, bool)
}

func (e EnvironmentSource) lookup() func(string) (string, bool) {
	if e.LookupEnv != nil {
		return e.LookupEnv
	}
	return os.LookupEnv
}

func (e EnvironmentSource) Available() string {
	if _, err := config.EnrollmentCredential(e.lookup()); err != nil {
		return err.Error()
	}
	return ""
}

func (e EnvironmentSource) Describe() string {
	if path, ok := e.lookup()(config.EnvEnrollmentFile); ok && path != "" {
		return fmt.Sprintf("the file named by %s (%s)", config.EnvEnrollmentFile, path)
	}
	return fmt.Sprintf(
		"the %s environment variable — readable through /proc/<pid>/environ; "+
			"a file or a prompt is better",
		config.EnvEnrollmentToken,
	)
}

func (e EnvironmentSource) Read() (string, error) {
	return config.EnrollmentCredential(e.lookup())
}

// ResolveTokenSource picks where the credential will come from.
//
// Precedence, most explicit first: a path the operator named, then the
// environment variables the manual procedure has always used, then a prompt.
// --non-interactive removes the prompt from the list, so the absence of a
// terminal is an error rather than a hang — which is the behaviour a
// configuration-management run needs.
func ResolveTokenSource(
	tokenFile string, nonInteractive bool, lookupEnv func(string) (string, bool),
) TokenSource {
	if tokenFile != "" {
		return FileSource{Path: tokenFile}
	}
	environment := EnvironmentSource{LookupEnv: lookupEnv}
	if environment.Available() == "" {
		return environment
	}
	if nonInteractive {
		// Returned rather than nil so preflight reports *why*: an operator
		// who passed --non-interactive and nothing else needs to be told
		// which flag they are missing, not that the source is unset.
		return noninteractiveSource{}
	}
	return PromptSource{}
}

// noninteractiveSource is the "you asked for no prompt and gave no file" case.
type noninteractiveSource struct{}

func (noninteractiveSource) Available() string {
	return fmt.Sprintf(
		"--non-interactive was given with no credential source: pass --token-file, "+
			"or set %s", config.EnvEnrollmentFile,
	)
}

func (noninteractiveSource) Describe() string { return "nothing — no credential source" }

func (noninteractiveSource) Read() (string, error) {
	return "", fmt.Errorf("no credential source")
}
