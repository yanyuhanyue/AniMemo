# AniMemo v1.0 Performance Hardening Report

Date: 2026-08-12

## Identity and authority

| Item | Value |
| --- | --- |
| Historical phase base | `179688c3478555f52ee66c78bfec103e1766047d` |
| Baseline/hardening merge | `ca7a3e5c3be40190911eed49f508a3922d1a560d` — PR #68 |
| Virtual-user correction merge | `b2fc5c00774aee07e6fc61a055afc46b17b062e9` — PR #69 |
| Final authoritative performance run | [#31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595) — PASS |
| Final main CI | [#31610514044](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610514044) — PASS |
| Final main Release Gate | [#31610514585](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610514585) — PASS |

## Delivered hardening

- Added deterministic SMALL/MEDIUM/LARGE seed and measurement contracts.
- Added production-build Chromium, PostgreSQL and disposable resource/load probes.
- Removed Marketplace and Staff Plugin Review N+1 query growth without changing API v1.
- Serialized Staff Update Operation polling and suppressed hidden-tab work.
- Added a deterministic performance regression gate and RC-only Release Producer integration.
- Corrected the load harness so nominal virtual users are distinct authenticated identities with distinct owned data.
- Kept production throttles, Nginx/OpenResty behavior, auth tokens and frozen contracts unchanged.

## PERF1 before / after

| Finding | Before | Fix | After | Conclusion |
| --- | --- | --- | --- | --- |
| PERF-BE-001 Marketplace N+1 | 16 / 61 / 151 queries at 5 / 20 / 50 plugins | select/prefetch projects, versions, owner and user installations | 2 / 2 / 2 queries; LARGE p95 18.4 ms | PROVEN |
| PERF-BE-002 Staff Review N+1 | 14 / 29 / 59 queries | annotate installation counts and prefetch submissions once | 8 / 8 / 8 queries; LARGE p95 23.8 ms | PROVEN |
| PERF-FE-003 Update polling | 3-second response allowed 2 in flight; hidden interval issued 1 request | shared serialized live-refresh controller | maximum in-flight 1; hidden interval requests 0 | PROVEN |

## Authoritative load correction

Run [#31602499414](https://github.com/yanyuhanyue/AniMemo/actions/runs/31602499414) failed because all virtual workers shared one owner JWT. The default authenticated-user throttle correctly grouped them under one user primary key, producing 28 HTTP 429 responses in the 20-user stage and 9,042 in the sustained stage.

PR #69 created 20 isolated users, each with its own entry and access token. Run `31610568595` proved 20 unique usernames, 20 unique entry IDs and zero 429 responses without changing production throttles. This closes the measurement defect rather than weakening the gate.

## Finding matrix

```text
PERF0 FOUND: 0
PERF0 FIXED: 0
PERF0 OPEN: 0

PERF1 FOUND: 3
PERF1 FIXED: 3
PERF1 OPEN: 0

PERF2 DEFERRED: 4
PERF3 DEFERRED: 0
```

PERF2 is intentionally deferred: common JavaScript chunk, generated fonts, Dashboard facets scan and Integration authentication-observation writes. None produced a v1.0 correctness failure, resource runaway or small-scale stability failure. Each v1.1 item requires the same benchmark before/after and preserved contracts.

## Final correctness matrix

| Area | Result | Evidence |
| --- | --- | --- |
| Frontend | PASS | PR Fast and Pre-Merge Full for exact PR #69 head; main lightweight CI PASS |
| Critical E2E | PASS | Pre-Merge Full run `31609754665` |
| Backend | PASS | Pre-Merge Full run `31609754665`; PostgreSQL probe PASS |
| PostgreSQL | PASS | SMALL/MEDIUM/LARGE probe and EXPLAIN evidence |
| Plugin | PASS | N+1 contract tests and full plugin gate |
| Bridge | PASS | Pre-Merge Full run `31609754665` |
| Fresh Docker | PASS | PR #69 Release Gate |
| Stateful Upgrade | PASS | PR #69 Release Gate |
| Performance regression | PASS | Run `31610568595` aggregate gate |

## Architecture matrix

```text
API V1: PASS
AUTH: PASS
RESOURCE IDENTITY: PASS
PLUGIN SDK V2: PASS
INTEGRATION PROTOCOL V1: PASS
DA-TD1-001: UNCHANGED
DA-TD1-004: UNCHANGED
```

## Limitations

- Capacity beyond 20 concurrent users is outside the v1.0 target and was not claimed.
- Browser field telemetry, external Turnstile/Bangumi/R2 latency and production-network behavior were not measured.
- Sustained Integration long-poll/write behavior needs its own v1.1 workload before changing `last_seen_at` semantics.
- GitHub Actions emitted Node 20 action-runtime deprecation notices; action-major upgrades remain `V11-OPS-002` and did not invalidate this run.

## Production impact

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION UPDATE: NOT RUN
PRODUCTION PERFORMANCE TEST: NOT RUN
PRODUCTION DATABASE: NOT RUN
R2 PRODUCTION WRITE: NOT RUN
SSH: NOT RUN
PRODUCTION: UNCHANGED
```

## RC readiness

```text
PERF0 OPEN: 0
PERF1 OPEN: 0
PERFORMANCE REGRESSION GATE: PASS
CORRECTNESS REGRESSION: PASS
PRODUCTION: UNCHANGED
NEXT: Final RC
```
