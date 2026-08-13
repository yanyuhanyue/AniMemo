# AniMemo v1.0 Concurrency and Long-Task Isolation

Status: **INCOMPLETE / RELEASE BLOCKER**

Application candidate: `306878de039cba6a706ce11c03ac9dab97448917`

Performance workflow: GitHub Actions run `31722761167`
Completed: 2026-08-14, Asia/Shanghai

## Required decision

The Final RC request required at least this matrix against a fake or stubbed slow provider:

```text
normal API users: 20 / 40 / 60
concurrent long operations: 0 / 2 / 4 / 8
```

It also required normal-API p50/p95/p99, throughput, timeouts, 5xx, 429, CPU, memory, PostgreSQL connections, Redis use, worker saturation, and long-operation latency.

That matrix was **NOT RUN**. No long-running provider operation was injected, and the existing harness reports p50/p95 but not p99 or 429 for the required matrix. Therefore the Job Queue decision is **INCONCLUSIVE** and cannot be promoted to `DEFER v1.1` or `STRUCTURAL RC1` from the current measurements.

## Evidence that did run

Run `31722761167` completed successfully on exact SHA `306878de039cba6a706ce11c03ac9dab97448917`:

| Job | Result |
| --- | --- |
| `backend-postgresql-probe` | PASS |
| `frontend-production-probe` | PASS |
| `isolated-resource-load` | PASS |
| `performance-regression-gate` | PASS |

The aggregate regression gate accepted the established baseline contract: PostgreSQL SMALL/MEDIUM/LARGE probes, frontend deterministic journeys, burst concurrency `1/5/10/20`, and a 25-minute sustained concurrency-5 workload.

## Isolated workload results

Environment authority: isolated Ubuntu runner with production-like Docker Compose, PostgreSQL, and Redis. Target: `http://perf.example.test:8088`. Twenty distinct users and entry IDs were provisioned through the real first-run and JWT paths.

| Mode | Concurrency | Requests | Errors / 5xx / transport | p50 | p95 | Throughput |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Burst | 1 | 12 | 0 / 0 / 0 | 15.812 ms | 183.886 ms | 23.721 rps |
| Burst | 5 | 60 | 0 / 0 / 0 | 45.140 ms | 221.199 ms | 63.359 rps |
| Burst | 10 | 120 | 0 / 0 / 0 | 120.726 ms | 316.098 ms | 68.106 rps |
| Burst | 20 | 240 | 0 / 0 / 0 | 196.676 ms | 543.808 ms | 52.713 rps |
| Sustained | 5 | 19,845 | 0 / 0 / 0 | 29.059 ms | 57.723 ms | 13.227 rps |

The sustained run lasted `1500.321` seconds. No load or sampler hard failure occurred.

Resource summary:

| Resource | Observation |
| --- | --- |
| API | 242,745,344-byte peak memory; +32,400,999 bytes; 161.42% peak sampled CPU |
| Web | 6,046,089-byte peak memory; 4.38% peak sampled CPU |
| PostgreSQL | 75,151,441-byte peak memory; 81.73% peak sampled CPU |
| PostgreSQL connections | 14 peak of 100 configured |
| Redis container | 9,806,282-byte peak memory; 3.86% peak sampled CPU |
| Redis dataset | 1,410,976-byte peak; 31 peak keys |

## PostgreSQL dataset probes

The backend job used authoritative PostgreSQL with `EXPLAIN ANALYZE BUFFERS` support:

| Dataset | Journal entries | Watch history | Plugins | Supporting users | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| SMALL | 50 | 25 | 5 | 10 | PASS |
| MEDIUM | 1,000 | 500 | 20 | 50 | PASS |
| LARGE | 10,000 | 5,000 | 50 | 100 | PASS |

All 16 LARGE-path probes returned HTTP 200. Query counts remained bounded, including journal list/detail/filter/sort, page 48, watch history, plugin surfaces, staff surfaces, and Integration surfaces.

## Capacity interpretation

For the measured normal-read workload only:

- Comfortable observed point: sustained concurrency 5, with zero errors and 57.723 ms p95.
- Degraded but usable observed point: burst concurrency 20, with zero errors and 543.808 ms p95.
- Saturation point: **NOT ESTABLISHED**. Throughput peaked at concurrency 10 and declined at 20, but no 40/60/80/100-user steps or long operations were run.

These observations must not be relabeled as the required capacity matrix. They say nothing reliable about synchronous external-provider latency, worker/thread starvation, or normal API behavior while 2/4/8 long operations are active.

## Missing evidence

- `20/40/60 x 0/2/4/8` normal-user/long-operation matrix.
- Stubbed provider latency and long-operation latency distribution.
- Normal API p99 and 429 counts under that matrix.
- Gunicorn worker saturation or queue-depth evidence.
- Isolation comparison showing whether four long operations materially degrade normal APIs.

## Decision

Concurrency Probe: **FAIL** against the Final RC requirement; the required matrix is incomplete.

Comfortable capacity: **observed only for the existing normal workload at concurrency 5**.

Degraded point: **observed at normal burst concurrency 20**.

Saturation point: **NOT ESTABLISHED**.
Job Queue: **INCONCLUSIVE**.

Before Final RC, implement the bounded minimum matrix with a deterministic slow-provider stub. The result should inform architecture; this report does not authorize an immediate Celery, RabbitMQ, Kafka, or Runtime v3 expansion.
