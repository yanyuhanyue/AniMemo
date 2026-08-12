# AniMemo v1.0 Performance Baseline

Date: 2026-08-12

Historical phase base: `179688c3478555f52ee66c78bfec103e1766047d`

Performance implementation main: `ca7a3e5c3be40190911eed49f508a3922d1a560d`

Final measured main: `b2fc5c00774aee07e6fc61a055afc46b17b062e9`

Authoritative run: [Performance Baseline #31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595) — PASS

## Scope

This baseline covers the v1.0 personal/small-scale target: production frontend build behavior, critical browser request topology, PostgreSQL SMALL/MEDIUM/LARGE API paths, 1/5/10/20-user isolated load, a 25-minute sustained workload and API/Web/PostgreSQL/Redis resources.

It is not a maximum-capacity claim and it does not represent production hardware, production data or public-network latency.

## Shared contract

- SMALL / MEDIUM / LARGE: 50 / 1,000 / 10,000 journal entries.
- Backend: 2 warm-ups + 10 measured requests, PostgreSQL 16, median and nearest-rank p95, selected `EXPLAIN (ANALYZE, BUFFERS)`.
- Frontend: production Vite preview, Chromium, one warm-up + five measured runs, fresh contexts and disabled cache.
- Load: 1/5/10/20 distinct virtual users, then five users sustained for 1,500 seconds.
- Gate: deterministic correctness/resource conditions only; no arbitrary percentage latency threshold.

## Final performance matrix

| Gate | Result | Evidence |
| --- | --- | --- |
| Frontend baseline | PASS | Five critical journeys; stable API topology; production bundle inventory |
| API baseline | PASS | All SMALL/MEDIUM/LARGE probes returned HTTP 200 |
| PostgreSQL baseline | PASS | 2 warm-ups + 10 measured runs and selected EXPLAIN evidence |
| Request topology | PASS | No unexplained critical initial duplicate request |
| N+1 audit | PASS | Marketplace 2 queries; Staff review 8 queries across 5/20/50 plugins |
| Pagination scaling | PASS | Journal query count 6 through LARGE page 48; Watch History bounded at 5/6 |
| Resource baseline | PASS | 87 complete samples; no hard failure or sampling error |
| Sustained run | PASS | 20,039 requests over 1,500.368 s; zero errors |
| 1 user | PASS | 12 requests; p95 99.3 ms; zero errors |
| 5 users | PASS | 60 requests; p95 155.3 ms; zero errors |
| 10 users | PASS | 120 requests; p95 263.3 ms; zero errors |
| 20 users | PASS | 240 requests; p95 424.3 ms; zero errors |

## Representative baseline numbers

### PostgreSQL LARGE

| Path | Median | p95 | Queries |
| --- | ---: | ---: | ---: |
| Journal page 1 | 128.2 ms | 132.6 ms | 6 |
| Journal page 48 | 133.2 ms | 199.2 ms | 6 |
| Journal facets | 175.8 ms | 232.9 ms | 7 |
| Plugin marketplace | 16.0 ms | 18.4 ms | 2 |
| Staff plugin review | 22.7 ms | 23.8 ms | 8 |
| Staff dashboard | 198.4 ms | 201.3 ms | 19 |
| Integration events | 5.1 ms | 5.3 ms | 3 |

### Sustained isolated workload

- Requests: `20,039`; errors/5xx/transport errors: `0/0/0`.
- Overall p50/p95: `24.8 / 50.9 ms`.
- API memory: `194.9 → 215.5 MiB`, peak `219.8 MiB`.
- PostgreSQL connections: peak `14 / 100`.
- Redis application memory: `1,379.6 → 1,295.4 KiB`; keys `21 → 6`.
- Virtual identities: `20` provided, `20` unique usernames, `20` unique entry IDs.

## Finding barrier

| Severity | Found | Fixed | Open / deferred |
| --- | ---: | ---: | ---: |
| PERF0 | 0 | 0 | 0 |
| PERF1 | 3 | 3 | 0 |
| PERF2 | 4 | 0 | 4 deferred |
| PERF3 | 0 | 0 | 0 |

Fixed PERF1:

- `PERF-BE-001`: Plugin Marketplace N+1, `16/61/151 → 2/2/2` queries.
- `PERF-BE-002`: Staff Plugin Review N+1, `14/29/59 → 8/8/8` queries.
- `PERF-FE-003`: Update Operation overlap/hidden polling, maximum in-flight `2 → 1`, hidden interval requests `1 → 0`.

Deferred PERF2:

- `PERF-FE-001`: common JavaScript chunk.
- `PERF-FE-002`: generated font asset inventory.
- `PERF-DB-001`: Dashboard facet owner-wide scan.
- `PERF-API-001`: Integration `last_seen_at` write amplification requires dedicated sustained poll evidence.

## Correctness and contract status

```text
PERFORMANCE REGRESSION GATE: PASS
CORRECTNESS REGRESSION: PASS
API V1: PASS / UNCHANGED
AUTH: PASS / UNCHANGED
RESOURCE IDENTITY: PASS / UNCHANGED
PLUGIN SDK V2: PASS / UNCHANGED
INTEGRATION PROTOCOL V1: PASS / UNCHANGED
DA-TD1-001: UNCHANGED
DA-TD1-004: UNCHANGED
```

## Evidence index

- `docs/performance-evidence/frontend-v1.0.md`
- `docs/performance-evidence/backend-db-v1.0.md`
- `docs/performance-evidence/resource-load-v1.0.md`
- `docs/performance-hardening-20260812.md`

## Final status

```text
PERFORMANCE BASELINE: PASS
PERFORMANCE HARDENING: PASS
PERF0 OPEN: 0
PERF1 OPEN: 0
PERFORMANCE REGRESSION GATE: PASS
V1.0 PERFORMANCE BLOCKERS: 0
PRODUCTION: UNCHANGED
NEXT: Final RC
```
