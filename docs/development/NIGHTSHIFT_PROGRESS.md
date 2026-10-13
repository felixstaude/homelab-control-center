# Operation NIGHTSHIFT — progress

Working state for the multi-milestone run covering Phase 2 (configuration and
scheduling) and the core-metrics part of Phase 3. Factual and terse: this file
records *what was done and verified*, so that work is not repeated after a
context loss.

**This file is not evidence that anything passed.** Test counts here were
copied from a run whose command is named beside them; re-run the command rather
than trusting the number.

---

## Preflight — complete

Milestone 1.8's exit criteria were re-verified before starting, on an idle
machine, in one pass:

| Gate | Command | Result |
| --- | --- | --- |
| Everything CI runs | `make check` | ✓ all gates |
| Backend unit | included above | 634 passed |
| Backend integration | `make test-integration` | 734 passed |
| Frontend unit | included in `make check` | 407 passed |
| Go, with race detector | `make check-agent` | 8 packages ok |
| Browser, with a real agent | `E2E_REQUIRE_AGENT=1 make e2e` | 151 passed |

Three defects found and fixed in commit `30c3a9f` before starting: a browser
suite run-order contract that held only by alphabetical accident,
`make test-integration` pointing at the developer's own database, and an
unformatted migration.

All thirteen §0 preconditions hold: host registry, host API, secure
enrollment, a Go agent that enrolls, credentials persisted at 0600, an
authenticated outbound WebSocket, derived connectivity, heartbeats,
reconnection, revocation, a hosts page on real API data, Go tests, and
controller–agent integration tests.

---

## Design decisions taken at Stage 1

Each is an amendment to a committed design document, so each gets an ADR
rather than being decided in a commit message.

| # | Decision | Amends |
| --- | --- | --- |
| D1 | **Five modules, not two.** `cpu`, `memory`, `swap`, `load`, `uptime` are separate modules rather than `cpu` (incl. load) and `memory` (incl. swap). | `MONITORING_MODULES.md §3` |
| D2 | **Configuration travels on the agent WebSocket**, not over a separate HTTP fetch/ack pair. | `AGENT_ARCHITECTURE.md §6.3` |
| D3 | **Telemetry travels on the agent WebSocket** too, not as HTTP batches. | `AGENT_ARCHITECTURE.md §2` |
| D4 | **Inheritance is per field and explicit.** An override row stores NULL for "inherit this field"; the API reports a `source` for every resolved value. | new |
| D5 | **Configuration history is full snapshots**, not diffs. | new |
| D6 | **Rollup tiers are raw → 5-minute → hourly.** No 1-minute tier. | `DATA_RETENTION.md §3` (confirms it) |

Reasons are in the ADRs; the summary is:

- **D1** — default-deny is per module (§2.1), so the module is the unit of
  consent; a swapless host must be able to report `swap` unavailable without
  disabling `memory`; and load average must not be presentable as a CPU
  percentage, which separate modules enforce in the data model rather than in a
  UI convention.
- **D2/D3** — the socket is already authenticated and identity-bound, so
  neither needs a second credential path; push removes the polling latency; and
  it is one fewer endpoint to defend. The *pull* remains the guarantee (the
  heartbeat carries the running version, so a missed push self-corrects), and
  the push is only a latency optimisation — the same
  event-stream-plus-reconciliation shape `SCHEDULING.md §9.2` already requires.
- **D4** — "`enabled: false`", "no value set" and "inherited false" must not be
  the same state on the wire or in the database.
- **D5** — the question asked of history is "what document was this agent
  running?", and a snapshot answers it without replay. A checksum over a
  reconstructed diff chain is a checksum over something nobody sent.
- **D6** — at the 10-second default a 1-minute bucket is 6:1, and the query
  path in `DATA_RETENTION.md §5.1` never selects it.

---

## Stages

| Stage | State |
| --- | --- |
| 1. Preflight and architecture | ✅ |
| 2. Configuration persistence | ✅ |
| 3. Configuration APIs | ✅ |
| 4. Configuration synchronisation | ✅ |
| 5. Scheduling engine | ✅ |
| 6. Core collectors | ✅ |
| 7. Telemetry transport | ✅ |
| 8. Historical storage | ✅ |
| 9. Retention foundation | ✅ |
| 10. Host monitoring frontend | ✅ |
| 11. Global settings | ✅ |
| 12. Overview integration | ✅ real hosts and utilisation; container and log cards remain labelled fixtures |
| 13. End-to-end integration | ✅ |
| 14. Security review | ✅ |
| 15. Performance engineering | ✅ |
| 16. Reliability testing | ✅ |
| 17. Documentation | ✅ |
| 18. Final verification | ✅ |

---

## Commits

| SHA | Subject |
| --- | --- |
| `30c3a9f` | Milestone 1.8 (10/10): the three defects the full suite found |
| `3bb73b0` | NIGHTSHIFT (1/?): configuration persistence and the contracts under it |
| `6bfb3ac` | NIGHTSHIFT (2/?): the configuration API, split across two gates |
| `161c73b` | NIGHTSHIFT (3/?): configuration on the agent socket, controller half |
| `8add7c6` | NIGHTSHIFT (4/?): the agent applies, acknowledges and refuses configuration |
| `475674d` | NIGHTSHIFT (5/?): the scheduler, the calendar engine and five real collectors |
| `35a3b61` | NIGHTSHIFT (6/?): telemetry ingestion and time-partitioned storage |
| `8ca8413` | NIGHTSHIFT (7/?): the agent collects, queues and sends — end to end in tests |
| `e27773e` | Milestone 1.9 (10/?): the retention job, and the bug that meant it never ran |
| `d582139` | Milestone 1.9 (11/?): the first real metrics on screen, and three states not two |
| `f481085` | Milestone 1.9 (12/?): the overview stops being entirely fiction |
| `ce2527b` | Milestone 1.9 (13/?): the pipeline proven end to end, with nothing faked |
| `f3c68f4` | Milestone 1.9 (14/?): the security review, as attacks on what this phase added |
| `6251bf5` | Milestone 1.9 (15/?): the performance measurement, and the two defects it found |
| `6f37532` | Milestone 1.9 (16/?): reliability, with real processes actually dying |
| `1250a5e` | Milestone 1.9 (17/?): the documentation, with the parts that are not done marked |
| `8c7a119` | Milestone 1.9 (18/?): the browser suite, and CI for the two new scenarios |

---

## Verified at the end of the run

Every gate, run on an idle machine in one pass. **Re-run these; do not trust
the numbers.**

| Gate | Command | Result |
| --- | --- | --- |
| Everything CI runs | `make check` | ✓ all gates |
| Backend unit | included above | 802 passed, 1,005 skipped |
| Frontend unit | included above | 542 passed |
| Go, with the race detector | included above | 12 packages ok |
| Documentation | included above | 10/10 checks |
| OpenAPI drift | included above | up to date |
| Cross-language contracts | included above | up to date |
| Backend integration | `make test-integration` | 1,807 passed |
| Real server over HTTP | `make smoke` | 48 checks |
| Browser, with a real agent | `make e2e` | 151 passed |
| Real agent, real /proc | `make monitoring-e2e` | 60 checks |
| Failure recovery | `make reliability-e2e` | 24 checks |
| Measurement | `make measure-metrics` | numbers in `PERFORMANCE.md` §4 |

The skipped backend unit tests are the integration-marked ones, which the
unit tier cannot run: they need a database, and `make test-integration`
supplies a disposable one.

---

## What is not done

Carried into the roadmap as 🟡 rather than left implicit. Phase 2: capability
reporting, fleet-scale jitter verification, projected storage cost in the
configuration UI, and `event`/`manual` schedule kinds. Phase 3: the disk,
disk-I/O, network and hardware-inventory modules, the storage budget, and the
deletion of `src/mocks/` — the container and log cards remain fixtures
because Docker discovery is Phase 4 and log collection Phase 6.

Three of `DATA_RETENTION.md` §8's six required benchmarks are complete, two
partial and one not applicable; the table there says which.
