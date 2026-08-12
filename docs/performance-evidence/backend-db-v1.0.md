# AniMemo v1.0 Backend / PostgreSQL Performance Evidence

Date: 2026-08-12

Candidate: `b2fc5c00774aee07e6fc61a055afc46b17b062e9`

Workflow run: [Performance Baseline #31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595)

Job: `backend-postgresql-probe` — PASS

## Authority and method

The final probe ran on `ubuntu-latest` with PostgreSQL 16 and Redis 7. Each endpoint received two warm-up requests and ten measured requests. Reports record HTTP status, median/p95 latency, query count, normalized duplicate-query count, response bytes and item count. Selected LARGE hot paths include `EXPLAIN (ANALYZE, BUFFERS)`.

All measured requests returned HTTP 200. PostgreSQL mode was `POSTGRESQL_AUTHORITATIVE`; SQLite timings are not used below.

## Dataset contract

| Dataset | Journal entries | Supporting users | Plugins | Watch History | Integration events |
| --- | ---: | ---: | ---: | ---: | ---: |
| SMALL | 50 | 10 | 5 | 25 | 50 |
| MEDIUM | 1,000 | 50 | 20 | 500 | 1,000 |
| LARGE | 10,000 | 100 | 50 | 5,000 | 1,000 |

## Journal, Dashboard and Watch History

Each cell is `median / p95 ms · median queries`.

| Probe | SMALL | MEDIUM | LARGE |
| --- | ---: | ---: | ---: |
| Journal page 1 | 32.0 / 33.7 · 6 | 37.2 / 85.4 · 6 | 128.2 / 132.6 · 6 |
| Journal middle page | 32.1 / 81.8 · 6 | 38.3 / 43.4 · 6 | 142.0 / 156.1 · 6 |
| Journal page 48 | N/A | N/A | 133.2 / 199.2 · 6 |
| Status filter | 14.6 / 16.1 · 6 | 28.6 / 30.5 · 6 | 49.1 / 110.5 · 6 |
| Score sort | 31.8 / 39.1 · 6 | 40.9 / 109.7 · 6 | 128.4 / 129.7 · 6 |
| Journal facets | 33.9 / 35.3 · 7 | 45.7 / 47.2 · 7 | 175.8 / 232.9 · 7 |
| Journal detail | 11.4 / 11.6 · 6 | 12.0 / 14.2 · 6 | 11.4 / 12.0 · 6 |
| Watch History page 1 | 8.1 / 8.7 · 5 | 15.0 / 17.7 · 6 | 15.2 / 68.5 · 6 |
| Watch History deep page | 6.5 / 6.8 · 6 | 14.8 / 16.7 · 6 | 15.0 / 18.1 · 6 |

Pagination, filtering, sorting and Watch History remain query-count bounded across dataset growth. LARGE page 48 returns 48 records and does not introduce an N+1.

## Auth, Plugin, Staff and Integration

| Probe | SMALL | MEDIUM | LARGE |
| --- | ---: | ---: | ---: |
| Auth session | 11.1 / 12.4 · 8 | 12.8 / 13.9 · 8 | 20.1 / 22.0 · 8 |
| Plugin marketplace | 6.7 / 8.1 · 2 | 9.3 / 11.3 · 2 | 16.0 / 18.4 · 2 |
| Installed plugins | 6.4 / 6.7 · 4 | 7.7 / 9.4 · 4 | 11.0 / 16.8 · 4 |
| Staff dashboard | 21.7 / 22.8 · 19 | 28.1 / 29.5 · 19 | 198.4 / 201.3 · 19 |
| Staff plugin review | 13.2 / 14.8 · 8 | 16.4 / 82.2 · 8 | 22.7 / 23.8 · 8 |
| Integration connections | 4.8 / 4.9 · 4 | 4.8 / 6.3 · 4 | 4.8 / 4.9 · 4 |
| Integration bindings | 5.9 / 6.8 · 4 | 5.6 / 5.8 · 4 | 5.7 / 5.8 · 4 |
| Integration events | 5.4 / 5.7 · 3 | 5.4 / 6.3 · 3 | 5.1 / 5.3 · 3 |

Plugin marketplace and Staff plugin review now remain bounded at 2 and 8 queries respectively through 50 plugins.

## LARGE EXPLAIN highlights

| Probe / statement | Actual rows | Execution | Shared hits | Shared reads | Temp I/O |
| --- | ---: | ---: | ---: | ---: | ---: |
| Journal facets main page | 48 | 64.4 ms | 47,115 | 0 | 0 / 0 |
| Journal facets count | 1 | 40.4 ms | 46,923 | 0 | 0 / 0 |
| Facet tags/airing scan | 10,000 | 5.9 ms | 406 | 0 | 0 / 0 |
| Journal page 48 main page | 48 | 71.6 ms | 56,139 | 0 | 0 / 0 |
| Marketplace project aggregate | 50 | 1.8 ms | 464 | 0 | 0 / 0 |
| Staff review deployment aggregate | 50 | 1.8 ms | 364 | 0 | 0 / 0 |
| Integration events page | 50 | 0.04 ms | 5 | 0 | 0 / 0 |

The owner-wide facet scan is real but completes in 5.9 ms at 10,000 entries in this isolated run; total facet endpoint p95 is 232.9 ms. It is therefore PERF2, not a v1.0 blocker. The higher journal cost is concentrated in the annotated page/count query shape and remains bounded at the supported personal/small-scale dataset.

## PERF1 before / after

| Finding | Before query counts | After query counts | Result |
| --- | --- | --- | --- |
| PERF-BE-001 Marketplace N+1 | 16 / 61 / 151 | 2 / 2 / 2 | FIXED |
| PERF-BE-002 Staff Plugin Review N+1 | 14 / 29 / 59 | 8 / 8 / 8 | FIXED |

The fix uses selected/prefetched relations and annotated installation counts while preserving API v1 payload, ordering and visibility semantics.

## Final finding decisions

- `PERF-BE-001`: PERF1, FIXED in PR #68.
- `PERF-BE-002`: PERF1, FIXED in PR #68.
- `PERF-DB-001`: PERF2, DEFERRED — Dashboard facets still scan owner tags/airing data; optimize only with the same PostgreSQL probe and unchanged API v1 facets.
- `PERF-API-001`: PERF2, DEFERRED — Integration authentication still observes `last_seen_at`; a dedicated sustained Integration poll/write benchmark is required before changing Protocol v1 behavior.

## Limitations

- PostgreSQL is isolated CI, not production hardware or production data.
- Remote Turnstile, Bangumi, R2 and other external I/O were not benchmarked.
- The isolated mixed workload does not exercise sustained Integration long polling, so write-amplification remains a v1.1 measurement item.
- Production database, SSH, deploy and production performance testing: NOT RUN.
