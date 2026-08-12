# AniMemo v1.0 Performance Inventory

Date: 2026-08-12
Benchmark contract: `docs/performance-benchmark-contract-v1.0.md`
Inventory commit base: `0f865f4c5b43b36de1fde847ee8ac0d6f99ce6ab`

This inventory defines what the v1.0 performance baseline must measure. Items labelled **candidate** are measurement targets, not findings and not authorization to optimize.

## Critical user journeys

| Priority | Journey | Primary frontend route | Critical API topology |
| --- | --- | --- | --- |
| P0 | Login / session restore | `/login` | CSRF, refresh, current user, site settings, enabled plugin manifests |
| P0 | Dashboard initial load | `/dashboard` | settings, quick filters, tag presets, analytics, entries with facets |
| P0 | Dashboard filter/search/pagination | `/dashboard` | debounced entries query; page 1, middle page, page 48; append pages |
| P0 | Journal create/update | Dashboard editor | entry create/detail/update/delete and mutation reconciliation |
| P0 | Watch History | Dashboard entry editor | history initial page, pagination, append/update/delete |
| P1 | Staff Dashboard | `/admin-control` | dashboard summary, site settings, resource panels, system health |
| P1 | Plugin Platform | `/plugins` | marketplace, installed plugins, developer projects, enabled manifests |
| P1 | Profile | Dashboard profile dialog | current settings and password/account mutations |
| P1 | Integration diagnostics | Staff / external integration clients | connections, bindings, actions, events poll/ACK |
| P1 | System Update page | Staff updates tab | status, release list, active operation and logs |

Future Marketplace scale, Runtime v3, Mobile and UI/UX 2.0 are excluded.

## Critical API and database hot paths

| Area | Endpoint / operation | Measurement focus | Candidate risk to prove or reject |
| --- | --- | --- | --- |
| Auth | `/api/v1/token/`, refresh, `/auth/me/` | latency, query count, response bytes | password hashing is intentionally expensive; no auth weakening is allowed |
| Journal | `/api/v1/entries/` | pagination, search/filter/sort, facets, query count, bytes | annotated history/external identity queries; facet collection scans owner rows |
| Journal detail | `/api/v1/entries/<id>/` | query count and response size | external identity serialization |
| Analytics | `/api/v1/stats/me/` | aggregate query count and latency | repeated aggregate work during Dashboard hydration |
| Watch History | `/api/v1/entries/<id>/watch-history/` | page 1/deep page, append, count | count query on deep pages; bounded result serialization |
| Plugin Platform | marketplace/installed/my/enabled | query count, response size, list scaling | installation counts and nested version/submission serialization |
| Staff | `/api/v1/staff/dashboard/` | query count, bytes, 20-second refresh | multiple counts and bounded recent lists |
| Staff plugins | `/api/v1/staff/plugins/review/` | query count versus plugin count | per-project installation counts are a candidate N+1 |
| Integration | events/actions/connections/bindings | query count, idle poll cost, writes | DB polling and `last_seen_at` write amplification candidates |
| Update Staff API | status/releases/operation/logs | request frequency and bytes | active-operation polling must not overlap or continue when irrelevant |

## Heavy lists and payloads

- Dashboard entries: fixed 48-item pages with infinite append; include facets only on page 1.
- Staff dashboard users: bounded recent 100, plus bounded recent entries/columns and global counts.
- Plugin marketplace, installed list, developer projects and Staff review/deployment lists: currently list-based; pagination backlog is measured before any v1.0 action.
- Watch History: bounded page size and deterministic fixture; import preview/apply use bounded synthetic inputs only.
- Integration events: maximum 100 per poll with a maximum 25-second wait.
- API response bytes and item counts are recorded without removing frozen API v1 fields.

## Polling and repeated requests

- Staff Dashboard uses a shared live-refresh controller every 20 seconds. It suppresses overlap, skips hidden tabs, refreshes on focus/visibility, and requires production-browser verification.
- Update operation status polls every 2.5 seconds only while an operation is active. Hidden-tab and overlap behavior require verification.
- Shared public journal refreshes every 60 seconds.
- Integration events use bounded server-side polling (`wait <= 25`) backed by repeated database checks.
- Dashboard initial requests already have a critical browser regression that expects one entries request; the performance probe expands topology evidence across all critical routes.
- Exact URL repetition is not automatically a defect: retries, mutation invalidation, freshness and development-only StrictMode behavior must be classified before a finding is raised.

## Frontend bundle and main-thread inventory

- `App.jsx` route-splits Login, Dashboard, Staff, Plugin Platform, Featured, Community and preview pages with React lazy loading.
- Shared runtime dependencies include React, React Router, Axios, GSAP, Font Awesome, QR rendering and Noto Sans SC assets.
- Login motion/Turnstile, Dashboard catalog rendering, Staff tables and Plugin cards are the main browser measurement surfaces.
- Record production main/route chunks, transferred resource bytes, LCP, CLS, long tasks, and interaction/route-transition proxies. Metrics that cannot be measured reliably are `NOT RUN`.

## Remote I/O and background work

- Bangumi search, subject/person lookup, OAuth/import and external sync are remote I/O; no large real import is used.
- Turnstile is external browser work; local probes use controlled behavior and record real integration impact as a limitation when unavailable.
- Media R2 paths are mock/isolated timing only. Production R2 is never contacted.
- Update Agent release verification/cache is observed only through Staff request frequency; updater verification, attestations, preflight and stable-window semantics remain frozen.
- Maintenance commands cover integration event/receipt cleanup, plugin data retention and media reservation reconciliation. This phase does not redesign them.

## Resource and capacity inventory

The isolated workload records API/Web/PostgreSQL/Redis memory and CPU where available, Redis key/memory change, database connections, request/error counts, and latency at 1, 5, 10 and 20 concurrent users. A 25-minute mixed read-heavy loop covers Dashboard, filter/search, entry detail, Watch History, Plugin Platform and Staff health. The goal is personal/small-scale stability, not maximum throughput.

## Existing guards to reuse

- Dashboard production browser initial-request and mutation regressions.
- Dashboard 100/500/1,001 entry pagination and full-dataset filter tests.
- Journal list, Watch History, analytics and Staff Dashboard query-efficiency tests.
- PostgreSQL concurrency gates for auth, plugins, Integration, Journal and media paths.
- Production build, critical E2E, fresh Docker and stateful upgrade release gates.

## Finding barrier

Wave 1 produces measurement evidence only. The coordinator then deduplicates frontend/API/resource symptoms, assigns final PERF0–PERF3 severity, and authorizes only PERF0/PERF1 remediation. PERF2/PERF3 are deferred to `docs/v1.1-technical-backlog.md` during final consolidation.
