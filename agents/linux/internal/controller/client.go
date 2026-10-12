// Package controller talks to the control plane.
//
// Two operations, and they are the whole agent↔controller contract:
//
//   - [Client.Enroll] spends a one-time enrollment credential for a durable
//     per-host one. Called once, by a human running `hcc-agent enroll`.
//   - [Client.Connect] opens the long-lived authenticated WebSocket the agent
//     lives on.
//
// # The agent dials out, always
//
// The controller never connects to a host (ADR-018). That is what makes this
// work for a dedicated server behind NAT, a homelab with no port forwarding,
// and a machine whose firewall permits nothing inbound — which is to say, for
// the deployments this product is actually for. It also means the agent is the
// side that has to be stubborn about retrying, because nothing will come and
// find it.
//
// # TLS is verified, and there is no option to turn that off
//
// There is deliberately no --insecure, no -k and no HCC_AGENT_TLS_SKIP_VERIFY.
// An earlier draft of this agent had the last of those, with a warning logged
// at every start, and it was removed rather than kept: the agent's credential
// and every heartbeat cross that connection, so an unverified TLS session is
// an invitation to anyone on the path to collect a credential that authorizes
// reporting as this host. A flag that exists "just for testing" is a flag that
// ends up in a unit file on a real server, and the warning nobody reads is not
// a mitigation.
//
// An operator running an internal CA installs their CA certificate on the
// host, which is a one-line operation Debian has had for decades, or points
// [Config.CAFile] at it for this agent alone. Both are supported; skipping
// verification is not.
//
// Plain HTTP is permitted for **loopback only**. A controller on
// 127.0.0.1 is reached over a socket no other machine can see, which is the
// development arrangement and genuinely safe; the same URL with any other host
// in it is refused rather than warned about.
package controller

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/coder/websocket"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

// API paths on the controller. Constants rather than inline strings so the two
// call sites and the tests cannot drift apart.
const (
	pathEnroll  = "/api/v1/agents/enroll"
	pathConnect = "/api/v1/agents/connect"
)

// Timeouts.
//
// enrollTimeout bounds a single human-initiated request. Generous, because an
// operator running enrollment over a slow link should not have it fail at five
// seconds, and the command is interactive so a hang is visible.
//
// handshakeTimeout bounds opening the socket. Deliberately much shorter: the
// agent retries on its own schedule, and a handshake that hangs for a minute
// delays every subsequent attempt.
const (
	enrollTimeout    = 30 * time.Second
	handshakeTimeout = 15 * time.Second
)

// Config configures a [Client].
type Config struct {
	// BaseURL is the controller's base URL, e.g. https://hcc.example.com.
	BaseURL string

	// CAFile optionally points at a PEM bundle to trust in addition to the
	// system roots. For an operator running an internal CA who would rather
	// not install it host-wide.
	//
	// Additive, not a replacement: the system roots are still trusted, so
	// pointing this at an internal CA does not stop the agent from validating
	// a public certificate if the controller is later moved behind one.
	CAFile string

	// AgentVersion and Platform are reported to the controller for display.
	// Advisory; the controller never makes a decision from them.
	AgentVersion string
	Platform     string
}

// Client is a controller client. Safe for concurrent use.
type Client struct {
	baseURL *url.URL
	http    *http.Client
	cfg     Config
}

// ErrUnauthorized reports a credential the controller refused.
//
// Returned for both enrollment and connection. Deliberately one error for
// "never existed", "expired", "already used" and "revoked", because that is
// all the controller says — see docs/SECURITY.md on why it does not say more.
var ErrUnauthorized = errors.New("the controller rejected this credential")

// ErrUnavailable reports that the controller could not be reached or returned
// a server error. Distinguished from [ErrUnauthorized] because it is the one
// the agent should keep retrying.
var ErrUnavailable = errors.New("the controller is unavailable")

// ErrInsecureURL reports a plaintext controller URL that is not loopback.
type ErrInsecureURL struct {
	URL string
}

func (e *ErrInsecureURL) Error() string {
	return fmt.Sprintf(
		"refusing to use %q: plain HTTP sends this host's credential in the clear. "+
			"Use https://, or 127.0.0.1 for local development",
		e.URL,
	)
}

// New builds a Client, validating the URL and the TLS policy up front.
//
// Up front rather than on first use, so a bad URL in a unit file fails at
// start with a readable message instead of on the first connection attempt
// hours later.
func New(cfg Config) (*Client, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	if trimmed == "" {
		return nil, errors.New("controller URL is required")
	}

	parsed, err := url.Parse(trimmed)
	if err != nil {
		return nil, fmt.Errorf("controller URL %q is not a valid URL: %w", cfg.BaseURL, err)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("controller URL %q has no host", cfg.BaseURL)
	}

	switch parsed.Scheme {
	case "https":
	case "http":
		if !isLoopback(parsed.Hostname()) {
			return nil, &ErrInsecureURL{URL: trimmed}
		}
	default:
		return nil, fmt.Errorf(
			"controller URL scheme must be https (or http for loopback), got %q", parsed.Scheme,
		)
	}

	transport, err := newTransport(cfg.CAFile)
	if err != nil {
		return nil, err
	}

	return &Client{
		baseURL: parsed,
		cfg:     cfg,
		http: &http.Client{
			Transport: transport,
			// No client-level timeout: it would also bound the WebSocket,
			// which is meant to stay open for hours. Each call sets its own
			// deadline through the context instead.
			Timeout: 0,
		},
	}, nil
}

// isLoopback reports whether a host is unambiguously this machine.
//
// Checked as an address first, so ::1 and 127.0.0.2 are both recognised, and
// only then by name — and by name only for "localhost" exactly. A hostname
// that merely resolves to a loopback address is not accepted: DNS is not under
// the agent's control, and "it resolved to 127.0.0.1 when I checked" is not a
// property that holds for the lifetime of the process.
func isLoopback(host string) bool {
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return host == "localhost"
}

// newTransport builds the HTTP transport, trusting caFile in addition to the
// system roots when one is given.
func newTransport(caFile string) (*http.Transport, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	// MinVersion is set explicitly rather than left to the default. The
	// default is already TLS 1.2 in current Go, and stating it means a future
	// toolchain that lowered the default could not silently lower this agent's
	// floor.
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}

	if caFile == "" {
		return transport, nil
	}

	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("cannot read CA file %q: %w", caFile, err)
	}
	// The system pool, then the extra certificate added to it. Starting from
	// an empty pool would mean configuring an internal CA silently stopped the
	// agent trusting a public one, which is a surprise an operator would hit
	// the day they moved the controller behind a real certificate.
	pool, err := x509.SystemCertPool()
	if err != nil {
		return nil, fmt.Errorf("cannot load system certificate pool: %w", err)
	}
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("CA file %q contains no usable PEM certificate", caFile)
	}
	transport.TLSClientConfig.RootCAs = pool
	return transport, nil
}

// EnrollResult is what a successful enrollment returns.
type EnrollResult struct {
	HostID   string `json:"host_id"`
	HostName string `json:"host_name"`
	// Credential is the long-lived agent credential, in plaintext. The only
	// copy: the controller keeps a digest and will not return it again.
	Credential               string `json:"credential"`
	ProtocolVersion          int    `json:"protocol_version"`
	HeartbeatIntervalSeconds int    `json:"heartbeat_interval_seconds"`
	SupersededPrevious       bool   `json:"superseded_previous"`
}

// String renders the result with the credential redacted.
//
// Defined because the obvious thing to do with a struct in a log line is
// print it, and the obvious thing would print the secret.
func (r EnrollResult) String() string {
	return fmt.Sprintf(
		"EnrollResult{HostID:%q HostName:%q Credential:<redacted> ProtocolVersion:%d}",
		r.HostID, r.HostName, r.ProtocolVersion,
	)
}

// LogValue keeps the redaction when the result is logged as a structured
// attribute rather than with %v.
func (r EnrollResult) LogValue() any { return r.String() }

// Enroll exchanges a one-time enrollment credential for a durable one.
//
// The enrollment credential travels in the Authorization header, never in the
// URL: a query parameter would be recorded by every proxy on the path and by
// the controller's own access log, and this is the secret a human has just
// pasted into a terminal.
//
// Called once per host, by a person. Not retried internally — a retry would
// spend a credential that may in fact have been consumed by the attempt that
// appeared to fail, and the operator is standing right there and can decide.
func (c *Client) Enroll(ctx context.Context, enrollmentCredential string) (*EnrollResult, error) {
	if strings.TrimSpace(enrollmentCredential) == "" {
		return nil, errors.New("an enrollment credential is required")
	}

	body, err := json.Marshal(map[string]string{
		"agent_version": c.cfg.AgentVersion,
		"platform":      c.cfg.Platform,
	})
	if err != nil {
		return nil, fmt.Errorf("cannot encode enrollment request: %w", err)
	}

	ctx, cancel := context.WithTimeout(ctx, enrollTimeout)
	defer cancel()

	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, c.baseURL.String()+pathEnroll, strings.NewReader(string(body)),
	)
	if err != nil {
		return nil, fmt.Errorf("cannot build enrollment request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+enrollmentCredential)
	request.Header.Set("User-Agent", c.userAgent())

	response, err := c.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	defer response.Body.Close()

	// Bounded read. The controller is trusted more than the agent is, but
	// "trusted" is not "permitted to make this process allocate without
	// limit" — and a misconfigured reverse proxy returning a large error page
	// is a likelier cause here than malice.
	payload, err := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if err != nil {
		return nil, fmt.Errorf("%w: cannot read the response: %w", ErrUnavailable, err)
	}

	switch {
	case response.StatusCode == http.StatusOK:
	case response.StatusCode == http.StatusUnauthorized:
		return nil, fmt.Errorf("%w: %s", ErrUnauthorized, describeError(payload))
	case response.StatusCode >= http.StatusInternalServerError:
		return nil, fmt.Errorf("%w: the controller returned %d", ErrUnavailable, response.StatusCode)
	default:
		return nil, fmt.Errorf(
			"the controller refused enrollment with %d: %s",
			response.StatusCode, describeError(payload),
		)
	}

	var result EnrollResult
	if err := json.Unmarshal(payload, &result); err != nil {
		return nil, fmt.Errorf("the controller's enrollment response was not valid JSON: %w", err)
	}
	if result.Credential == "" {
		return nil, errors.New("the controller's enrollment response contained no credential")
	}
	if result.ProtocolVersion != protocol.Version {
		// A hard failure rather than a warning. Enrolling against a controller
		// whose protocol this agent cannot speak produces a host that looks
		// enrolled in the interface and can never connect, which is a worse
		// outcome than refusing now — and the operator is present and can
		// install a matching agent.
		return nil, fmt.Errorf(
			"this agent speaks protocol version %d and the controller speaks %d; "+
				"install a matching agent build",
			protocol.Version, result.ProtocolVersion,
		)
	}
	return &result, nil
}

// Conn is an open agent connection.
type Conn struct {
	socket *websocket.Conn
}

// Connect opens the authenticated WebSocket.
//
// The credential goes in the Authorization header, which a non-browser client
// can set on a handshake. A query parameter — the usual workaround for browser
// WebSocket clients, which cannot set headers — is not used and must not be:
// it would put a long-lived credential in the access log of every proxy
// between the agent and the controller.
//
// Returns [ErrUnauthorized] when the controller refuses the handshake with
// 401, which is the signal the caller must not retry quickly.
func (c *Client) Connect(ctx context.Context, credential string) (*Conn, error) {
	endpoint := *c.baseURL
	switch endpoint.Scheme {
	case "https":
		endpoint.Scheme = "wss"
	default:
		endpoint.Scheme = "ws"
	}
	endpoint.Path = strings.TrimRight(endpoint.Path, "/") + pathConnect

	handshakeCtx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()

	socket, response, err := websocket.Dial(handshakeCtx, endpoint.String(), &websocket.DialOptions{
		HTTPClient: c.http,
		HTTPHeader: http.Header{
			"Authorization":                {"Bearer " + credential},
			protocol.HeaderProtocolVersion: {fmt.Sprint(protocol.Version)},
			"User-Agent":                   {c.userAgent()},
		},
	})
	if err != nil {
		// The handshake response body carries the controller's reason when it
		// refused, and the status is what tells the caller whether to retry.
		if response != nil {
			defer response.Body.Close()
			payload, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
			if response.StatusCode == http.StatusUnauthorized {
				return nil, fmt.Errorf("%w: %s", ErrUnauthorized, describeError(payload))
			}
			return nil, fmt.Errorf(
				"%w: the controller refused the connection with %d: %s",
				ErrUnavailable, response.StatusCode, describeError(payload),
			)
		}
		return nil, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}

	// The read limit is applied to the socket itself, so an oversized frame is
	// refused by the library before it is buffered. Setting it here rather
	// than checking length after reading is the difference between a bound and
	// a report that the bound was exceeded.
	socket.SetReadLimit(protocol.MaxFrameBytes)

	return &Conn{socket: socket}, nil
}

// Send writes one agent → controller frame.
//
// One method rather than one per frame type. The alternative — SendHeartbeat,
// SendConfigRequest, SendConfigAck, SendConfigNack — would be four
// near-identical bodies and a fifth the day a frame is added, and every one of
// them would be a place for the encoding to differ. The frame's type is still
// checked at the call site, because the protocol package's constructors are
// the only way to build one.
func (c *Conn) Send(ctx context.Context, frame any) error {
	payload, err := json.Marshal(frame)
	if err != nil {
		return fmt.Errorf("cannot encode an outgoing frame: %w", err)
	}
	if err := c.socket.Write(ctx, websocket.MessageText, payload); err != nil {
		return fmt.Errorf("cannot send a frame to the controller: %w", err)
	}
	return nil
}

// Read blocks for the next controller frame.
//
// Returns the decoded frame, or an error. A close frame is returned as a
// frame rather than as an error, because the reason it carries is the whole
// point — the caller has to branch on it to decide whether to retry.
func (c *Conn) Read(ctx context.Context) (protocol.ServerFrame, error) {
	kind, payload, err := c.socket.Read(ctx)
	if err != nil {
		return protocol.ServerFrame{}, err
	}
	if kind != websocket.MessageText {
		return protocol.ServerFrame{}, errors.New("the controller sent a binary frame; this protocol is text")
	}
	return protocol.ParseServerFrame(payload)
}

// Close ends the connection, reporting a normal closure.
//
// Errors are deliberately discarded: this runs on the shutdown path, where the
// socket is frequently already gone, and a failure to deliver a courtesy close
// must not mask whatever actually ended the connection.
func (c *Conn) Close() {
	_ = c.socket.Close(websocket.StatusNormalClosure, "")
}

// CloseError inspects an error from [Conn.Read] for a WebSocket close status.
//
// Exported so the agent's loop can tell a clean close from a dropped
// connection without importing the websocket package itself — which keeps the
// dependency to one package of the agent rather than spread through it.
func CloseError(err error) (websocket.StatusCode, bool) {
	status := websocket.CloseStatus(err)
	if status == -1 {
		return 0, false
	}
	return status, true
}

func (c *Client) userAgent() string {
	return fmt.Sprintf("hcc-agent/%s (%s)", c.cfg.AgentVersion, c.cfg.Platform)
}

// describeError pulls the message out of the control plane's error envelope.
//
// Falls back to a fixed string rather than echoing the raw body. An
// unparseable body is likely an HTML error page from a reverse proxy, and
// pasting a kilobyte of markup into the agent's log helps nobody.
func describeError(payload []byte) string {
	var envelope struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil || envelope.Error.Message == "" {
		return "the controller gave no readable reason"
	}
	if envelope.Error.Code != "" {
		return fmt.Sprintf("%s (%s)", envelope.Error.Message, envelope.Error.Code)
	}
	return envelope.Error.Message
}
