package collect_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/felixstaude/homelab-control-center/agents/linux/internal/collect"
)

// A fake Docker daemon on a real unix socket.
//
// A real socket and a real HTTP server rather than an injected transport,
// because the two things most likely to be wrong here are *not* the JSON
// decoding: they are whether the client dials a unix path at all, and
// whether an unreadable socket is reported as a permission problem rather
// than as "Docker is broken". Neither is observable through a fake
// transport.
type fakeDaemon struct {
	t          *testing.T
	socket     string
	containers []map[string]any
	// stats and inspect are keyed by container id.
	stats   map[string]map[string]any
	inspect map[string]map[string]any
	// requests records every path, so a test can assert that a stopped
	// container was never asked about and that nothing but GET was used.
	requests []string
	methods  map[string]bool
	server   *http.Server
}

func newFakeDaemon(t *testing.T) *fakeDaemon {
	t.Helper()
	// A short directory: a unix socket path is capped at 108 bytes, and
	// t.TempDir() under a long test name can exceed it.
	directory, err := os.MkdirTemp("", "hccd")
	if err != nil {
		t.Fatalf("cannot create a socket directory: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(directory) })

	daemon := &fakeDaemon{
		t:       t,
		socket:  filepath.Join(directory, "d.sock"),
		stats:   map[string]map[string]any{},
		inspect: map[string]map[string]any{},
		methods: map[string]bool{},
	}

	listener, err := net.Listen("unix", daemon.socket)
	if err != nil {
		t.Fatalf("cannot listen on %s: %v", daemon.socket, err)
	}
	daemon.server = &http.Server{Handler: daemon} //nolint:gosec // a test server on a temp socket
	go func() { _ = daemon.server.Serve(listener) }()
	t.Cleanup(func() { _ = daemon.server.Close() })
	return daemon
}

func (f *fakeDaemon) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	f.requests = append(f.requests, request.URL.Path)
	f.methods[request.Method] = true

	path := request.URL.Path
	switch {
	case strings.HasSuffix(path, "/version"):
		f.write(writer, map[string]any{"ApiVersion": "1.41"})
	case strings.HasSuffix(path, "/containers/json"):
		f.write(writer, f.containers)
	case strings.HasSuffix(path, "/stats"):
		id := pathSegment(path, "containers")
		if body, ok := f.stats[id]; ok {
			f.write(writer, body)
			return
		}
		http.Error(writer, "no such container", http.StatusNotFound)
	case strings.HasSuffix(path, "/json"):
		id := pathSegment(path, "containers")
		if body, ok := f.inspect[id]; ok {
			f.write(writer, body)
			return
		}
		http.Error(writer, "no such container", http.StatusNotFound)
	default:
		http.Error(writer, "unexpected", http.StatusNotFound)
	}
}

func (f *fakeDaemon) write(writer http.ResponseWriter, body any) {
	writer.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(writer).Encode(body); err != nil {
		f.t.Errorf("cannot encode: %v", err)
	}
}

// pathSegment returns the segment after `after` in a slash-separated path.
func pathSegment(path, after string) string {
	parts := strings.Split(path, "/")
	for index, part := range parts {
		if part == after && index+1 < len(parts) {
			return parts[index+1]
		}
	}
	return ""
}

// container adds a container to the fake daemon's world.
func (f *fakeDaemon) container(id, name, state string) {
	f.containers = append(f.containers, map[string]any{
		"Id": id, "Names": []string{"/" + name}, "State": state,
	})
}

func (f *fakeDaemon) withStats(id string, cpuNanos, systemNanos, memory, limit, rx, tx float64) {
	f.stats[id] = map[string]any{
		"cpu_stats": map[string]any{
			"cpu_usage":        map[string]any{"total_usage": cpuNanos},
			"system_cpu_usage": systemNanos,
			"online_cpus":      4,
		},
		"memory_stats": map[string]any{"usage": memory, "limit": limit},
		"networks": map[string]any{
			"eth0": map[string]any{"rx_bytes": rx, "tx_bytes": tx},
		},
	}
}

func (f *fakeDaemon) withInspect(id string, restarts int, startedAt, health string) {
	state := map[string]any{"StartedAt": startedAt}
	if health != "" {
		state["Health"] = map[string]any{"Status": health}
	}
	f.inspect[id] = map[string]any{"RestartCount": restarts, "State": state}
}

func (f *fakeDaemon) collector() *collect.Docker {
	return collect.NewDocker(f.socket)
}

// --- The security boundary --------------------------------------------------

func TestContainerMonitoringIsOffUntilTheHostGrantsIt(t *testing.T) {
	// The whole security argument. Access to the Docker socket is equivalent
	// to root on the host, so the capability is granted *on the machine* by
	// an administrator — the controller can only decide whether to use one
	// the host has already given. An unset socket is not a misconfiguration
	// to work around; it is the default.
	collector := collect.NewDocker("")

	availability := collector.Available()

	if availability.Available {
		t.Fatal("container monitoring was available without a local grant")
	}
	for _, fragment := range []string{"HCC_AGENT_DOCKER_SOCKET", "equivalent to root"} {
		if !strings.Contains(availability.Reason, fragment) {
			t.Errorf("the reason does not mention %q: %s", fragment, availability.Reason)
		}
	}
	// And it collects nothing rather than falling back to a default path.
	if _, err := collector.Collect(context.Background()); err == nil {
		t.Error("an unconfigured collector produced a collection")
	}
}

func TestOnlyReadOnlyRequestsAreEverMade(t *testing.T) {
	// This is monitoring, not container administration. Asserted against the
	// methods the daemon actually saw, so a future `POST /containers/x/stop`
	// is a failing test rather than a code review somebody skipped.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "web", "running")
	daemon.withStats("a1", 1000, 100000, 1024, 2048, 10, 20)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")

	if _, err := daemon.collector().Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}

	for method := range daemon.methods {
		if method != http.MethodGet {
			t.Errorf("the collector issued a %s request", method)
		}
	}
}

func TestAMissingSocketIsADifferentAnswerFromAnUngrantedOne(t *testing.T) {
	// "Docker is not running here" and "you have not granted access" have
	// different remedies, and one message for both sends an operator to the
	// wrong one half the time.
	collector := collect.NewDocker(filepath.Join(t.TempDir(), "absent.sock"))

	availability := collector.Available()

	if availability.Available {
		t.Fatal("a missing socket reported available")
	}
	if !strings.Contains(availability.Reason, "does not exist") {
		t.Errorf("the reason does not name the cause: %s", availability.Reason)
	}
	if strings.Contains(availability.Reason, "HCC_AGENT_DOCKER_SOCKET") {
		t.Error("a missing socket was reported as an ungranted one")
	}
}

func TestANonSocketPathIsRefused(t *testing.T) {
	ordinary := filepath.Join(t.TempDir(), "not-a-socket")
	if err := os.WriteFile(ordinary, []byte("x"), 0o600); err != nil {
		t.Fatalf("cannot write: %v", err)
	}

	availability := collect.NewDocker(ordinary).Available()

	if availability.Available {
		t.Fatal("an ordinary file was accepted as a Docker socket")
	}
	if !strings.Contains(availability.Reason, "not a socket") {
		t.Errorf("the reason does not name the cause: %s", availability.Reason)
	}
}

func TestAGrantedSocketWithAWorkingDaemonIsAvailable(t *testing.T) {
	daemon := newFakeDaemon(t)

	if availability := daemon.collector().Available(); !availability.Available {
		t.Errorf("a working daemon reported unavailable: %s", availability.Reason)
	}
}

// --- What it reports --------------------------------------------------------

func TestStoppedContainersAreListedAndNotQueried(t *testing.T) {
	// A stopped container is something an operator wants to see — "the
	// container I expected is not running" is the commonest question this
	// answers — and it has no resource usage. Querying it would be two API
	// calls for a row of zeros.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "web", "running")
	daemon.withStats("a1", 1000, 100000, 1024, 2048, 0, 0)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")
	daemon.container("b2", "old-worker", "exited")

	samples, err := daemon.collector().Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if value, found := labelled(samples, "running", "container", "old-worker"); !found || value != 0 {
		t.Errorf("a stopped container is missing or not reported as stopped: %v %v", value, found)
	}
	for _, path := range daemon.requests {
		if strings.Contains(path, "b2") {
			t.Errorf("a stopped container was queried: %s", path)
		}
	}
	if value, _ := labelled(samples, "containers_total", "", ""); value != 2 {
		t.Errorf("containers_total = %v, want 2", value)
	}
	if value, _ := labelled(samples, "containers_running", "", ""); value != 1 {
		t.Errorf("containers_running = %v, want 1", value)
	}
}

func TestCPUIsAShareOfOneCoreTimesTheCoreCount(t *testing.T) {
	// Docker's own convention: a container saturating four cores reads 400%,
	// which is what `docker stats` prints. Rescaling it to a share of the
	// host would disagree with every other tool an operator has.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "busy", "running")
	daemon.withStats("a1", 0, 0, 1024, 2048, 0, 0)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")
	collector := daemon.collector()
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("first collect: %v", err)
	}

	// Half the system's CPU time, on a 4-CPU container: 0.5 × 4 × 100 = 200%.
	daemon.withStats("a1", 500, 1000, 1024, 2048, 0, 0)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("second collect: %v", err)
	}

	value, found := labelled(samples, "cpu_utilisation", "container", "busy")
	if !found {
		t.Fatalf("no cpu_utilisation: %v", samples)
	}
	if value != 200 {
		t.Errorf("cpu_utilisation = %v, want 200", value)
	}
}

func TestTheFirstCollectionReportsStateButNoRates(t *testing.T) {
	// A rate needs two samples, and "is this container running" does not.
	// Making an operator wait an interval for the thing they opened the page
	// to see would be the wrong trade.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "web", "running")
	daemon.withStats("a1", 1000, 100000, 4096, 8192, 10, 20)
	daemon.withInspect("a1", 3, "2026-10-15T06:00:00Z", "")

	samples, err := daemon.collector().Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if value, found := labelled(samples, "running", "container", "web"); !found || value != 1 {
		t.Error("the first collection did not report running state")
	}
	if value, _ := labelled(samples, "restart_count", "container", "web"); value != 3 {
		t.Errorf("restart_count = %v, want 3", value)
	}
	if value, _ := labelled(samples, "memory_bytes", "container", "web"); value != 4096 {
		t.Errorf("memory_bytes = %v, want 4096", value)
	}
	if _, found := labelled(samples, "cpu_utilisation", "container", "web"); found {
		t.Error("the first collection produced a CPU rate")
	}
}

func TestARestartedContainerProducesAGapNotASpike(t *testing.T) {
	// A container's CPU counter resets when it restarts. Treating the
	// negative delta as a positive one renders as several hundred percent at
	// the exact moment an operator is working out why it restarted.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "flaky", "running")
	daemon.withStats("a1", 900_000, 1_000_000, 1024, 2048, 5000, 5000)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")
	collector := daemon.collector()
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("first collect: %v", err)
	}

	// Restarted: the container's own counters are back near zero.
	daemon.withStats("a1", 10, 2_000_000, 1024, 2048, 50, 50)
	samples, err := collector.Collect(context.Background())
	if err != nil {
		t.Fatalf("second collect: %v", err)
	}

	if _, found := labelled(samples, "cpu_utilisation", "container", "flaky"); found {
		t.Error("a reset CPU counter produced a utilisation")
	}
	if _, found := labelled(samples, "receive_bytes", "container", "flaky"); found {
		t.Error("a reset network counter produced a rate")
	}
	// State is still reported: the container is running, and that is true.
	if value, _ := labelled(samples, "running", "container", "flaky"); value != 1 {
		t.Error("the container's state was lost along with its rates")
	}
}

func TestHealthIsAbsentWhereNoHealthcheckExists(t *testing.T) {
	// Most images declare none. 0 would say unhealthy and 1 would say
	// healthy, and neither is true of a container nobody is checking.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "unchecked", "running")
	daemon.withStats("a1", 1000, 100000, 1024, 2048, 0, 0)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")
	daemon.container("b2", "checked", "running")
	daemon.withStats("b2", 1000, 100000, 1024, 2048, 0, 0)
	daemon.withInspect("b2", 0, "2026-10-15T06:00:00Z", "unhealthy")

	samples, err := daemon.collector().Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	if _, found := labelled(samples, "healthy", "container", "unchecked"); found {
		t.Error("a container with no healthcheck was given a health value")
	}
	value, found := labelled(samples, "healthy", "container", "checked")
	if !found {
		t.Fatal("a container with a healthcheck has no health value")
	}
	if value != 0 {
		t.Errorf("healthy(unhealthy container) = %v, want 0", value)
	}
}

func TestOneContainerThatVanishesDoesNotLoseTheRest(t *testing.T) {
	// A container exiting between the list and the stats call is routine.
	daemon := newFakeDaemon(t)
	daemon.container("a1", "survivor", "running")
	daemon.withStats("a1", 1000, 100000, 1024, 2048, 0, 0)
	daemon.withInspect("a1", 0, "2026-10-15T06:00:00Z", "")
	// No stats and no inspect registered: the daemon answers 404.
	daemon.container("gone", "vanished", "running")

	samples, err := daemon.collector().Collect(context.Background())

	if err != nil {
		t.Fatalf("one vanished container failed the collection: %v", err)
	}
	if _, found := labelled(samples, "memory_bytes", "container", "survivor"); !found {
		t.Error("the surviving container was lost")
	}
	// Its state was still listed, because the list is what said it existed.
	if value, _ := labelled(samples, "running", "container", "vanished"); value != 1 {
		t.Error("a container the list reported was dropped entirely")
	}
}

func TestADeadDaemonFailsTheCollectionRatherThanReportingZero(t *testing.T) {
	// "Zero containers" and "the daemon is not answering" are different
	// facts, and a monitoring tool that renders the second as the first
	// tells an operator their containers are gone.
	daemon := newFakeDaemon(t)
	collector := daemon.collector()
	_ = daemon.server.Close()
	if err := os.Remove(daemon.socket); err != nil {
		t.Fatalf("cannot remove the socket: %v", err)
	}

	if _, err := collector.Collect(context.Background()); err == nil {
		t.Fatal("a dead daemon produced a successful collection")
	}
}

func TestTheContainerCountIsCappedWithoutLosingTheCount(t *testing.T) {
	daemon := newFakeDaemon(t)
	for index := 0; index < collect.MaxContainers+10; index++ {
		daemon.container(fmt.Sprintf("c%d", index), fmt.Sprintf("app-%d", index), "exited")
	}

	samples, err := daemon.collector().Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	series := 0
	for _, sample := range samples {
		if sample.Metric == "running" {
			series++
		}
	}
	if series > collect.MaxContainers {
		t.Errorf("%d container series, over the %d cap", series, collect.MaxContainers)
	}
}

func TestTheDockerCollectorIdentifiesItself(t *testing.T) {
	if got := collect.NewDocker("").Module(); got != "docker" {
		t.Errorf("docker module = %q", got)
	}
}
