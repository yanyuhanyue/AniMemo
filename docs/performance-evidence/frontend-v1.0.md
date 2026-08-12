# AniMemo v1.0 Frontend Performance Evidence

Date: 2026-08-12

Candidate: `b2fc5c00774aee07e6fc61a055afc46b17b062e9`

Workflow run: [Performance Baseline #31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595)

Job: `frontend-production-probe` — PASS

## Authority and method

The final probe ran on `ubuntu-latest` against the exact merged main candidate. It used Node `v20.20.2`, Chromium `151.0.7922.34`, a Vite production preview, a `1440×900` viewport, a fresh browser context for every run and disabled browser cache. Each normal journey used one warm-up and five measured runs; p95 is nearest-rank.

The browser API was deterministic Playwright routing. These results are authoritative for production-build asset inventory, request topology, duplicate-request classification, polling behavior and deterministic browser regressions. They are auxiliary for real network and PostgreSQL latency; those are covered separately by the backend and isolated-load jobs.

The previously committed `frontend-v1.0.json` is the Wave 1 local auxiliary capture. The authoritative raw JSON is the `performance-frontend` artifact from run `31610568595` and is retained by GitHub Actions for 14 days.

## Journey results

| Journey | Route ready median / p95 | LCP median / p95 | API requests | JS transfer / decoded | Interaction median / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Login | 304 / 327 ms | 828 / 840 ms | 4 | 249,411 / 740,399 B | mode transition 322.7 / 325.8 ms |
| Dashboard | 553 / 553 ms | 324 / 352 ms | 12 | 292,486 / 892,468 B | search 802.5 / 805.4 ms; status request 21.5 / 37.1 ms |
| Watch History | 552 / 597 ms | 348 / 372 ms | 15 | 292,486 / 892,468 B | initial 3,056.2 / 3,070.4 ms; page 160.3 / 171.5 ms; append 165.3 / 168.9 ms |
| Staff | 378 / 389 ms | 372 / 396 ms | 9 | 284,031 / 862,899 B | Plugin Center 473.2 / 485.4 ms |
| Plugin Platform | 262 / 279 ms | 232 / 256 ms | 8 | 247,131 / 736,069 B | installed tab 43.0 / 47.9 ms |

No journey had an unexplained exact duplicate initial API request. Login had no animation frame over 50 ms. Staff and Plugin Platform had no measured long task. Dashboard p95 CLS was `0.324808` in the deterministic mock and is retained as auxiliary evidence rather than treated as field CLS.

## Request topology and deep pagination

- Dashboard issued one page-1 entries request with facets, then one distinct request for search and one for status filtering.
- Watch History repeated stats and page-1 entries only after the explicit append mutation; the harness classified both as mutation-freshness work, not an initial request storm.
- Staff loaded dashboard/site settings once, then loaded plugin and review data when Plugin Center was selected.
- Plugin Platform loaded marketplace, installed plugins and user projects once each.
- The LARGE-shaped page-48 audit requested pages `1…48` exactly once each, included facets only on page 1, rendered 2,304 mock entries and completed in `38,652 ms`. It is topology evidence; PostgreSQL page-48 latency is recorded in `backend-db-v1.0.md`.

## Bundle inventory

| Asset group | Emitted bytes |
| --- | ---: |
| Reachable production files | 25,379,859 |
| JavaScript | 1,102,823 |
| CSS | 951,922 |
| Common `index` JavaScript chunk | 723,611 emitted / 242,687 transferred |
| Dashboard route chunk | 168,857 decoded |
| Staff route chunk | 139,288 decoded |
| Plugin Platform route chunk | 12,458 decoded |

The total inventory includes the broad generated Noto Sans SC asset matrix. It is not a claim that every route transfers every font.

## Polling regression evidence

Staff live refresh passed overlap coalescing, hidden-tab suppression and visible-return refresh at its configured `20,000 ms` interval.

The Staff Update Operation probe passed at its configured `2,500 ms` interval:

| Condition | Before | After |
| --- | ---: | ---: |
| 3-second response maximum in flight | 2 | 1 |
| Hidden tab requests after one interval | 1 | 0 |

The fix routes update-operation polling through the shared serialized live-refresh controller. It does not call a real Update Agent operation.

## Final finding decisions

### PERF-FE-003 — PERF1 — FIXED

Update Operation polling could overlap when a response exceeded the 2.5-second interval and continued while hidden. PR #68 serialized refreshes and suppressed hidden-tab polling. The same deterministic browser probe proves the before/after values above.

### PERF-FE-001 — PERF2 — DEFERRED

The common JavaScript chunk remains `723,611 B` emitted and `242,687 B` transferred. The production browser journeys passed and no v1.0 regression was proven, so route/dependency splitting is deferred to v1.1 with a same-probe before/after requirement.

### PERF-FE-002 — PERF2 — DEFERRED

The generated font inventory dominates emitted bytes, while route traces transfer only matched font files. Subsetting or packaging changes are deferred until Chinese rendering fidelity and actual route transfer can be proven together.

## Limitations

- Field INP, real-user field CLS and V8 parsed/heap attribution: NOT RUN.
- Cloudflare Turnstile network impact: NOT RUN; no external Turnstile request was made.
- Real PostgreSQL/Redis latency: reported by the other jobs, not this browser mock.
- Production, SSH, deployment, update and production performance testing: NOT RUN.
