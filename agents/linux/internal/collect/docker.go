package collect

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// MaxContainers bounds how many containers one host may report.
//
// Cardinality, and also *work*: each running container costs two API calls
// per collection, so this is the ceiling on both the series count and the
// request rate. A hundred running containers on one homelab host is already
// an unusual machine.
const MaxContainers = 100

// MaxContainerNameChars is the longest container name that can be a label.
// Over it, the container is skipped rather than truncated — two names cut to
// the same 64 characters would become one series.
const MaxContainerNameChars = 64

// DockerAPIVersion is the Engine API version this speaks.
//
// 1.41 is Docker 20.10 (December 2020) and is the first with `one-shot` on
// the stats endpoint, which is what makes a collection cheap — see
// [Docker.Collect]. Pinned rather than negotiated: an unpinned client gets
// whatever the daemon's newest is, and a field that changes shape between
// versions would then change shape under a running agent.
const DockerAPIVersion = "v1.41"

// dockerRequestTimeout bounds one API call.
//
// Short, because the daemon is on a local socket and a slow answer means it
// is in trouble rather than busy. The whole collection is additionally
// bounded by the scheduler's own deadline.
const dockerRequestTimeout = 5 * time.Second

// Docker reports container state and resource usage from the Docker Engine
// API.
//
// # This is monitoring, and the socket is a privilege
//
// Read-only: the only endpoints used are `GET /containers/json`,
// `GET /containers/{id}/json` and `GET /containers/{id}/stats`. There is no
// start, no stop, no exec, and no code path that builds a request with any
// other method — a container control surface is a separate decision with a
// separate threat model, and this is not it.
//
// That restraint is necessary and not sufficient, because **access to the
// Docker socket is equivalent to root on the host**. Anything that can talk
// to it can start a privileged container with the host filesystem mounted.
// So the capability is granted *locally and explicitly*, by an administrator
// on the machine setting [config.EnvDockerSocket], and the controller can
// only decide whether to *use* a capability the host has already granted.
// There is no default path: an unset socket makes the module unavailable
// with a reason that says exactly what to set and what it means.
//
// Two independent switches, and both must be on:
//
//  1. The host administrator sets `HCC_AGENT_DOCKER_SOCKET`. A compromised
//     controller cannot do this.
//  2. An operator enables the `docker` module for the host. Every module
//     ships disabled and the registry cannot express otherwise.
//
// # Rates are computed here
//
// `/stats?stream=false` without `one-shot` makes the daemon take two samples
// internally, which costs a second per container. With `one-shot=true` it
// answers immediately and `precpu_stats` is empty — so the delta is computed
// from this collector's own previous sample, exactly as [CPU] and [DiskIO]
// do, and for the same reasons (ADR-065). A container that restarted between
// samples has counters that went backwards, and that interval produces
// nothing rather than a spike.
//
// # Identity is the container name
//
// Not the id, which changes every time a container is recreated — so a
// `docker compose up` would end every series and start new ones. The name is
// what an operator recognises and what Compose keeps stable across
// recreation. A genuinely renamed container does start a new series, which
// is honest: it is a different thing now.
type Docker struct {
	// Socket is the Docker Engine API socket path. **Empty disables the
	// module**, which is the default and is the security boundary: see the
	// type comment.
	Socket string

	mu       sync.Mutex
	previous map[string]containerCounters
	taken    time.Time

	clientOnce sync.Once
	client     *http.Client
}

// containerCounters is the subset of the stats response rates need.
type containerCounters struct {
	cpuNanoseconds    float64
	systemNanoseconds float64
	receiveBytes      float64
	transmitBytes     float64
}

// NewDocker builds a Docker collector. An empty socket disables it.
func NewDocker(socket string) *Docker {
	return &Docker{Socket: strings.TrimSpace(socket)}
}

// Module implements [Collector].
func (d *Docker) Module() string { return "docker" }

// Available implements [Collector].
//
// Four distinguishable answers, because they call for four different
// actions: not configured, socket missing, permission denied, and daemon not
// answering. "Docker monitoring does not work" would send an operator to the
// wrong one three times out of four.
func (d *Docker) Available() Availability {
	if d.Socket == "" {
		return Availability{
			Available: false,
			Reason: "container monitoring is not enabled on this host. Access to the " +
				"Docker socket is equivalent to root, so it is granted on the machine " +
				"rather than from the controller: set HCC_AGENT_DOCKER_SOCKET=" +
				"/var/run/docker.sock in /etc/hcc-agent/agent.env and make sure the " +
				"agent account can read that socket",
		}
	}
	info, err := os.Stat(d.Socket)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return Availability{
			Available: false,
			Reason: fmt.Sprintf(
				"%s does not exist, so Docker is not running on this host (or its "+
					"socket is somewhere else)", d.Socket,
			),
		}
	case err != nil:
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s cannot be examined: %v", d.Socket, err),
		}
	case info.Mode()&os.ModeSocket == 0:
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("%s is not a socket", d.Socket),
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), dockerRequestTimeout)
	defer cancel()
	if err := d.ping(ctx); err != nil {
		if errors.Is(err, os.ErrPermission) {
			return Availability{
				Available: false,
				Reason: fmt.Sprintf(
					"the agent account cannot open %s. Add it to the group that owns "+
						"the socket — on most systems `usermod -aG docker %s` — and "+
						"restart the service", d.Socket, ServiceAccountName,
				),
			}
		}
		return Availability{
			Available: false,
			Reason:    fmt.Sprintf("the Docker daemon at %s did not answer: %v", d.Socket, err),
		}
	}
	return Availability{Available: true}
}

// ServiceAccountName is the account the agent runs as, named here so the
// permission advice above can say it without importing internal/install —
// which would be a dependency from a collector onto the installer.
const ServiceAccountName = "hcc-agent"

// Collect implements [Collector].
func (d *Docker) Collect(ctx context.Context) ([]Sample, error) {
	if d.Socket == "" {
		return nil, errors.New("container monitoring is not enabled on this host")
	}

	containers, err := d.list(ctx)
	if err != nil {
		return nil, err
	}

	now := time.Now()
	d.mu.Lock()
	previous := d.previous
	taken := d.taken
	current := make(map[string]containerCounters, len(containers))
	d.mu.Unlock()

	elapsed := 0.0
	if previous != nil {
		elapsed = now.Sub(taken).Seconds()
	}

	samples := make([]Sample, 0, len(containers)*4+2)
	running := 0

	for _, container := range containers {
		name := container.displayName()
		if name == "" || len(name) > MaxContainerNameChars {
			continue
		}
		labels := map[string]string{"container": name}
		isRunning := container.State == "running"
		if isRunning {
			running++
		}
		samples = append(samples, Sample{
			Metric: "running", Labels: labels, Value: boolValue(isRunning),
		})
		if !isRunning {
			// A stopped container has no resource usage to report and a
			// restart count that cannot change while it is stopped. Querying
			// it would be two API calls for a row of zeros.
			continue
		}

		if details, err := d.inspect(ctx, container.ID); err == nil {
			samples = append(samples, Sample{
				Metric: "restart_count", Labels: labels,
				Value: float64(details.RestartCount),
			})
			if started := details.startedAt(); !started.IsZero() {
				samples = append(samples, Sample{
					Metric: "uptime_seconds", Labels: labels,
					Value: now.Sub(started).Seconds(),
				})
			}
			// Absent when the image declares no healthcheck, which is most
			// images. "0" would say unhealthy and "1" would say healthy, and
			// neither is true of a container nobody is checking.
			if status := details.healthStatus(); status != "" {
				samples = append(samples, Sample{
					Metric: "healthy", Labels: labels,
					Value: boolValue(status == "healthy"),
				})
			}
		}

		stats, err := d.stats(ctx, container.ID)
		if err != nil {
			// One container that will not answer — it exited between the
			// list and the stats call, which is routine — must not lose the
			// rest of the host's containers.
			continue
		}
		counters := stats.counters()
		current[name] = counters

		if limit := stats.MemoryStats.Limit; limit > 0 {
			samples = append(samples,
				Sample{Metric: "memory_bytes", Labels: labels, Value: stats.MemoryStats.Usage},
				Sample{Metric: "memory_limit", Labels: labels, Value: limit},
				Sample{
					Metric: "memory_utilisation", Labels: labels,
					Value: percent(stats.MemoryStats.Usage, limit),
				},
			)
		}

		before, had := previous[name]
		if !had || elapsed <= 0 {
			continue
		}
		if value, ok := containerCPU(before, counters, stats.CPUStats.OnlineCPUs); ok {
			samples = append(samples, Sample{
				Metric: "cpu_utilisation", Labels: labels, Value: value,
			})
		}
		if value, ok := rate(before.receiveBytes, counters.receiveBytes, elapsed); ok {
			samples = append(samples, Sample{
				Metric: "receive_bytes", Labels: labels, Value: value,
			})
		}
		if value, ok := rate(before.transmitBytes, counters.transmitBytes, elapsed); ok {
			samples = append(samples, Sample{
				Metric: "transmit_bytes", Labels: labels, Value: value,
			})
		}
	}

	// Host-wide counts, reported on every collection including the first.
	// "How many containers are on this host" needs no delta and is the
	// number an operator checks first.
	samples = append(samples,
		Sample{Metric: "containers_total", Value: float64(len(containers))},
		Sample{Metric: "containers_running", Value: float64(running)},
	)

	d.mu.Lock()
	d.previous = current
	d.taken = now
	d.mu.Unlock()

	kept, _ := keepValid(samples)
	sortSamples(kept)
	return kept, nil
}

// containerCPU computes a container's share of the host's CPU.
//
// Docker's own formula: the container's CPU-time delta over the *system*
// CPU-time delta, scaled by the number of CPUs the container may use. The
// result is a percentage of one CPU times the core count, so a container
// saturating four cores on a twenty-core host reads 400%, not 20% — which is
// the convention `docker stats` prints and the one an operator expects.
//
// Reports false when either counter went backwards, which on a container
// means it was restarted between samples.
func containerCPU(before, after containerCounters, onlineCPUs float64) (float64, bool) {
	cpuDelta := after.cpuNanoseconds - before.cpuNanoseconds
	systemDelta := after.systemNanoseconds - before.systemNanoseconds
	if cpuDelta < 0 || systemDelta <= 0 {
		return 0, false
	}
	if onlineCPUs <= 0 {
		onlineCPUs = 1
	}
	return cpuDelta / systemDelta * onlineCPUs * 100, true
}

// --- The Engine API ---------------------------------------------------------

type dockerContainer struct {
	ID    string   `json:"Id"`
	Names []string `json:"Names"`
	State string   `json:"State"`
}

// displayName is the container's name without the API's leading slash.
func (c dockerContainer) displayName() string {
	if len(c.Names) == 0 {
		return ""
	}
	return strings.TrimPrefix(c.Names[0], "/")
}

type dockerInspect struct {
	RestartCount int `json:"RestartCount"`
	State        struct {
		StartedAt string `json:"StartedAt"`
		Health    *struct {
			Status string `json:"Status"`
		} `json:"Health"`
	} `json:"State"`
}

func (d dockerInspect) startedAt() time.Time {
	parsed, err := time.Parse(time.RFC3339Nano, d.State.StartedAt)
	if err != nil {
		return time.Time{}
	}
	return parsed
}

func (d dockerInspect) healthStatus() string {
	if d.State.Health == nil {
		return ""
	}
	return d.State.Health.Status
}

type dockerStats struct {
	CPUStats struct {
		CPUUsage struct {
			TotalUsage float64 `json:"total_usage"`
		} `json:"cpu_usage"`
		SystemCPUUsage float64 `json:"system_cpu_usage"`
		OnlineCPUs     float64 `json:"online_cpus"`
	} `json:"cpu_stats"`
	MemoryStats struct {
		Usage float64 `json:"usage"`
		Limit float64 `json:"limit"`
	} `json:"memory_stats"`
	Networks map[string]struct {
		ReceiveBytes  float64 `json:"rx_bytes"`
		TransmitBytes float64 `json:"tx_bytes"`
	} `json:"networks"`
}

// counters sums the per-interface figures into one pair.
//
// Summed here and only here: a container's interfaces are its own and do not
// overlap, unlike the host's, so this is the one place in the agent where
// adding network counters together is correct.
func (s dockerStats) counters() containerCounters {
	counters := containerCounters{
		cpuNanoseconds:    s.CPUStats.CPUUsage.TotalUsage,
		systemNanoseconds: s.CPUStats.SystemCPUUsage,
	}
	for _, network := range s.Networks {
		counters.receiveBytes += network.ReceiveBytes
		counters.transmitBytes += network.TransmitBytes
	}
	return counters
}

// httpClient builds a client that speaks HTTP over the unix socket.
//
// The standard library and nothing else. The official Docker SDK pulls in a
// large dependency tree for an agent whose whole point is being one static
// binary, and the three endpoints this uses are plain GETs.
func (d *Docker) httpClient() *http.Client {
	d.clientOnce.Do(func() {
		socket := d.Socket
		d.client = &http.Client{
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					return (&net.Dialer{}).DialContext(ctx, "unix", socket)
				},
				// One idle connection is enough: collections are serialised
				// within a module and a pool would hold file descriptors
				// open against a socket the operator may revoke.
				MaxIdleConns:    1,
				IdleConnTimeout: 30 * time.Second,
			},
		}
	})
	return d.client
}

// MaxDockerResponseBytes bounds one API response.
//
// The daemon is local and trusted-ish, and the bound exists anyway: a
// container list is the one response whose size scales with something an
// attacker on this host controls, and an unbounded read is the agent's
// memory.
const MaxDockerResponseBytes = 8 << 20

// get performs one read-only API call and decodes it.
//
// The only request builder in this file, and it only ever issues GET — which
// is what makes "this cannot start a container" a property of the code
// rather than a promise.
func (d *Docker) get(ctx context.Context, path string, into any) error {
	ctx, cancel := context.WithTimeout(ctx, dockerRequestTimeout)
	defer cancel()

	// The host is ignored by the unix dialer and has to be *something*.
	request, err := http.NewRequestWithContext(
		ctx, http.MethodGet, "http://docker/"+DockerAPIVersion+path, nil,
	)
	if err != nil {
		return err
	}
	response, err := d.httpClient().Do(request)
	if err != nil {
		// net/http wraps the dial error; the permission case is the one
		// worth naming, because its remedy is a group membership rather
		// than anything about Docker.
		if strings.Contains(err.Error(), "permission denied") {
			return fmt.Errorf("%w: %s", os.ErrPermission, d.Socket)
		}
		return err
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
		return fmt.Errorf("the Docker API answered HTTP %d for %s", response.StatusCode, path)
	}
	if into == nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, MaxDockerResponseBytes))
		return nil
	}
	return json.NewDecoder(io.LimitReader(response.Body, MaxDockerResponseBytes)).Decode(into)
}

func (d *Docker) ping(ctx context.Context) error {
	return d.get(ctx, "/version", nil)
}

func (d *Docker) list(ctx context.Context) ([]dockerContainer, error) {
	var containers []dockerContainer
	// all=1: a stopped container is a thing an operator wants to see, and
	// "the container I expected is not running" is the commonest question
	// this module answers.
	if err := d.get(ctx, "/containers/json?all=1", &containers); err != nil {
		return nil, fmt.Errorf("cannot list containers: %w", err)
	}
	if len(containers) > MaxContainers {
		containers = containers[:MaxContainers]
	}
	return containers, nil
}

func (d *Docker) inspect(ctx context.Context, id string) (dockerInspect, error) {
	var details dockerInspect
	err := d.get(ctx, "/containers/"+url.PathEscape(id)+"/json", &details)
	return details, err
}

func (d *Docker) stats(ctx context.Context, id string) (dockerStats, error) {
	var stats dockerStats
	// one-shot, which is why this is affordable: without it the daemon takes
	// two samples internally and blocks for a second per container. The
	// delta is this collector's own, from its previous sample.
	err := d.get(
		ctx, "/containers/"+url.PathEscape(id)+"/stats?stream=false&one-shot=true", &stats,
	)
	return stats, err
}
