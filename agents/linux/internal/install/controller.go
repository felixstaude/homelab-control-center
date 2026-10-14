package install

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
)

// client builds a controller client with this binary's own build metadata.
//
// One constructor for both operations below, so the probe and the enrollment
// cannot end up with different TLS policies — which is the sort of difference
// that makes a preflight check pass and the operation it was checking fail.
func client(controllerURL, caFile string) (*controller.Client, error) {
	info := buildinfo.Get()
	return controller.New(controller.Config{
		BaseURL:      controllerURL,
		CAFile:       caFile,
		AgentVersion: info.Version,
		Platform:     info.Platform,
	})
}

// DefaultProbe is the production [ProbeFunc].
//
// Returns a human description of what answered, which the installer prints —
// "reachable, certificate verified" is the line that tells an operator the
// part they most often get wrong is right.
func DefaultProbe(ctx context.Context, controllerURL, caFile string) (string, error) {
	connection, err := client(controllerURL, caFile)
	if err != nil {
		// A scheme or host problem, caught before any network traffic. The
		// client's own message names the variable and the fix.
		return "", err
	}
	result, err := connection.Probe(ctx)
	if err != nil {
		return "", explainProbeFailure(err, caFile)
	}
	if result.TLS {
		if caFile != "" {
			return "reachable, certificate verified against the system roots and " + caFile, nil
		}
		return "reachable, certificate verified", nil
	}
	// Only possible for loopback: the client refuses plain HTTP to anything
	// else. Said out loud so a developer installation does not look like a
	// production one in the transcript.
	return "reachable over plain HTTP — loopback only, which is the development case", nil
}

// explainProbeFailure adds the remedy to the three failures that account for
// nearly every one.
//
// The underlying error is always preserved. "x509: certificate signed by
// unknown authority" is the most common enrollment failure there is, and it is
// also the one string an operator can usefully search for, so it is kept and
// annotated rather than replaced with something friendlier.
func explainProbeFailure(err error, caFile string) error {
	message := err.Error()
	switch {
	case containsAny(message, "certificate signed by unknown authority", "x509: "):
		if caFile == "" {
			return fmt.Errorf(
				"%w\n\nThe controller's certificate was not issued by a CA this host "+
					"trusts.\nIf you run an internal CA, install it on this host or pass "+
					"--ca-file /path/to/ca.pem.\nThere is deliberately no option to skip "+
					"the check: the credential about to be minted crosses this connection",
				err,
			)
		}
		return fmt.Errorf(
			"%w\n\nThe certificate did not verify even with %s. Check that the file is "+
				"the issuing CA rather than the server's own certificate, and that the "+
				"controller's name matches the certificate",
			err, caFile,
		)
	case containsAny(message, "no such host", "server misbehaving"):
		return fmt.Errorf(
			"%w\n\nThe hostname did not resolve from this machine. Check the spelling, "+
				"and check that this host's resolver can see the controller's name",
			err,
		)
	case containsAny(message, "connection refused"):
		return fmt.Errorf(
			"%w\n\nSomething answered at that address and refused the port. Check the "+
				"port, and check that the controller's reverse proxy is running",
			err,
		)
	case containsAny(message, "i/o timeout", "context deadline exceeded"):
		return fmt.Errorf(
			"%w\n\nNothing answered before the timeout. A firewall between this host "+
				"and the controller is the usual cause; the agent only ever dials out, "+
				"so no inbound rule is needed on this machine",
			err,
		)
	default:
		return err
	}
}

func containsAny(subject string, needles ...string) bool {
	for _, needle := range needles {
		if strings.Contains(subject, needle) {
			return true
		}
	}
	return false
}

// DefaultEnroll is the production [EnrollFunc].
func DefaultEnroll(
	ctx context.Context, controllerURL, caFile, enrollmentCredential string,
) (*Enrollment, error) {
	connection, err := client(controllerURL, caFile)
	if err != nil {
		return nil, err
	}
	result, err := connection.Enroll(ctx, enrollmentCredential)
	if err != nil {
		if errors.Is(err, controller.ErrUnauthorized) {
			return nil, fmt.Errorf(
				"%w\n\nAn enrollment credential is single-use and expires in minutes. "+
					"Issue a new one from the host's page and run this again",
				err,
			)
		}
		return nil, err
	}
	return &Enrollment{
		HostID:             result.HostID,
		HostName:           result.HostName,
		Credential:         result.Credential,
		SupersededPrevious: result.SupersededPrevious,
	}, nil
}
