# AniMemo v1.0 Frontend Performance Wave 1 Evidence

Date: 2026-08-12  
Measured commit: `b88d47bc2c7c6701ddeb900546ae080f8c980522`  
Owner: Frontend Wave 1  
Authority: Auxiliary browser evidence only

## Scope and limits

The probe covers Login, Dashboard initial/search/status-filter, Watch History initial/pagination/append, Staff Dashboard/plugin tab, Plugin Platform marketplace/installed tab, request topology, duplicate classification, route chunks, LCP, CLS, long tasks, animation-frame proxy, Staff refresh polling, and Dashboard page 48 request topology.

The run used a Windows local Vite production preview, Chromium `151.0.7922.34`, Node `v24.12.0`, viewport `1440×900`, fresh browser context per run, cache disabled, and deterministic Playwright API route mocks. These results are **AUXILIARY** and do not represent PostgreSQL, Redis, production network, or production performance.

The normal journeys used one warm-up and five measured runs, with median and nearest-rank p95. The page-48 audit used one high-cost run and reports no p95. The mock deep-pagination shape returned 48 records per page with a 10,000-record total; it is request-topology evidence, not database scaling evidence.

Raw output: `docs/performance-evidence/frontend-v1.0.json`.

## Results

| Journey | Route ready median / p95 ms | LCP median / p95 ms | API requests | JS transfer / decoded | Interaction median / p95 |
|---|---:|---:|---:|---:|---:|
| Login | 992 / 1,007 | 1,040 / 1,068 | 4 | 249,405 / 740,399 B | mode transition 346.9 / 350.5 ms |
| Dashboard | 956 / 1,012 | 508 / 528 | 12 | 292,480 / 892,468 B | search 832.8 / 977.1 ms; status filter 12.9 / 24.8 ms |
| Watch History | 959 / 997 | 508 / 520 | 15 | 292,480 / 892,468 B | open 1,936.7 / 2,132 ms; pagination 188.7 / 220.1 ms; append 197.7 / 211.7 ms |
| Staff | 662 / 960 | 580 / 624 | 9 | 284,030 / 862,906 B | plugin tab 394.8 / 508.2 ms |
| Plugin Platform | 460 / 465 | 460 / 464 | 8 | 247,126 / 736,069 B | installed tab 44.9 / 50.1 ms |

The browser observer recorded no Login animation frame over 50 ms. Long tasks were zero in most runs; Dashboard and Staff each had one measured run with a longest task of 58 ms and 56 ms respectively. CLS is auxiliary and variable in this local mock: Dashboard p95 `0.320590`, Watch History p95 `0.096324`, Staff p95 `0.001422`, Login and Plugin Platform p95 `0`.

## Request topology

Stable initial API topology was observed for all five journeys. Representative paths:

- Login: CSRF → token refresh → enabled plugins → site settings.
- Dashboard: auth bootstrap → settings/filters/tag presets/stats → entries page 1 with facets; search and status filter each issue a new page-1 query with the corresponding parameters.
- Watch History: Dashboard bootstrap → entries page 1 → watch-history page 1 → watch-history page 2 → append POST → mutation freshness refresh of stats and entries.
- Staff: auth bootstrap → staff dashboard → staff site settings; selecting Plugin Center loads staff plugins and review queues.
- Plugin Platform: auth bootstrap → enabled plugins/site settings → marketplace, installed, and user project data.

Watch History showed two exact URL repeats per measured run:

```text
GET /api/v1/stats/me/
GET /api/v1/entries/?page=1&page_size=48&priority=1&ordering=-airing_period&include_facets=1
```

The harness classified both as cross-phase, user-triggered mutation freshness requests after the append POST. They are not counted as unexplained initial duplicates and did not trigger the immediate hard-failure rule.

Dashboard page 48 audit: one request each for pages `1…48`, page 48 requested once, facets requested only on page 1, and no exact duplicate requests. The run completed in `39,050 ms` and rendered `2,304` mock entries after page 48. This is auxiliary browser topology only; authoritative LARGE pagination latency remains `NOT RUN` here and belongs to the Ubuntu/PostgreSQL workstream.

## Bundle and chunks

The current entry-reachable emitted asset inventory is approximately `25,379,866 B`, including fonts; current reachable JavaScript is `1,102,830 B` and CSS is `951,922 B`. The common JS chunk is `723,611 B` emitted / `242,681 B` transferred in the browser probe. Route chunks were observed as:

- Login: `UserAuthPage` 15,352 B decoded.
- Dashboard: `DashboardPage` 168,857 B decoded.
- Staff: `AdminDashboardPage` 139,295 B decoded.
- Plugin Platform: `PluginPlatformPage` 12,458 B decoded.

The inventory includes the generated font asset set. It is a build inventory, not a claim that all assets are downloaded by every journey. V8 parsed size and heap attribution are `NOT RUN`.

## Polling and background behavior

Staff Dashboard live refresh probe passed: configured interval `20,000 ms`; initial request `1`; two overlapping focus events resulted in one additional request; hidden-tab focus produced no request; returning visible produced one refresh. This was a deterministic mock and did not run for 20 seconds.

Staff System Update Operation polling was **NOT RUN** in the final evidence. The source contains a separate `2,500 ms` interval, but the interrupted run did not complete the planned mock validation. No conclusion is recorded for hidden-tab suppression or overlap behavior.

## Findings

Severity values below are coordinator-facing proposals only.

### PERF-FE-001

```text
ID: PERF-FE-001
Proposed Severity: PERF1
Area: Initial JavaScript bundle
Journey: Login, Dashboard, Staff, Plugin Platform
Dataset: N/A — production build inventory
Evidence: Common emitted JS chunk is 723,611 B; browser transfer is 242,681 B; Dashboard/Staff route chunks are 168,857 B / 139,295 B decoded.
Before: N/A — Wave 1 baseline only
Root Cause: The current production build keeps a large common application chunk and substantial route code in emitted bundles.
Suggested Fix: Coordinator to correlate with production bundle baseline and decide whether route-level split or dependency/manual-chunk work is release-worthy.
Contract Risk: Must preserve API v1, Auth, Plugin SDK v2, and route behavior.
Owner: Frontend / Coordinator
```

This is a candidate only. The evidence does not establish a release-blocking regression or prove that optimization is required before v1.0.

### PERF-FE-002

```text
ID: PERF-FE-002
Proposed Severity: PERF2
Area: Generated font asset inventory
Journey: All web routes
Dataset: N/A — production build inventory
Evidence: Reachable emitted inventory includes approximately 22.0 MB of font files, while the browser journeys load only the fonts selected by CSS/font matching.
Before: N/A — Wave 1 baseline only
Root Cause: The font package emits a broad character-set/format asset matrix.
Suggested Fix: Defer to v1.1 unless authoritative download traces show a user-impacting font transfer problem.
Contract Risk: Preserve Chinese text rendering and font-fidelity acceptance.
Owner: Frontend / Coordinator
```

### Not findings

- No unexplained exact duplicate critical initial API request was observed.
- No HTTP 5xx, request storm, N+1 browser symptom, or route-transition failure was observed in the mock runs.
- Staff Dashboard hidden-tab suppression and overlap coalescing passed.
- Watch History repeats occurred after the explicit append mutation and were classified as freshness behavior.

## NOT RUN / non-authoritative items

- PostgreSQL/Redis production-like browser integration: `NOT RUN` on this Windows local environment.
- Real MEDIUM/LARGE backend latency and pagination scaling: `NOT RUN`; SQLite/Windows cannot represent authoritative database performance.
- Field INP: `NOT RUN`; synthetic interactions are reported as wall-clock proxies only.
- JavaScript parsed size, V8 heap attribution, and memory leak measurement: `NOT RUN`.
- Cloudflare Turnstile integration impact: `NOT RUN`; no external Turnstile call was made.
- Watch History import preview/apply: `NOT RUN`; no large real import was executed.
- Staff System Update Operation hidden-tab/overlap: `NOT RUN`; the attempted auxiliary probe was interrupted before completion.
- Production, SSH, deployment, release promotion, and production smoke/performance: `NOT RUN`.

## Harness lifecycle note

The probe now uses `taskkill /pid <vite-pid> /t /f` on Windows during normal `finally` cleanup so Vite, esbuild, and its owned descendants terminate together; non-Windows retains the child kill path. An externally interrupted shell can bypass JavaScript `finally`, so manual cleanup remains limited to the probe-owned command-line/PID tree. The final cleanup check found no probe-owned Node, Chromium, esbuild process, or listener on port 4185.
