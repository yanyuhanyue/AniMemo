# AniMemo v1.0 Backend / Database Performance Evidence — Wave 1

Date: 2026-08-12

Branch: `perf/backend-db-baseline`

Measurement contract: `docs/performance-benchmark-contract-v1.0.md`

This document contains Wave 1 measurement evidence only. No product logic, migration, workflow, release, updater, API, Auth, Plugin SDK or Integration Protocol contract was changed.

## Environment and authority

| Item | Result |
| --- | --- |
| Local OS | Windows |
| Local database | SQLite |
| Docker | Unavailable |
| Local PostgreSQL | Unavailable |
| Authoritative Ubuntu + PostgreSQL + Redis API latency | **NOT RUN** |
| PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` | **NOT RUN** |
| Local SQLite use | Query shape, duplicate-query, payload and seed/probe validation only |

SQLite timings are deliberately omitted from the JSON summary (`NOT AUTHORITATIVE — SQLite query-shape mode`). The local observations below must not be represented as PostgreSQL latency or production capacity.

## Harness delivered

- `performance/seed.py` deterministically generates the shared SMALL / MEDIUM / LARGE shapes without a large fixture file.
- `benchmark_backend_performance` defaults to PostgreSQL-only and refuses SQLite unless `--allow-sqlite-query-shape` is explicitly supplied.
- Every endpoint receives 2 warm-up requests and 10 measured requests.
- Every measured result records status code, query count, normalized duplicate-query executions, response bytes and item count.
- `--explain` is PostgreSQL-only and captures up to three slowest unique read queries per selected hot path using `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`.
- Any unexpected non-200 response aborts the harness instead of producing a misleading report.
- Generated data is isolated under the `perf-v1-*` namespace, and reset deletes only that namespace.

Ubuntu/PostgreSQL invocation after the branch is available in an isolated runner:

```bash
cd backend
python manage.py migrate --noinput
python manage.py benchmark_backend_performance \
  --dataset small \
  --explain \
  --output ../artifacts/backend-small-postgresql.json
python manage.py benchmark_backend_performance \
  --dataset medium \
  --explain \
  --output ../artifacts/backend-medium-postgresql.json
python manage.py benchmark_backend_performance \
  --dataset large \
  --explain \
  --output ../artifacts/backend-large-postgresql.json
```

Required environment is the existing isolated CI contract: Ubuntu, PostgreSQL, Redis, DEBUG-safe test configuration and no production credentials.

## Generated dataset shape

| Dataset | Journal entries | Supporting users | Plugins | Watch History | Integration events |
| --- | ---: | ---: | ---: | ---: | ---: |
| SMALL | 50 | 10 | 5 | 25 | 50 |
| MEDIUM | 1,000 | 50 | 20 | 500 | 1,000 |
| LARGE | 10,000 | 100 | 50 | 5,000 | 1,000 |

The owner dataset includes bounded tags, 27 years, all watch statuses, scores/null scores, descriptions, external identities with metadata summaries and a 500-record Watch History target at MEDIUM/LARGE. Supporting users include profiles/settings/staff roles. Plugin data includes immutable versions, deployments, approved submissions and bounded installations. Integration data includes two connections, user bindings and queued events.

## Local query-shape evidence

All listed values are the median over 10 measured requests after 2 warm-ups. Status was HTTP 200 for every run.

### Journal, Dashboard and Watch History

| Probe | SMALL queries / bytes / items | MEDIUM queries / bytes / items | LARGE queries / bytes / items |
| --- | --- | --- | --- |
| Journal page 1 | 6 / 45,436 / 48 | 6 / 45,774 / 48 | 6 / 47,170 / 48 |
| Journal middle page | 6 / 45,436 / 48 | 6 / 45,759 / 48 | 6 / 45,870 / 48 |
| Journal page 48 | N/A | N/A | 6 / 46,391 / 48 |
| Journal status filter | 6 / 9,053 / 10 | 6 / 44,009 / 48 | 6 / 44,135 / 48 |
| Journal score sort | 6 / 45,467 / 48 | 6 / 46,449 / 48 | 6 / 46,728 / 48 |
| Journal facets | 7 / 45,744 / 48 | 7 / 46,082 / 48 | 7 / 47,478 / 48 |
| Watch History page 1 | 5 / 8,230 / 25 | 6 / 33,364 / 100 | 6 / 33,415 / 100 |
| Watch History page 5 | 6 / 67 / 0 | 6 / 32,886 / 100 | 6 / 32,935 / 100 |

Query-count interpretation:

- Journal pagination, filter, sort and LARGE page 48 stay bounded at six queries in SQLite query-shape mode.
- Facets stay at seven queries, but implementation inspection confirms facets iterate every owner entry's `tags` and `airing_period`. PostgreSQL latency/rows scanned are required before assigning a final performance severity.
- Watch History pagination remains bounded at five/six queries and 100 serialized records per page.

### Auth/session, Staff and Integration diagnostics

| Probe | SMALL | MEDIUM | LARGE |
| --- | --- | --- | --- |
| Auth `/auth/me/` queries / bytes | 8 / 600 | 8 / 600 | 8 / 600 |
| Staff Dashboard queries / bytes / users | 19 / 7,704 / 12 | 19 / 22,842 / 52 | 19 / 41,486 / 100 |
| Integration connections queries / bytes | 4 / 275 | 4 / 275 | 4 / 275 |
| Integration bindings queries / bytes / returned bindings | 4 / 391 / 1 | 4 / 391 / 1 | 4 / 391 / 1 |
| Integration events queries / bytes / returned events | 3 / 14,172 / 50 | 3 / 14,270 / 50 | 3 / 14,371 / 50 |

Interpretation:

- Auth/session, Staff Dashboard and Integration diagnostics have bounded query counts across generated dataset growth.
- Each HMAC Integration events request still executes the frozen authentication path that updates `IntegrationConnection.last_seen_at`; sustained write amplification remains a measurement candidate for the resource/load workstream, not a finding from this local read probe.
- Login password hashing / Turnstile wall-clock latency is not represented by `/auth/me/`; authoritative login/session timing remains **NOT RUN** until the isolated PostgreSQL/Redis run.

### Plugin Platform

| Probe | SMALL (5 plugins) | MEDIUM (20 plugins) | LARGE (50 plugins) |
| --- | ---: | ---: | ---: |
| Marketplace queries | 16 | 61 | 151 |
| Marketplace duplicate executions | 12 | 57 | 147 |
| Marketplace bytes | 2,799 | 11,173 | 27,913 |
| Installed plugin list queries | 4 | 4 | 4 |
| Installed plugin list bytes | 2,498 | 9,965 | 24,895 |
| Staff Plugin Review queries | 14 | 29 | 59 |
| Staff Plugin Review duplicate executions | 4 | 19 | 49 |
| Staff Plugin Review bytes | 2,524 | 9,882 | 24,582 |

Installed plugin list remains bounded at four queries. Marketplace and Staff Plugin Review do not.

## Proposed findings

### PERF-BE-PROPOSED-001

| Field | Evidence |
| --- | --- |
| Proposed Severity | **PERF1 (PROPOSED)** |
| Area | Plugin Platform backend |
| Journey | Public Plugin Marketplace initial load |
| Dataset | SMALL / MEDIUM / LARGE, 5 / 20 / 50 plugins |
| Evidence | Queries scale 16 → 61 → 151; duplicate executions scale 12 → 57 → 147 |
| Before | SQLite query-shape only; PostgreSQL latency **NOT RUN** |
| Root Cause | `MarketplaceView` iterates projects; `serialize_marketplace_project` separately loads owner, published versions and installation count for each project. Captured normalized shapes each execute N times. |
| Suggested Fix | Preserve API v1 payload; batch/select/prefetch owner and published versions and annotate installation count. Verify with the same SMALL/MEDIUM/LARGE probe. |
| Contract Risk | Low if response fields/order/visibility semantics remain unchanged; API v1 is frozen. |
| Owner | Backend / Plugin Platform |

Why proposed PERF1: this is a deterministic, high-frequency list N+1 with approximately `3N + 1` queries. Final severity must be assigned at the finding barrier after PostgreSQL timing and current expected marketplace scale are considered.

### PERF-BE-PROPOSED-002

| Field | Evidence |
| --- | --- |
| Proposed Severity | **PERF1 (PROPOSED)** |
| Area | Staff / Plugin Platform backend |
| Journey | Staff Plugin Review initial load |
| Dataset | SMALL / MEDIUM / LARGE, 5 / 20 / 50 marketplace versions |
| Evidence | Queries scale 14 → 29 → 59; duplicate executions scale 4 → 19 → 49 |
| Before | SQLite query-shape only; PostgreSQL latency **NOT RUN** |
| Root Cause | `StaffPluginReviewQueueView` calls `version.plugin.user_installations.count()` once per marketplace version. Captured COUNT shape executes N times. |
| Suggested Fix | Preserve the Staff response contract and annotate/batch installation counts; verify with the same probe. |
| Contract Risk | Low if Staff payload semantics are unchanged. |
| Owner | Backend / Staff + Plugin Platform |

Why proposed PERF1: this is deterministic N+1 growth on a polling-capable Staff page. Final severity must account for frontend polling evidence and PostgreSQL results at the barrier.

## Candidate risks requiring PostgreSQL or load evidence

These are not findings yet:

- Dashboard facets performs an owner-wide values scan even though query count is constant. Need PostgreSQL latency, rows and buffers at 1k/10k.
- Offset pagination including page 48 has constant query count locally. Need PostgreSQL `EXPLAIN ANALYZE BUFFERS` to characterize offset/count cost.
- Integration HMAC polling updates `last_seen_at` on every authenticated request. Need sustained/concurrent PostgreSQL write and resource evidence.
- Staff Dashboard response grows to 41,486 bytes for 100 users but query count is constant. Need browser/polling evidence before severity.
- Plugin lists are intentionally unpaginated under the current API. The measured N+1 must be addressed without silently changing the frozen API v1 contract.

## Tests and local commands

```powershell
$env:DEBUG='true'
$env:TURNSTILE_ENABLED='false'
python manage.py test journal.test_performance_baseline -v 2
```

Result: **7 passed**. Coverage includes shared dataset contract, red-capable query scaling helper, duplicate SQL normalization, SQLite refusal, non-200 rejection contract, exact/repeatable SMALL seed and Journal list query-count scaling.

```powershell
python manage.py benchmark_backend_performance --dataset small  --allow-sqlite-query-shape --output $env:TEMP/animemo-backend-small-query-shape.json
python manage.py benchmark_backend_performance --dataset medium --allow-sqlite-query-shape --output $env:TEMP/animemo-backend-medium-query-shape.json
python manage.py benchmark_backend_performance --dataset large  --allow-sqlite-query-shape --output $env:TEMP/animemo-backend-large-query-shape.json
```

Result: all 16 probes returned HTTP 200; LARGE included Dashboard page 48. Outputs stayed outside the repository and are auxiliary only.

## Limitations

```text
POSTGRESQL AUTHORITATIVE LATENCY: NOT RUN
POSTGRESQL EXPLAIN ANALYZE BUFFERS: NOT RUN
REDIS-AUTHORITY RESULT: NOT RUN
CONCURRENCY 1/5/10/20: NOT RUN BY THIS WORKSTREAM
SUSTAINED 25 MINUTES: NOT RUN BY THIS WORKSTREAM
PRODUCTION PERFORMANCE TEST: NOT RUN
PRODUCTION DATABASE: NOT RUN
SSH: NOT RUN
```
