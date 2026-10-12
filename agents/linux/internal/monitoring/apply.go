package monitoring

import "fmt"

// Outcome is what happened to a configuration the controller offered.
//
// Exactly one of Applied and Problems is meaningful: a non-empty Problems
// means nothing was written and the previous configuration is still in force.
// Modelled as one struct rather than two return values because the caller has
// to send either an acknowledgement or a rejection and must not be able to
// forget one of the branches.
type Outcome struct {
	// Version and Checksum are echoed from the offer, so the caller can
	// acknowledge or reject *that* version without holding onto the snapshot.
	Version  int64
	Checksum string

	// Applied is the new configuration, set only when Problems is empty.
	Applied *Stored
	// Problems is every reason the configuration was refused.
	Problems []string
}

// Accepted reports whether the configuration is now in force.
func (o Outcome) Accepted() bool {
	return len(o.Problems) == 0 && o.Applied != nil
}

// Apply verifies, validates and stores a configuration the controller offered.
//
// The order is the design, and each step has to come before the next:
//
//  1. **Verify the checksum** against the bytes that arrived. A document whose
//     digest does not match was corrupted or tampered with, and parsing it
//     first would mean validating content nobody vouched for.
//  2. **Parse and validate.** Every problem, not the first.
//  3. **Write atomically**, only if there were none.
//
// A rejected configuration **never replaces the last known good one**. That is
// the rule that matters most here (AGENT_ARCHITECTURE.md §6.3): a monitoring
// agent that stops monitoring because somebody typed a bad interval has failed
// at its job. So the write is last, and a failure at any earlier step leaves
// the file untouched.
//
// A failure to *write* is returned as an error rather than as a rejection, and
// the distinction is worth keeping: a rejection says "this configuration is
// wrong", which the operator should see in the interface and act on, while a
// write failure says "this host's disk is full or read-only", which is a
// different problem with a different fix. Reporting the second as the first
// would send somebody to look at a schedule when they should look at a disk.
func Apply(path string, version int64, checksum string, raw []byte) (Outcome, error) {
	outcome := Outcome{Version: version, Checksum: checksum}

	if !VerifyChecksum(raw, checksum) {
		// The computed digest is not reported back. It would be of no use to
		// the controller — which knows what it sent — and a rejection is
		// stored and displayed, so putting a second digest in it only invites
		// somebody to compare the wrong pair.
		outcome.Problems = []string{
			"the configuration's checksum does not match its contents; " +
				"it was corrupted or altered in transit",
		}
		return outcome, nil
	}

	document, problems, err := Parse(raw)
	if err != nil {
		outcome.Problems = []string{err.Error()}
		return outcome, nil
	}
	if len(problems) > 0 {
		outcome.Problems = problems
		return outcome, nil
	}

	if err := Save(path, raw); err != nil {
		return outcome, fmt.Errorf("cannot store the new configuration: %w", err)
	}

	outcome.Applied = &Stored{Raw: raw, Checksum: checksum, Document: document}
	return outcome, nil
}
