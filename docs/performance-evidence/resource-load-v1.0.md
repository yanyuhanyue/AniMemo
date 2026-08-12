# AniMemo v1.0 Resource / Load Evidence

Date: 2026-08-12

Candidate: `b2fc5c00774aee07e6fc61a055afc46b17b062e9`

Workflow run: [Performance Baseline #31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595)

Job: `isolated-resource-load` — PASS

## Authority and isolation

The workload ran on a disposable Ubuntu Compose project with the candidate API/Web images, PostgreSQL and Redis. The target was the isolated local host `perf.example.test:8088`; the harness rejects the production domain, production IP and production Compose/container names.

The seed contained 10,000 journal entries and 20 virtual users. The artifact reported 20 unique usernames and 20 unique entry IDs. Access tokens existed only in `$RUNNER_TEMP`; uploaded `seed.json` contains only dataset, journal-entry count and virtual-user count.

## Concurrency and sustained results

| Mode | Users | Requests | Errors | HTTP 5xx | p50 | p95 | Throughput |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Burst | 1 | 12 | 0 | 0 | 20.6 ms | 99.3 ms | 28.7 req/s |
| Burst | 5 | 60 | 0 | 0 | 42.4 ms | 155.3 ms | 80.0 req/s |
| Burst | 10 | 120 | 0 | 0 | 96.8 ms | 263.3 ms | 82.1 req/s |
| Burst | 20 | 240 | 0 | 0 | 197.0 ms | 424.3 ms | 87.9 req/s |
| Sustained | 5 | 20,039 | 0 | 0 | 24.8 ms | 50.9 ms | 13.4 req/s |

The sustained workload ran for `1,500.368 s`; total workload elapsed time including burst stages was `1,509.68 s`. There were no transport errors, unexpected statuses or hard failures.

### Sustained journey latency

| Journey | Requests | p50 | p95 | Errors |
| --- | ---: | ---: | ---: | ---: |
| Dashboard with facets | 3,339 | 29.3 ms | 40.4 ms | 0 |
| Filter/search | 3,339 | 27.4 ms | 36.3 ms | 0 |
| Entry detail | 3,340 | 12.5 ms | 16.1 ms | 0 |
| Watch History | 3,340 | 8.5 ms | 11.6 ms | 0 |
| Enabled plugins | 3,341 | 12.5 ms | 18.2 ms | 0 |
| Staff health | 3,340 | 46.3 ms | 62.5 ms | 0 |

## Resource evidence

| Resource | Start | End | Peak / maximum |
| --- | ---: | ---: | ---: |
| API memory | 194.9 MiB | 215.5 MiB | 219.8 MiB |
| API memory growth | — | +20.6 MiB | below 512 MiB runaway sentinel |
| API CPU | — | — | 169.99% |
| Web memory | 5.6 MiB | 5.1 MiB | 5.8 MiB |
| PostgreSQL memory | 64.9 MiB | 70.0 MiB | 70.6 MiB |
| PostgreSQL CPU | — | — | 43.29% |
| PostgreSQL connections | 14 | 14 | 14 / 100 |
| Redis application memory | 1,379.6 KiB | 1,295.4 KiB | 1,379.6 KiB |
| Redis keys | 21 | 6 | 27 |

There were 87 complete resource samples and no sampling error. API memory did not grow monotonically into the configured runaway boundary; PostgreSQL connections remained at 14% of the configured maximum; Redis memory and key count ended below their starting values.

## Harness correction evidence

The first authoritative run, [#31602499414](https://github.com/yanyuhanyue/AniMemo/actions/runs/31602499414), reused one authenticated owner identity across nominal workers. DRF correctly throttled that shared user at `300/min`, producing exactly 28 HTTP 429 responses in the 20-user stage and 9,042 in the sustained stage. This was a virtual-user modeling defect, not evidence that production limits should be raised.

PR #69 provisioned distinct user identities, data ownership and JWTs. On the exact merged main candidate:

| Check | Before | After |
| --- | ---: | ---: |
| Provided virtual users | 1 shared identity | 20 |
| Unique usernames | 1 | 20 |
| Unique entry IDs | 1 shared owner entry | 20 |
| 20-user HTTP 429 | 28 | 0 |
| Sustained HTTP 429 | 9,042 | 0 |
| Production throttle change | — | none |

## Final finding decision

No resource/load PERF0 or PERF1 remains open. The aggregate regression gate accepted all four concurrency levels, the 25-minute sustained run, complete resource sampling and distinct virtual-user identities.

## Production boundary

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION UPDATE: NOT RUN
PRODUCTION PERFORMANCE TEST: NOT RUN
PRODUCTION DATABASE: NOT RUN
R2 PRODUCTION WRITE: NOT RUN
SSH: NOT RUN
```
