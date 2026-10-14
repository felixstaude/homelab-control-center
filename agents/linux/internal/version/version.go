// Package version parses and orders semantic versions.
//
// It mirrors apps/server/hcc/updates/version.py, and the duplication is
// deliberate for the same reason the protocol package duplicates the wire
// format: these are two independently deployed programs that have to agree,
// and there is no shared definition across the language boundary.
//
// What keeps them in step is contracts/version-ordering.json — generated from
// the Python implementation, committed, and replayed by this package's tests.
// A change on one side that the other does not match fails CI rather than
// drifting until a fleet is offered a downgrade as an upgrade.
//
// # Why this is not strings.Compare
//
// Because "0.10.0" < "0.9.0" is true for byte comparison, and an agent that
// ordered versions that way would refuse every upgrade after the ninth
// release. That failure arrives late, long after the tests that would have
// caught it were written.
//
// # Why not a third-party semver module
//
// The agent vendors exactly one dependency (coder/websocket) and that is a
// property worth keeping for a binary that installs itself as root: every
// dependency is code running with those privileges. Semver 2.0.0's ordering
// rules are a page long and fully covered by the contract tests, so the
// trade is a hundred lines here against a supply-chain surface there.
package version

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// MaxLength bounds what will be parsed.
//
// Longer than any legitimate version and short enough that a hostile value
// from release metadata cannot make parsing expensive.
const MaxLength = 64

// ErrInvalid reports a string that is not a semantic version.
var ErrInvalid = errors.New("not a semantic version")

// Version is a parsed semantic version.
//
// Build metadata is kept because a release that names its commit is worth
// displaying, and excluded from ordering because the specification says two
// versions differing only in build metadata have equal precedence.
type Version struct {
	Major int
	Minor int
	Patch int
	// Prerelease holds the dot-separated identifiers, empty for a release.
	Prerelease []string
	// Build is the metadata after '+', empty when absent.
	Build string
	// raw is the input, for round-tripping in output an operator reads.
	raw string
}

// Parse parses a semantic version.
//
// Surrounding whitespace is tolerated, because a version read from a file has
// a trailing newline. Nothing else is: a leading "v", a two-part version and
// "latest" are errors rather than guesses, so that the thing which produced
// the string is responsible for being exact.
func Parse(raw string) (Version, error) {
	if len(raw) > MaxLength {
		return Version{}, fmt.Errorf(
			"%w: %d characters, over the %d allowed", ErrInvalid, len(raw), MaxLength,
		)
	}
	text := strings.TrimSpace(raw)
	if text == "" {
		return Version{}, fmt.Errorf("%w: empty", ErrInvalid)
	}

	core := text
	build := ""
	if index := strings.IndexByte(core, '+'); index >= 0 {
		core, build = core[:index], core[index+1:]
		if !validDotSeparated(build, isAlphanumericIdentifier) {
			return Version{}, fmt.Errorf("%w: %q has invalid build metadata", ErrInvalid, text)
		}
	}

	var prerelease []string
	if index := strings.IndexByte(core, '-'); index >= 0 {
		pre := core[index+1:]
		core = core[:index]
		if !validDotSeparated(pre, isPrereleaseIdentifier) {
			return Version{}, fmt.Errorf("%w: %q has an invalid pre-release", ErrInvalid, text)
		}
		prerelease = strings.Split(pre, ".")
	}

	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return Version{}, fmt.Errorf(
			"%w: %q — expected MAJOR.MINOR.PATCH with optional -prerelease and +build, "+
				"for example 0.2.0 or 0.2.0-beta.1", ErrInvalid, text,
		)
	}
	numbers := make([]int, 3)
	for index, part := range parts {
		value, err := parseNumeric(part)
		if err != nil {
			return Version{}, fmt.Errorf("%w: %q — %s", ErrInvalid, text, err)
		}
		numbers[index] = value
	}

	return Version{
		Major:      numbers[0],
		Minor:      numbers[1],
		Patch:      numbers[2],
		Prerelease: prerelease,
		Build:      build,
		raw:        text,
	}, nil
}

// MustParse parses or panics. For constants in tests and in this package's own
// initialisation, never for anything that arrives from outside.
func MustParse(raw string) Version {
	parsed, err := Parse(raw)
	if err != nil {
		panic(err)
	}
	return parsed
}

// parseNumeric accepts a non-negative integer with no leading zeros.
//
// Leading zeros are refused rather than accepted-and-normalised: "01.0.0" is
// not a version, and silently treating it as 1.0.0 would mean two spellings of
// one release.
func parseNumeric(text string) (int, error) {
	if text == "" {
		return 0, errors.New("empty numeric component")
	}
	if len(text) > 1 && text[0] == '0' {
		return 0, fmt.Errorf("%q has a leading zero", text)
	}
	for index := 0; index < len(text); index++ {
		if text[index] < '0' || text[index] > '9' {
			return 0, fmt.Errorf("%q is not numeric", text)
		}
	}
	value, err := strconv.Atoi(text)
	if err != nil {
		return 0, fmt.Errorf("%q is out of range", text)
	}
	return value, nil
}

// validDotSeparated reports whether every dot-separated identifier passes
// *valid*, and that there is at least one and none are empty.
func validDotSeparated(text string, valid func(string) bool) bool {
	if text == "" {
		return false
	}
	for _, identifier := range strings.Split(text, ".") {
		if identifier == "" || !valid(identifier) {
			return false
		}
	}
	return true
}

func isAlphanumericIdentifier(text string) bool {
	for index := 0; index < len(text); index++ {
		c := text[index]
		switch {
		case c >= '0' && c <= '9':
		case c >= 'a' && c <= 'z':
		case c >= 'A' && c <= 'Z':
		case c == '-':
		default:
			return false
		}
	}
	return true
}

// isPrereleaseIdentifier allows the same character set as build metadata, and
// additionally refuses a leading zero on a purely numeric identifier.
func isPrereleaseIdentifier(text string) bool {
	if !isAlphanumericIdentifier(text) {
		return false
	}
	if isNumeric(text) && len(text) > 1 && text[0] == '0' {
		return false
	}
	return true
}

func isNumeric(text string) bool {
	if text == "" {
		return false
	}
	for index := 0; index < len(text); index++ {
		if text[index] < '0' || text[index] > '9' {
			return false
		}
	}
	return true
}

// String returns the version as it was written.
func (v Version) String() string {
	if v.raw != "" {
		return v.raw
	}
	// A zero Version, or one built by hand in a test.
	text := fmt.Sprintf("%d.%d.%d", v.Major, v.Minor, v.Patch)
	if len(v.Prerelease) > 0 {
		text += "-" + strings.Join(v.Prerelease, ".")
	}
	if v.Build != "" {
		text += "+" + v.Build
	}
	return text
}

// IsPrerelease reports whether this version carries a pre-release identifier.
func (v Version) IsPrerelease() bool { return len(v.Prerelease) > 0 }

// Compare returns -1, 0 or +1 as v sorts before, equal to, or after other.
//
// Build metadata is ignored, per the specification.
func (v Version) Compare(other Version) int {
	if c := compareInt(v.Major, other.Major); c != 0 {
		return c
	}
	if c := compareInt(v.Minor, other.Minor); c != 0 {
		return c
	}
	if c := compareInt(v.Patch, other.Patch); c != 0 {
		return c
	}

	// A version *with* a pre-release is older than the same version without
	// one. This is the rule that gets inverted, and inverting it means
	// offering a beta to a host on the stable channel.
	switch {
	case len(v.Prerelease) == 0 && len(other.Prerelease) == 0:
		return 0
	case len(v.Prerelease) == 0:
		return 1
	case len(other.Prerelease) == 0:
		return -1
	}

	for index := 0; index < len(v.Prerelease) && index < len(other.Prerelease); index++ {
		if c := comparePrereleaseIdentifier(v.Prerelease[index], other.Prerelease[index]); c != 0 {
			return c
		}
	}
	// All shared identifiers equal: the shorter run sorts lower, so
	// 1.0.0-alpha < 1.0.0-alpha.1.
	return compareInt(len(v.Prerelease), len(other.Prerelease))
}

// comparePrereleaseIdentifier applies the specification's three rules:
// numeric identifiers compare numerically, alphanumeric ones compare as ASCII,
// and a numeric identifier is always lower than an alphanumeric one.
func comparePrereleaseIdentifier(left, right string) int {
	leftNumeric, rightNumeric := isNumeric(left), isNumeric(right)
	switch {
	case leftNumeric && rightNumeric:
		// Parsed rather than compared as text: "11" is above "2".
		leftValue, _ := strconv.Atoi(left)
		rightValue, _ := strconv.Atoi(right)
		return compareInt(leftValue, rightValue)
	case leftNumeric:
		return -1
	case rightNumeric:
		return 1
	default:
		return strings.Compare(left, right)
	}
}

func compareInt(left, right int) int {
	switch {
	case left < right:
		return -1
	case left > right:
		return 1
	default:
		return 0
	}
}

// Equal reports equal precedence, which is not the same as an identical
// string: 0.1.0 and 0.1.0+abc are equal here.
func (v Version) Equal(other Version) bool { return v.Compare(other) == 0 }

// LessThan reports whether v sorts before other.
func (v Version) LessThan(other Version) bool { return v.Compare(other) < 0 }

// GreaterThan reports whether v sorts after other.
func (v Version) GreaterThan(other Version) bool { return v.Compare(other) > 0 }

// Step kinds, matching hcc.updates.version.StepKind. The strings cross the
// wire and appear in the interface, so they are shared values rather than two
// enums that could diverge.
const (
	StepMajor      = "major"
	StepMinor      = "minor"
	StepPatch      = "patch"
	StepPrerelease = "prerelease"
	StepNone       = "none"
)

// Step reports how large the step from installed to target is.
//
// Measured on the major/minor/patch triple, so 0.1.0 → 0.1.1-beta.1 is a patch
// step that also happens to target a pre-release. A policy checks both facts
// separately; collapsing them into one word would make "patch releases only"
// silently admit or silently refuse pre-releases.
func Step(installed, target Version) string {
	switch {
	case installed.Major != target.Major:
		return StepMajor
	case installed.Minor != target.Minor:
		return StepMinor
	case installed.Patch != target.Patch:
		return StepPatch
	case installed.Equal(target):
		return StepNone
	default:
		return StepPrerelease
	}
}
