package controller_test

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/controller"
	"github.com/felixstaude/homelab-control-center/agents/linux/internal/protocol"
)

const enrollmentCredential = "6kqZPR0LmNEO9Wr6SxBOB4TLgWFr3PTuJnB5fjKkHhA"

// testClient builds a client pointed at a test server.
//
// httptest serves on 127.0.0.1, which the client's loopback exception permits
// over plain HTTP — so these tests exercise the real transport without
// needing a certificate. The TLS policy itself is asserted separately.
func testClient(t *testing.T, handler http.Handler) (*controller.Client, *httptest.Server) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)

	client, err := controller.New(controller.Config{
		BaseURL:      server.URL,
		AgentVersion: "0.1.0-test",
		Platform:     "linux/amd64",
	})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return client, server
}

// ---------------------------------------------------------------------------
// The TLS policy
// ---------------------------------------------------------------------------

func TestPlaintextIsRefusedForANonLoopbackHost(t *testing.T) {
	t.Parallel()

	// Plain HTTP would send this host's credential in the clear, and the
	// refusal happens at construction so a bad unit file fails at start
	// rather than on the first connection hours later.
	for _, raw := range []string{
		"http://hcc.example.com",
		"http://192.168.1.10:8000",
		"http://10.0.0.1",
		// A hostname that merely *resolves* to loopback is not accepted: DNS
		// is not under the agent's control, and "it resolved to 127.0.0.1
		// when I checked" is not a property that holds for the process's life.
		"http://localhost.attacker.example",
	} {
		_, err := controller.New(controller.Config{BaseURL: raw})
		if err == nil {
			t.Errorf("%s was accepted", raw)
			continue
		}
		var insecure *controller.ErrInsecureURL
		if !errors.As(err, &insecure) {
			t.Errorf("%s: expected ErrInsecureURL, got %T: %v", raw, err, err)
		}
	}
}

func TestPlaintextIsPermittedForLoopback(t *testing.T) {
	t.Parallel()

	// A controller on 127.0.0.1 is reached over a socket no other machine can
	// see, which is the development arrangement and genuinely safe.
	for _, raw := range []string{
		"http://127.0.0.1:8000",
		"http://127.0.0.2:8000",
		"http://localhost:8000",
		"http://[::1]:8000",
	} {
		if _, err := controller.New(controller.Config{BaseURL: raw}); err != nil {
			t.Errorf("%s should be accepted: %v", raw, err)
		}
	}
}

func TestHTTPSIsAlwaysAccepted(t *testing.T) {
	t.Parallel()

	if _, err := controller.New(controller.Config{BaseURL: "https://hcc.example.com"}); err != nil {
		t.Fatalf("https should be accepted: %v", err)
	}
}

func TestAnUnknownSchemeIsRefused(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		"ws://hcc.example.com",
		"ftp://hcc.example.com",
		"file:///etc/passwd",
		"hcc.example.com",
	} {
		if _, err := controller.New(controller.Config{BaseURL: raw}); err == nil {
			t.Errorf("%s was accepted", raw)
		}
	}
}

func TestAnEmptyURLIsRefused(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{"", "   "} {
		if _, err := controller.New(controller.Config{BaseURL: raw}); err == nil {
			t.Errorf("%q was accepted", raw)
		}
	}
}

func TestATrailingSlashIsTolerated(t *testing.T) {
	t.Parallel()

	// An operator who pastes the URL from a browser gets a trailing slash, and
	// a double slash in the request path would be a 404 with no explanation.
	var path string
	client, _ := testClientWithBase(t, "/", http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			path = r.URL.Path
			writeEnrollSuccess(w)
		},
	))

	if _, err := client.Enroll(context.Background(), enrollmentCredential); err != nil {
		t.Fatalf("enroll: %v", err)
	}
	if path != "/api/v1/agents/enroll" {
		t.Errorf("path = %q, want it without a doubled slash", path)
	}
}

func TestAMissingCAFileIsReportedAtConstruction(t *testing.T) {
	t.Parallel()

	_, err := controller.New(controller.Config{
		BaseURL: "https://hcc.example.com",
		CAFile:  "/nonexistent/ca.pem",
	})
	if err == nil {
		t.Fatal("expected a missing CA file to be reported")
	}
}

func TestACAFileWithNoCertificateIsReported(t *testing.T) {
	t.Parallel()

	path := writeFile(t, "not a certificate")

	_, err := controller.New(controller.Config{
		BaseURL: "https://hcc.example.com",
		CAFile:  path,
	})
	if err == nil {
		t.Fatal("expected a CA file with no usable PEM to be reported")
	}
	if !strings.Contains(err.Error(), "PEM") {
		t.Errorf("the error should name the problem, got: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Enrollment
// ---------------------------------------------------------------------------

func TestEnrollSendsTheCredentialInTheAuthorizationHeader(t *testing.T) {
	t.Parallel()

	// And nowhere else. A query parameter would be recorded by every proxy on
	// the path and by the controller's own access log, and this is the secret
	// a human has just pasted into a terminal.
	var (
		authorization string
		rawQuery      string
		rawPath       string
		body          string
	)
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization = r.Header.Get("Authorization")
		rawQuery = r.URL.RawQuery
		rawPath = r.URL.Path
		buffer := make([]byte, 1024)
		read, _ := r.Body.Read(buffer)
		body = string(buffer[:read])
		writeEnrollSuccess(w)
	}))

	if _, err := client.Enroll(context.Background(), enrollmentCredential); err != nil {
		t.Fatalf("enroll: %v", err)
	}

	if authorization != "Bearer "+enrollmentCredential {
		t.Errorf("authorization = %q, want a bearer credential", authorization)
	}
	if strings.Contains(rawQuery, enrollmentCredential) || strings.Contains(rawPath, enrollmentCredential) {
		t.Errorf("the credential reached the URL: path=%q query=%q", rawPath, rawQuery)
	}
	if strings.Contains(body, enrollmentCredential) {
		t.Errorf("the credential reached the body: %q", body)
	}
}

func TestEnrollReportsTheAgentsSelfDescription(t *testing.T) {
	t.Parallel()

	var body string
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buffer := make([]byte, 1024)
		read, _ := r.Body.Read(buffer)
		body = string(buffer[:read])
		writeEnrollSuccess(w)
	}))

	if _, err := client.Enroll(context.Background(), enrollmentCredential); err != nil {
		t.Fatalf("enroll: %v", err)
	}

	for _, want := range []string{"0.1.0-test", "linux/amd64"} {
		if !strings.Contains(body, want) {
			t.Errorf("the request should report %q, got: %s", want, body)
		}
	}
	// And nothing that names a host: the host comes from the credential.
	for _, forbidden := range []string{"host_id", "hostname", "host_name"} {
		if strings.Contains(body, forbidden) {
			t.Errorf("the request carries %q, which an agent could use to claim a host", forbidden)
		}
	}
}

func TestEnrollReturnsTheDurableCredential(t *testing.T) {
	t.Parallel()

	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeEnrollSuccess(w)
	}))

	result, err := client.Enroll(context.Background(), enrollmentCredential)
	if err != nil {
		t.Fatalf("enroll: %v", err)
	}
	if result.Credential == "" {
		t.Fatal("the result carries no credential")
	}
	if result.HostName != "homeserver" {
		t.Errorf("host name = %q, want homeserver", result.HostName)
	}
}

func TestTheEnrollResultNeverPrintsTheCredential(t *testing.T) {
	t.Parallel()

	// The obvious thing to do with a struct in a log line is print it, and the
	// obvious thing would print the secret.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeEnrollSuccess(w)
	}))

	result, err := client.Enroll(context.Background(), enrollmentCredential)
	if err != nil {
		t.Fatalf("enroll: %v", err)
	}

	for _, rendered := range []string{
		result.String(),
		fmt.Sprintf("%v", result),
		fmt.Sprintf("%s", result),
	} {
		if strings.Contains(rendered, result.Credential) {
			t.Errorf("the credential leaked through a formatter: %s", rendered)
		}
		if !strings.Contains(rendered, "<redacted>") {
			t.Errorf("the redaction marker is missing: %s", rendered)
		}
	}
}

func TestEnrollMapsARefusalToErrUnauthorized(t *testing.T) {
	t.Parallel()

	// 401 is the one the caller must not retry: the credential is single-use
	// and short-lived, so retrying spends nothing and achieves nothing.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"error":{"code":"agent_credential_rejected","message":"not valid"}}`)
	}))

	_, err := client.Enroll(context.Background(), enrollmentCredential)

	if !errors.Is(err, controller.ErrUnauthorized) {
		t.Fatalf("expected ErrUnauthorized, got: %v", err)
	}
	if !strings.Contains(err.Error(), "not valid") {
		t.Errorf("the controller's reason should reach the operator, got: %v", err)
	}
}

func TestEnrollMapsAServerErrorToErrUnavailable(t *testing.T) {
	t.Parallel()

	// Distinguished from a refusal because this one is worth retrying, and
	// because telling an operator their credential was rejected when the
	// controller merely fell over would send them to the wrong screen.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))

	_, err := client.Enroll(context.Background(), enrollmentCredential)

	if !errors.Is(err, controller.ErrUnavailable) {
		t.Fatalf("expected ErrUnavailable, got: %v", err)
	}
	if errors.Is(err, controller.ErrUnauthorized) {
		t.Error("a server error must not be reported as a rejected credential")
	}
}

func TestEnrollMapsAnUnreachableControllerToErrUnavailable(t *testing.T) {
	t.Parallel()

	client, err := controller.New(controller.Config{BaseURL: "http://127.0.0.1:1"})
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	_, err = client.Enroll(context.Background(), enrollmentCredential)

	if !errors.Is(err, controller.ErrUnavailable) {
		t.Fatalf("expected ErrUnavailable, got: %v", err)
	}
}

func TestEnrollRefusesAProtocolMismatch(t *testing.T) {
	t.Parallel()

	// A hard failure rather than a warning: enrolling against a controller
	// whose protocol this agent cannot speak produces a host that looks
	// enrolled in the interface and can never connect, which is worse than
	// refusing while the operator is standing there.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"host_id":"h","host_name":"homeserver","credential":"%s","protocol_version":%d}`,
			strings.Repeat("c", 43), protocol.Version+1)
	}))

	_, err := client.Enroll(context.Background(), enrollmentCredential)

	if err == nil {
		t.Fatal("expected a protocol mismatch to be refused")
	}
	if !strings.Contains(err.Error(), "matching agent build") {
		t.Errorf("the error should name the remedy, got: %v", err)
	}
}

func TestEnrollRefusesAResponseWithNoCredential(t *testing.T) {
	t.Parallel()

	// Otherwise the agent would write an empty credential file and report
	// success, and the failure would surface as an authentication error with
	// no indication of its cause.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"host_id":"h","host_name":"homeserver","protocol_version":%d}`, protocol.Version)
	}))

	_, err := client.Enroll(context.Background(), enrollmentCredential)

	if err == nil {
		t.Fatal("expected a credential-less response to be refused")
	}
}

func TestEnrollRefusesAnEmptyCredentialWithoutContactingTheController(t *testing.T) {
	t.Parallel()

	var called bool
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		writeEnrollSuccess(w)
	}))

	if _, err := client.Enroll(context.Background(), "   "); err == nil {
		t.Fatal("expected an empty credential to be refused")
	}
	if called {
		t.Error("the controller should not have been contacted")
	}
}

func TestEnrollDoesNotEchoAnUnparseableErrorBody(t *testing.T) {
	t.Parallel()

	// An unparseable body is likely an HTML error page from a reverse proxy,
	// and pasting a kilobyte of markup into the agent's log helps nobody.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, "<html><body><h1>502 Bad Gateway</h1>"+strings.Repeat("x", 500)+"</body></html>")
	}))

	_, err := client.Enroll(context.Background(), enrollmentCredential)

	if err == nil {
		t.Fatal("expected a refusal")
	}
	if strings.Contains(err.Error(), "<html>") {
		t.Errorf("the raw body was echoed into the error: %v", err)
	}
	if !strings.Contains(err.Error(), "no readable reason") {
		t.Errorf("the error should say what it concluded, got: %v", err)
	}
}

func TestEnrollRespectsContextCancellation(t *testing.T) {
	t.Parallel()

	// So an enrollment against an unreachable controller can be interrupted
	// rather than waited out for thirty seconds.
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeEnrollSuccess(w)
	}))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if _, err := client.Enroll(ctx, enrollmentCredential); err == nil {
		t.Fatal("expected a cancelled context to abort the request")
	}
}

// ---------------------------------------------------------------------------
// Connecting
// ---------------------------------------------------------------------------

func TestConnectSendsTheCredentialAndProtocolVersionAsHeaders(t *testing.T) {
	t.Parallel()

	// The handshake is refused deliberately — opening a real socket is the
	// integration suite's job. What matters here is what crossed the wire on
	// the way, and specifically that the credential was not in the URL.
	var (
		authorization string
		version       string
		rawQuery      string
		rawPath       string
	)
	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization = r.Header.Get("Authorization")
		version = r.Header.Get(protocol.HeaderProtocolVersion)
		rawQuery = r.URL.RawQuery
		rawPath = r.URL.Path
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"error":{"code":"unauthorized","message":"nope"}}`)
	}))

	credential := strings.Repeat("c", 43)
	_, err := client.Connect(context.Background(), credential)

	if !errors.Is(err, controller.ErrUnauthorized) {
		t.Fatalf("expected ErrUnauthorized, got: %v", err)
	}
	if authorization != "Bearer "+credential {
		t.Errorf("authorization = %q, want a bearer credential", authorization)
	}
	if version != fmt.Sprint(protocol.Version) {
		t.Errorf("protocol version header = %q, want %d", version, protocol.Version)
	}
	if strings.Contains(rawQuery, credential) || strings.Contains(rawPath, credential) {
		t.Errorf("the credential reached the URL: path=%q query=%q", rawPath, rawQuery)
	}
	if rawPath != "/api/v1/agents/connect" {
		t.Errorf("path = %q, want the connect endpoint", rawPath)
	}
}

func TestConnectMapsAServerErrorToErrUnavailable(t *testing.T) {
	t.Parallel()

	client, _ := testClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, `{"error":{"code":"capacity","message":"at capacity"}}`)
	}))

	_, err := client.Connect(context.Background(), strings.Repeat("c", 43))

	if !errors.Is(err, controller.ErrUnavailable) {
		t.Fatalf("expected ErrUnavailable, got: %v", err)
	}
	if !strings.Contains(err.Error(), "at capacity") {
		t.Errorf("the controller's reason should reach the operator, got: %v", err)
	}
}

func TestConnectMapsAnUnreachableControllerToErrUnavailable(t *testing.T) {
	t.Parallel()

	client, err := controller.New(controller.Config{BaseURL: "http://127.0.0.1:1"})
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	_, err = client.Connect(context.Background(), strings.Repeat("c", 43))

	if !errors.Is(err, controller.ErrUnavailable) {
		t.Fatalf("expected ErrUnavailable, got: %v", err)
	}
	if errors.Is(err, controller.ErrUnauthorized) {
		t.Error("an unreachable controller must not be reported as a rejected credential")
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func writeEnrollSuccess(w http.ResponseWriter) {
	fmt.Fprintf(w,
		`{"host_id":"3f2c","host_name":"homeserver","credential":"%s",`+
			`"protocol_version":%d,"heartbeat_interval_seconds":30,"superseded_previous":false}`,
		strings.Repeat("a", 43), protocol.Version,
	)
}

// testClientWithBase builds a client whose base URL carries a suffix, for the
// trailing-slash case.
func testClientWithBase(
	t *testing.T, suffix string, handler http.Handler,
) (*controller.Client, *httptest.Server) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)

	client, err := controller.New(controller.Config{BaseURL: server.URL + suffix})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	return client, server
}

func writeFile(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "file.pem")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	return path
}
