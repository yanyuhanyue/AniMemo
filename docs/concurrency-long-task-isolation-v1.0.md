# AniMemo v1.0 Concurrency and Long-Task Isolation

Status: **PASS WITH A MEASURED DEGRADED BOUNDARY**

Application candidate: `085657249c5d174e17dac7ff6dc0797f3179165a`

Performance workflow: [GitHub Actions run 31745479369](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745479369)

Completed: 2026-08-14, Asia/Shanghai

## Test contract

The final candidate completed the required minimum matrix against a disposable Compose environment:

```text
normal API users: 20 / 40 / 60
concurrent long operations: 0 / 2 / 4 / 8
```

The long operation used a fake Bangumi provider endpoint with external networking disabled and deterministic 1,200 ms provider latency. Gunicorn ran two workers with four threads each, for eight configured synchronous worker-thread slots. Every cell used barrier-synchronized clients, four normal iterations per user, and an uncounted normal-only warm-up immediately before measurement.

The runner sampled API/Web/PostgreSQL/Redis resources during every active matrix cell. The test never accessed production or the real Bangumi service.

## Final matrix

| Users | Long ops | Requests | Normal p50 | Normal p95 | Normal p99 | Normal throughput | Long-op p95 | Errors |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 0 | 320 | 120.198 ms | 325.316 ms | 415.175 ms | 124.541 rps | - | 0 |
| 20 | 2 | 322 | 122.675 ms | 288.490 ms | 338.044 ms | 127.970 rps | 1492.752 ms | 0 |
| 20 | 4 | 324 | 119.769 ms | 372.972 ms | 427.745 ms | 119.413 rps | 1498.636 ms | 0 |
| 20 | 8 | 328 | 142.142 ms | 1173.350 ms | 1375.156 ms | 87.827 rps | 2560.869 ms | 0 |
| 40 | 0 | 640 | 277.027 ms | 609.984 ms | 766.456 ms | 125.713 rps | - | 0 |
| 40 | 2 | 642 | 299.369 ms | 537.851 ms | 619.644 ms | 125.159 rps | 1681.120 ms | 0 |
| 40 | 4 | 644 | 240.489 ms | 778.612 ms | 861.171 ms | 119.412 rps | 1708.470 ms | 0 |
| 40 | 8 | 648 | 259.611 ms | 1197.285 ms | 1469.530 ms | 102.916 rps | 1874.409 ms | 0 |
| 60 | 0 | 960 | 424.197 ms | 848.891 ms | 967.935 ms | 124.986 rps | - | 0 |
| 60 | 2 | 962 | 376.435 ms | 869.045 ms | 1081.648 ms | 126.296 rps | 1522.179 ms | 0 |
| 60 | 4 | 964 | 375.643 ms | 1451.866 ms | 1697.984 ms | 116.951 rps | 2223.931 ms | 0 |
| 60 | 8 | 968 | 303.803 ms | 1549.515 ms | 1811.623 ms | 111.750 rps | 2977.043 ms | 0 |

Across all 12 cells, 7,722 requests returned HTTP 200. Totals were 0 timeouts, 0 HTTP 5xx, 0 HTTP 429, and 0 transport errors. The configured per-identity `300/min` budget was not exceeded.

## Degradation against each user baseline

| Users | Long ops | p95 change | p99 change | Throughput change | Interpretation |
| ---: | ---: | ---: | ---: | ---: | --- |
| 20 | 4 | +14.7% | +3.0% | -4.1% | Comfortable |
| 20 | 8 | +260.7% | +231.2% | -29.5% | Clear synchronous saturation |
| 40 | 4 | +27.6% | +12.4% | -5.0% | Degraded but usable |
| 40 | 8 | +96.3% | +91.7% | -18.1% | Clear synchronous saturation |
| 60 | 4 | +71.0% | +75.4% | -6.4% | Edge degraded, no hard starvation |
| 60 | 8 | +82.5% | +87.2% | -10.6% | Saturated/degraded boundary |

Two long operations did not materially harm throughput or error rate. Four long operations caused no hard failures and retained at least 93.6% of baseline throughput, although tail latency at 60 users rose noticeably. Eight operations equaled the configured worker-thread capacity and caused consistent tail-latency degradation.

## Resource evidence

| Resource | Maximum observed |
| --- | ---: |
| API sampled CPU | 224.02% |
| API memory | 226.7 MiB |
| PostgreSQL connections | 14 of 100 |
| Redis dataset memory | 1.46 MiB |
| Matrix cells with active sampling | 12 of 12 |

No PostgreSQL connection exhaustion, Redis growth failure, container crash, or sampler hard failure occurred. The API CPU samples show real work during active windows. Direct Gunicorn queue-depth telemetry was unavailable, so the saturation interpretation uses configured `2 x 4` capacity, synchronized occupancy, measured latency/throughput deltas, errors, and active resource samples.

The aggregate performance regression artifact also passed frontend production journeys, PostgreSQL SMALL/MEDIUM/LARGE probes, established `1/5/10/20` burst levels, and the 1,500-second sustained workload.

## Capacity decision

Comfortable capacity: **0-4 synchronous long operations in the tested envelope**. At 60 normal users, four operations are already an edge-degraded state, but they did not produce severe starvation, errors, or large throughput collapse.

Degraded but usable: **8 synchronous long operations**, with zero hard failures but pronounced p95/p99 inflation.

Saturation point: **8 synchronous long operations**, equal to all configured worker-thread slots. This is a degraded boundary, not an authorization to run more synchronous provider work.

Concurrency Probe: **PASS** for the requested non-production evidence matrix.

Job Queue decision: **DEFER TO v1.1**. The v1.0 evidence does not justify a last-minute Celery, RabbitMQ, Kafka, or worker-architecture expansion. v1.1 should introduce the Background Job boundary before raising synchronous long-operation concurrency or adding long-running Integration actions.
