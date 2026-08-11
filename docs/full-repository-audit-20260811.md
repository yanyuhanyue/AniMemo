# AniMemo v1.0 Full Repository Audit

审计日期：2026-08-11

## Executive Summary

本轮以正式生产验收基线 `e68dc0ce5c51c90d7b419369a31255d51736e196` 为起点，对 AniMemo v1.0 仓库的架构、安全、数据正确性、性能、可靠性、测试、部署和兼容负担进行了全仓审计。

审计共确认：

- P0：0 项。
- P1：6 项，均已修复并通过 PR、post-merge CI 与 Release Gate。
- P2：16 项，全部进入 v1.1 / Architecture Contract Hardening / Plugin Runtime v3 / UIUX 2.0 路线。
- P3：5 项，记录为低风险维护项。

本轮没有生产部署、生产 Smoke、生产数据库变更、R2/Cloudflare/Bridge/AstrBot/NapCat/OpenResty 变更。Dashboard V1.0 Core 保持冻结，未因可维护性债务进行重构。

最终审计 main：`a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905`。

结论：当前证据支持 `FULL REPOSITORY AUDIT: PASS` 与 `V1.0 RELEASE BLOCKERS: PASS`。下一阶段应进入 Architecture Contract Hardening，而不是 UI/UX 2.0、Mobile 或 Plugin Runtime v3 实施。

## Audit Scope

覆盖范围：

- Frontend、API client、Dashboard、Watch History、Analytics、Staff/Admin。
- Authentication、Authorization、2FA、Registration、Account Security、owner isolation。
- Django API、services、serializers、signals、hooks、transactions、PostgreSQL、migrations、Redis。
- R2 / Media、Plugin Platform、Plugin SDK、backend/frontend runtime、packaging、permissions、hooks。
- Integration Protocol、Integration Gateway、AstrBot Bridge、external account connections。
- CI、Release Gate、Docker、deployment scripts、OpenAPI、error contract、logging、dependencies、static quality。
- Mobile readiness、Plugin Runtime v3 readiness、Theme/UIUX readiness。

## Repository Baseline

| Item | Value |
| --- | --- |
| Expected audit base | `e68dc0ce5c51c90d7b419369a31255d51736e196` |
| Actual audit base | `e68dc0ce5c51c90d7b419369a31255d51736e196` |
| Final audited main | `a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905` |
| Dashboard V1.0 Core | FROZEN |
| Production deploy | NOT RUN |
| Production smoke | NOT RUN |
| Production SHA | `e68dc0ce5c51c90d7b419369a31255d51736e196` |

合法前进的 main 差异为三个审计修复 Squash commit：

1. `ef21526e184c06ef99d3fd00fa42284b53958733`：外部账号 OAuth refresh 并发正确性。
2. `7d6fc3c36c7aa61e2017b86c29e0b72ff0b50c6f`：Integration 响应大小与 retention。
3. `a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905`：Watch History Importer 存储与事务一致性。

## Methodology

审计采用以下证据，而非只做关键字搜索：

1. 建立子系统、入口、数据存储、外部依赖、信任边界、mutation/error path 与测试 inventory。
2. 阅读 URL routing、views、services、serializers、models、migrations、runtime registry、plugin package/install path、Bridge poller 与 deployment scripts。
3. 检查 owner-scoped queryset、capability enforcement、HMAC/nonce/replay、CSRF/JWT/refresh rotation、2FA/recovery code、staff hierarchy。
4. 检查多表 mutation 的 `transaction.atomic`、`select_for_update` 与 `transaction.on_commit`。
5. 检查 PostgreSQL concurrency tests、migration graph、Redis key TTL/namespace、R2 path/credential/write guard。
6. 检查 frontend async lifecycle、request generation、AbortController、optimistic rollback、server-state invalidation 与 critical browser E2E。
7. 检查 CI/Release Gate 在 pull request 与 push main 上的语义，验证 fresh Docker 与 BASE -> CURRENT stateful upgrade。
8. 检查 tracked secrets、package paths、ZIP validation、external requests、command execution与 error/logging contracts。
9. P0/P1 先分类后修复；P2/P3 只记录，不扩大 v1.0 scope。

## Audit Inventory

| Subsystem | Entry points / files | Data stores | Trust / mutation boundaries | Principal tests |
| --- | --- | --- | --- | --- |
| Frontend shell | `src/main.jsx`, `src/App.jsx`, `src/pages/` | memory state, limited UI preferences | Browser -> Django API; plugin asset imports | `tests/*.test.mjs`, `qa:critical` |
| Dashboard | `src/pages/DashboardPage.jsx`, `useDashboardData.js`, dashboard components | JournalEntry, WatchHistory, settings, filters | authenticated owner-scoped mutations | dashboard unit/contract tests and Playwright regressions |
| Auth/account | `backend/journal/auth_views.py`, `auth_service.py`, `account_security.py`, token views | User, security profile, refresh/access revocation | password, CSRF, HttpOnly refresh, 2FA, staff session | `test_security.py`, `test_registration_security.py` |
| Journal/core | `backend/journal/viewsets.py`, serializers, watch_history, analytics | PostgreSQL JournalEntry/WatchHistory/identity/sync | owner queryset and DTO/service boundaries | journal tests, watch history, analytics, pagination |
| Staff/admin | `staff_urls.py`, `staff_*_views.py`, `staff_services.py` | core data, audit log, system settings | staff capability + superuser hierarchy + 2FA | staff/security/query tests |
| Redis/security state | throttles, registration, HMAC nonce, temporary preview/session state | Redis/cache backend | namespaced keys, TTL, fail-closed security throttles | security, registration, integration tests |
| Media/R2 | `site_config/media_storage/`, storage serializers/views | MediaObject, local filesystem, R2 | superuser config, encrypted credentials, safe object keys | media storage and lifecycle tests |
| Plugin package/platform | `backend/plugin_host/package.py`, installer/services/views | PostgreSQL metadata, CAS, runtime filesystem | developer upload -> review -> superuser backend publish | package/platform/installer/GC/concurrency tests |
| Backend plugin runtime | `backend/plugin_host/runtime/`, `hooks.py`, capabilities | process-local registry + Core DTO capabilities | trusted in-process Python; actor-bound capabilities | runtime E2E, hooks, capability tests |
| Frontend plugin runtime | `src/plugins/sdk/PluginRuntimeContext.jsx` | browser module/style state | same-origin dynamic import; access filtered by server | plugin SDK/runtime tests |
| Official importer | `plugins/watch-history-importer/` | PluginData batches + Core entries/history | authenticated installation, bounded batch, transactional commit | reference integration, storage, PostgreSQL concurrency |
| Integration Protocol | `backend/integrations/` | connections, bindings, receipts, events, Redis nonce | HMAC instance identity -> user binding -> plugin action | protocol and PostgreSQL concurrency tests |
| AstrBot Bridge | `bridges/astrbot_plugin_animemo_bridge/` | local routes/state JSON | HMAC client, private delivery, ACK/retry | unit, validation, packaging, real runtime loader smoke |
| CI/release/deploy | `.github/workflows/`, `deploy/`, `scripts/stateful-*` | ephemeral Docker/runner data; production bind mounts | PR/push gates; scoped release operations | bootstrap, Docker, stateful upgrade gates |

## P0 Findings

未发现可证明的远程代码执行、认证绕过、权限提升、跨用户数据访问、真实 secret 泄漏、任意文件访问、重大供应链绕过或确定性整站破坏路径。

P0 FOUND: 0

P0 FIXED: 0

P0 OPEN: 0

## P1 Findings

### AUD-P1-001

ID: AUD-P1-001

SEVERITY: P1

SUBSYSTEM: External Accounts / OAuth

STATUS: FIXED

SUMMARY:

并发请求可同时使用即将过期的 OAuth refresh token；对旋转 refresh token 的 Provider，其中一个请求可能在另一个请求完成后失败。

EVIDENCE:

原刷新路径在 Provider network refresh 前没有锁定 `UserExternalAccountConnection`。修复位于 `backend/journal/external_accounts/connections.py`；PostgreSQL 回归位于 `backend/journal/test_external_account_concurrency.py`。

IMPACT:

真实用户同步/验证请求可能失败，连接可能被错误标记为需要重新授权。

ROOT CAUSE:

数据库连接状态与外部 refresh token rotation 没有串行化，也没有在锁内重读最新凭据。

FIX:

使用 PostgreSQL 行锁，锁内重读凭据；前一请求已刷新时复用最新 token；失败状态在事务提交后持久化。

TEST / VERIFICATION:

PR #42；PostgreSQL concurrency、backend suite、PR CI/Release Gate、main push CI/Release Gate 均 PASS。

### AUD-P1-002

ID: AUD-P1-002

SEVERITY: P1

SUBSYSTEM: Integration persistence / maintenance

STATUS: FIXED

SUMMARY:

Integration events 与 completed/failed action receipts 没有完整接入标准维护，记录可随正常流量持续增长。

EVIDENCE:

修复后的 `backend/integrations/management/commands/cleanup_integration_events.py` 同时清理过期 events 与 completed/failed receipts；`backend/journal/management/commands/run_maintenance.py` 已接入该命令；索引为 `integrations.0002_add_receipt_cleanup_index`。

IMPACT:

长期运行会造成 PostgreSQL 行数和索引持续膨胀，最终影响 Integration 与全站资源稳定性。

ROOT CAUSE:

已有事件清理命令未进入统一 scheduler path，receipt 模型没有 retention policy/index。

FIX:

增加配置化 retention、清理 completed/failed receipts、保留 pending、增加 additive cleanup index，并接入标准维护入口。

TEST / VERIFICATION:

PR #43；runtime cleanup tests、migration plan、stateful upgrade、PR 与 post-merge gates 均 PASS。

### AUD-P1-003

ID: AUD-P1-003

SEVERITY: P1

SUBSYSTEM: Integration action response

STATUS: FIXED

SUMMARY:

可信 backend plugin 的 Integration action response 只验证 JSON 可序列化，没有响应大小上限。

EVIDENCE:

`backend/integrations/services.py` 现在对序列化响应执行独立 256 KiB 上限；`backend/integrations/tests/test_protocol.py` 验证 `action_response_too_large` 的稳定失败回执与 replay。

IMPACT:

异常插件可把大型 payload 持久化到 receipt，放大数据库空间、序列化内存与 replay 成本。

ROOT CAUSE:

request body 已有上限，但 plugin response -> receipt path 没有对称资源边界。

FIX:

增加响应字节上限；超限保存稳定 failed receipt 并返回 502，后续相同 request_id 重放同一失败。

TEST / VERIFICATION:

PR #43；Integration protocol runtime tests、backend suite、PR 与 post-merge gates 均 PASS。

### AUD-P1-004

ID: AUD-P1-004

SEVERITY: P1

SUBSYSTEM: Watch History Importer / PluginData

STATUS: FIXED

SUMMARY:

官方 importer 将完整 preview batch 长期写入 PluginData，缺少单批大小、每用户批次数与 retention 边界。

EVIDENCE:

`backend/plugin_host/storage.py` 新增 `set_bounded()`；`backend/plugin_host/management/commands/cleanup_watch_history_import_batches.py` 与 `plugin_host.0003_add_plugin_data_retention_index` 提供 retention；限制项位于 `backend/config/settings.py` 和 `.env.example`。

IMPACT:

认证用户可反复上传/预览，稳定放大 PostgreSQL 存储与 JSON 处理成本。

ROOT CAUSE:

通用 PluginData API 没有为 importer 的大型批次数据提供原子配额模型。

FIX:

Web TXT 默认 4 MiB、单批 PluginData 默认 40 MiB、每用户最多 4 批、默认保留 7 天；用户行锁保证并发 preview 不突破数量限制。

TEST / VERIFICATION:

PR #44；storage limit tests、PostgreSQL concurrent preview、maintenance、migration/stateful gate 均 PASS。

### AUD-P1-005

ID: AUD-P1-005

SEVERITY: P1

SUBSYSTEM: Watch History Importer / transaction and events

STATUS: FIXED

SUMMARY:

Core entry/history mutation、batch imported 状态与事件投递不在一致的事务边界；存储或事件失败可导致已提交 mutation 返回假失败或部分成功。

EVIDENCE:

`plugins/watch-history-importer/backend/plugin.py` 现在把 Core mutation、subject 映射与 batch result 同事务提交，并通过 robust `transaction.on_commit` 发送 `history-updated` / `import-completed`。

IMPACT:

客户端可能看到失败并重试，但数据库已写入；或 Core 已写入而 batch 仍显示未完成。

ROOT CAUSE:

跨 Core capability、PluginData 与 Integration event 的提交顺序没有明确 transaction/on-commit contract。

FIX:

统一事务；持久化结果后再返回；事件失败不反转已完成 mutation 的成功结果。

TEST / VERIFICATION:

PR #44；reference integration tests 覆盖 storage/event failure、idempotent replay 与 persisted result；全部 gates PASS。

### AUD-P1-006

ID: AUD-P1-006

SEVERITY: P1

SUBSYSTEM: Watch History Importer / concurrency

STATUS: FIXED

SUMMARY:

同一 batch 可用不同 request_id 并发提交，两个请求都可能在 imported 状态写入前执行 Core mutation。

EVIDENCE:

`plugins/watch-history-importer/backend/plugin.py` 对 batch row 使用 `select_for_update`；`backend/plugin_host/tests/test_concurrency.py` 在 PostgreSQL 上并发提交同一 batch 并断言只创建一次 Core 数据与一次完成事件。

IMPACT:

可创建重复 JournalEntry / WatchHistory 或重复 Integration event。

ROOT CAUSE:

idempotency 只绑定 Integration request_id，没有对业务对象 batch identity 加数据库级串行化。

FIX:

锁定 batch，锁内重读 imported 状态；跨 request_id 重复提交返回已存结果。

TEST / VERIFICATION:

PR #44；PostgreSQL concurrent commit、PR stateful-upgrade、main push CI/Release Gate 均 PASS。

P1 FOUND: 6

P1 FIXED: 6

P1 OPEN: 0

## P2 Findings

### AUD-P2-001

ID: AUD-P2-001

SEVERITY: P2

SUBSYSTEM: Core API contract

STATUS: DEFERRED

SUMMARY: Core auth/journal/staff/plugin endpoints mostly live directly under `/api/`; only Integration Protocol has an explicit `/v1/` namespace.

EVIDENCE: `backend/config/urls.py`, `backend/journal/urls.py`, `backend/plugin_host/urls.py`.

IMPACT: Future Mobile/shared clients cannot negotiate breaking API changes independently from the web deployment.

ROOT CAUSE: The application evolved as one web frontend and one Django backend release unit.

DEFERRED REASON: API versioning is Architecture Contract Hardening scope; changing URLs before a compatibility design would create needless v1.0 churn.

TEST / VERIFICATION: Current OpenAPI validation and web clients pass; no current correctness blocker.

### AUD-P2-002

ID: AUD-P2-002

SEVERITY: P2

SUBSYSTEM: Authentication transport

STATUS: DEFERRED

SUMMARY: Refresh authentication is bound to HttpOnly browser cookies plus CSRF acquisition, while access tokens live in web-process memory.

EVIDENCE: `src/lib/api.js` uses `withCredentials`, `auth/csrf/`, cookie refresh and browser storage scrubbing; `docs/api-errors.md` documents the cookie-only refresh contract.

IMPACT: Native Mobile needs a different secure refresh transport and cannot reuse the current web auth adapter as-is.

ROOT CAUSE: Auth core and web transport were implemented together.

DEFERRED REASON: Requires Auth Core / Web Adapter separation and a product decision for native secure storage; no v1.0 web defect exists.

TEST / VERIFICATION: Refresh rotation, CSRF, logout, password/session revocation and concurrency tests pass.

### AUD-P2-003

ID: AUD-P2-003

SEVERITY: P2

SUBSYSTEM: Frontend portability

STATUS: DEFERRED

SUMMARY: API setup, routing, plugins, animations and shared UI utilities directly use `window`, `document`, `location`, `localStorage` and `sessionStorage`.

EVIDENCE: `src/lib/api.js`, `src/plugins/sdk/PluginRuntimeContext.jsx`, `src/components/PageColorTransition.jsx`, auth and modal utilities.

IMPACT: SSR, React Native and shared domain-package extraction require browser adapters or dependency injection.

ROOT CAUSE: Browser-only assumptions are distributed through presentation and transport modules.

DEFERRED REASON: Mobile/SSR implementation is explicitly outside this release; current browser target is correct.

TEST / VERIFICATION: Browser build, unit tests and critical E2E pass.

### AUD-P2-004

ID: AUD-P2-004

SEVERITY: P2

SUBSYSTEM: Dashboard maintainability

STATUS: DEFERRED

SUMMARY: `src/pages/DashboardPage.jsx` remains an 858-line orchestration component with mutations, dialogs, URL state and cross-panel coordination.

EVIDENCE: File size and responsibility review; existing supporting hooks/components reduce but do not remove the central coordination load.

IMPACT: Future changes have a wider regression surface and higher review cost.

ROOT CAUSE: Multiple v1.0 workflows converged on one page controller.

DEFERRED REASON: Dashboard Core is frozen and current mutation/query correctness is proven; refactoring solely for aesthetics would violate scope.

TEST / VERIFICATION: Dashboard large-data, query, mutation and critical E2E gates pass.

### AUD-P2-005

ID: AUD-P2-005

SEVERITY: P2

SUBSYSTEM: Theme/UIUX contract

STATUS: DEFERRED

SUMMARY: Global presentation remains concentrated in the 4,159-line `src/styles.css` with page-specific colors, spacing and component primitives; the production build emits a non-blocking large-chunk warning.

EVIDENCE: `src/styles.css`, page/component class usage, existing design documentation, and the final Vite build output (approximately 939 kB CSS and a 721 kB main JS chunk before gzip).

IMPACT: Theme changes and UIUX 2.0 would require broad CSS edits instead of semantic tokens and slots; first-load asset cost also has room for measured optimization.

ROOT CAUSE: The visual system was implemented directly against the production reference before a formal theme contract existed.

DEFERRED REASON: Theme Contract/UIUX 2.0 is a later phase; no current accessibility or operability release blocker was found.

TEST / VERIFICATION: Current visual/interaction tests and production acceptance baseline remain green.

### AUD-P2-006

ID: AUD-P2-006

SEVERITY: P2

SUBSYSTEM: OpenAPI / plugin contract

STATUS: DEFERRED

SUMMARY: Core OpenAPI validates cleanly, but dynamic `/api/plugins/<slug>/<path>` runtime routes and plugin-defined errors are intentionally outside the static core schema.

EVIDENCE: `backend/config/urls.py`, `backend/plugin_host/runtime/routes.py`, `docs/frontend-architecture.md`.

IMPACT: Generated clients cannot discover plugin API shapes and cross-client compatibility is contractual rather than machine-verified.

ROOT CAUSE: Runtime plugin routes are registered dynamically after package validation.

DEFERRED REASON: Needs a Plugin SDK/OpenAPI extension design; not appropriate for a release-blocker patch.

TEST / VERIFICATION: Core `spectacular --validate --fail-on-warn` passes; runtime route tests pass.

### AUD-P2-007

ID: AUD-P2-007

SEVERITY: P2

SUBSYSTEM: Frontend Plugin Runtime

STATUS: DEFERRED

SUMMARY: Enabled frontend plugins are dynamically imported and execute in the same browser origin and DOM as Core.

EVIDENCE: `src/plugins/sdk/PluginRuntimeContext.jsx` imports `frontendEntry`, injects styles and exposes host APIs.

IMPACT: A malicious frontend plugin could access same-origin web capabilities available to Core.

ROOT CAUSE: v2/v3 frontend runtime uses a trusted-publisher model rather than sandbox/iframe isolation.

DEFERRED REASON: Current publication/review trust model is controlled; absence of sandbox alone is not a v1.0 blocker. Isolation belongs to future plugin platform hardening.

TEST / VERIFICATION: Access exposure, asset sessions, disable/dispose and package validation tests pass.

### AUD-P2-008

ID: AUD-P2-008

SEVERITY: P2

SUBSYSTEM: Backend Plugin Runtime

STATUS: DEFERRED

SUMMARY: Backend plugins execute as in-process Python modules inside the Django API process.

EVIDENCE: `backend/plugin_host/runtime/registry.py` loads runtime files with `importlib` and tracks modules in `sys.modules`; backend publish requires a superuser in `backend/plugin_host/installer.py`.

IMPACT: A trusted backend plugin has process-level execution and can affect API worker stability.

ROOT CAUSE: Current accepted trust model is `trusted-superuser` in-process execution.

DEFERRED REASON: Ordinary users cannot publish backend runtime directly; worker/container isolation is Plugin Runtime v3 scope, not a discovered bypass.

TEST / VERIFICATION: Publish authorization, draft/runtime, package security and runtime E2E tests pass.

### AUD-P2-009

ID: AUD-P2-009

SEVERITY: P2

SUBSYSTEM: Official plugin portability

STATUS: DEFERRED

SUMMARY: The official watch-history importer directly imports Django settings/transactions/timezone and plugin_host storage/errors.

EVIDENCE: `plugins/watch-history-importer/backend/plugin.py` and its Bangumi client module.

IMPACT: Moving the runtime to worker/RPC/container isolation would require replacing direct Django/process imports with SDK contracts.

ROOT CAUSE: Host capabilities cover domain data, but operational storage/network/transaction facilities are not fully abstracted.

DEFERRED REASON: Implementing those abstractions now would prematurely start Runtime v3.

TEST / VERIFICATION: Current trusted runtime and package gates pass.

### AUD-P2-010

ID: AUD-P2-010

SEVERITY: P2

SUBSYSTEM: Plugin hooks/events/runtime state

STATUS: DEFERRED

SUMMARY: Hook, SDK event and runtime registrations are process-local callback registries protected by `RLock`.

EVIDENCE: `backend/plugin_host/hooks.py`, `backend/plugin_host/sdk/events.py`, `backend/plugin_host/runtime/registry.py`.

IMPACT: Multi-worker reload/reconciliation relies on each process observing deployment state and rebuilding identical registrations; callbacks do not cross process/RPC boundaries.

ROOT CAUSE: Runtime lifecycle is designed around an in-process singleton registry.

DEFERRED REASON: Current workers reconcile from persisted deployment state and tests cover replacement/cleanup; durable RPC/event contracts belong to Runtime v3.

TEST / VERIFICATION: Hook ordering, activation/deactivation, replacement and concurrency tests pass.

### AUD-P2-011

ID: AUD-P2-011

SEVERITY: P2

SUBSYSTEM: Integration event delivery performance

STATUS: DEFERRED

SUMMARY: HTTP long-poll holds a synchronous Django worker for up to 25 seconds and queries PostgreSQL every 250 ms while idle.

EVIDENCE: `backend/integrations/views.py` `EventsView.get()` uses a `time.sleep` loop and repeated `IntegrationEvent` queries.

IMPACT: A large number of Integration connections could consume workers and amplify idle database traffic.

ROOT CAUSE: Protocol v1 implements portable long-poll without an async notification/broker path.

DEFERRED REASON: Current deployment has limited trusted integrations and no measured saturation; queue/ASGI redesign would exceed v1.0 scope.

TEST / VERIFICATION: Protocol wait bounds, event privacy, cursor and ACK tests pass.

### AUD-P2-012

ID: AUD-P2-012

SEVERITY: P2

SUBSYSTEM: AstrBot Bridge state growth

STATUS: DEFERRED

SUMMARY: Delivered event IDs are bounded, but `pending_event_ids` is an unbounded deque persisted to `state.json`.

EVIDENCE: `bridges/astrbot_plugin_animemo_bridge/animemo_bridge/state.py` initializes `pending_event_ids = deque()` and appends deferred events without a maximum.

IMPACT: Prolonged missing-route traffic can grow local state and repeatedly block cursor advancement.

ROOT CAUSE: The Bridge preserves every unacknowledged event for later route repair and has no dead-letter/expiry policy.

DEFERRED REASON: Server retention, private route model and current scale keep risk medium; a bounded/dead-letter policy needs product semantics for dropped notifications.

TEST / VERIFICATION: Delivery, ACK retry, route defer and corrupt-state recovery tests pass.

### AUD-P2-013

ID: AUD-P2-013

SEVERITY: P2

SUBSYSTEM: Plugin marketplace/developer API performance

STATUS: DEFERRED

SUMMARY: Marketplace, installed and developer project endpoints return full lists and nested version data without pagination.

EVIDENCE: `backend/plugin_host/views.py` `MarketplaceView`, `InstalledPluginListView`, `MyPluginProjectView` and serializers.

IMPACT: A future large marketplace or prolific developer account could produce large responses and query/serialization amplification.

ROOT CAUSE: Current official/small marketplace assumptions shaped the API.

DEFERRED REASON: Current data set is intentionally small and bounded upload/draft limits reduce immediate risk; pagination is v1.1 API work.

TEST / VERIFICATION: Current marketplace/privacy/install tests pass.

### AUD-P2-014

ID: AUD-P2-014

SEVERITY: P2

SUBSYSTEM: Critical browser E2E coverage

STATUS: DEFERRED

SUMMARY: `npm run qa:critical` covers auth focus/login mechanics and Dashboard request/mutation paths, but not Watch History UI, staff auth boundary or plugin marketplace read.

EVIDENCE: `package.json` composes only `qa:auth-focus`, `qa:dashboard-initial-request` and `qa:dashboard-mutations`.

IMPACT: Those high-value browser integrations rely on backend/runtime tests and source-level UI contracts rather than one production-build browser path.

ROOT CAUSE: Critical E2E was deliberately kept narrow and deterministic for v1.0.

DEFERRED REASON: No known regression is unguarded at P1 severity; add only low-maintenance journeys in v1.1.

TEST / VERIFICATION: Existing critical suite is self-contained, production-build based and green in CI.

### AUD-P2-015

ID: AUD-P2-015

SEVERITY: P2

SUBSYSTEM: Frontend regression testing

STATUS: DEFERRED

SUMMARY: Many Node tests read JSX/CSS/Python source and assert regex/string structure; some UI/security behavior is therefore protected indirectly.

EVIDENCE: `tests/admin-audit-log.test.mjs`, `tests/bangumi-account-import.test.mjs`, `tests/core-journal-experience.test.mjs` and similar files use `readFileSync` plus `assert.match`.

IMPACT: Refactors can break tests without behavior changes, while structurally matching code may pass without executing the user workflow.

ROOT CAUSE: Static contract tests were inexpensive during rapid convergence and coexist with a smaller runtime E2E set.

DEFERRED REASON: Static package/CI boundary tests remain appropriate; behavior-critical cases should be migrated gradually, not deleted wholesale before release.

TEST / VERIFICATION: Dashboard mutation behavior and backend security paths already have runtime tests; remaining conversions are backlog work.

### AUD-P2-016

ID: AUD-P2-016

SEVERITY: P2

SUBSYSTEM: Static quality

STATUS: DEFERRED

SUMMARY: ESLint enforces recommended syntax plus Rules of Hooks but not `react-hooks/exhaustive-deps`; Python Ruff is limited to `E9,F63,F7,F82`.

EVIDENCE: `eslint.config.js` and `.github/workflows/ci.yml`.

IMPACT: Stale dependency arrays, unused code and additional high-confidence correctness classes rely on review/tests rather than static gates.

ROOT CAUSE: Static quality was intentionally introduced with low false-positive rules to avoid mass stylistic churn.

DEFERRED REASON: Enable only after measuring and addressing findings in scoped PRs; do not turn v1.0 closure into a repository-wide formatting/refactor project.

TEST / VERIFICATION: Current lint/Ruff gates pass with zero warnings/errors.

P2 FOUND: 16

P2 DEFERRED: 16

## P3 Findings

### AUD-P3-001

ID: AUD-P3-001

SEVERITY: P3

SUBSYSTEM: GitHub Actions maintenance

STATUS: DEFERRED

SUMMARY: Workflows still use `actions/checkout@v4`, `setup-node@v4` and `setup-python@v5`, producing GitHub's Node.js 20 deprecation annotations.

EVIDENCE: `.github/workflows/ci.yml`, `.github/workflows/release-gate.yml`, main push run annotations.

IMPACT: Low immediate risk because GitHub currently forces the actions onto Node 24; future removal could fail workflows.

ROOT CAUSE: Major action upgrades are pending normal dependency maintenance.

DEFERRED REASON: Dependabot branches exist and upgrades require ordinary full-gate review, not release-blocker handling.

TEST / VERIFICATION: Current workflows complete successfully.

### AUD-P3-002

ID: AUD-P3-002

SEVERITY: P3

SUBSYSTEM: Frontend container hardening

STATUS: DEFERRED

SUMMARY: The nginx frontend image uses the upstream default user/process model and Compose does not declare read-only filesystem or dropped capabilities.

EVIDENCE: `deploy/frontend.Dockerfile`, `deploy/docker-compose.yml`.

IMPACT: Defense-in-depth is weaker if nginx is compromised, though the service has no Docker socket/host network and is bound to localhost.

ROOT CAUSE: Deployment prioritized a minimal upstream nginx image.

DEFERRED REASON: No current escape or privilege path was found; hardening can be validated separately in v1.1 operations work.

TEST / VERIFICATION: Production-like Docker gate passes.

### AUD-P3-003

ID: AUD-P3-003

SEVERITY: P3

SUBSYSTEM: Environment coupling

STATUS: DEFERRED

SUMMARY: Bridge/deployment defaults include `https://re-anime.cc` and a concrete 1Panel OpenResty container name.

EVIDENCE: `bridges/astrbot_plugin_animemo_bridge/main.py`, `deploy/deploy.sh`.

IMPACT: Forks and alternate environments must override defaults and are easier to misconfigure.

ROOT CAUSE: Operational scripts target the current single production topology.

DEFERRED REASON: Values are configurable and current production coupling is intentional.

TEST / VERIFICATION: Bootstrap, Bridge validation and deployment preflight tests pass.

### AUD-P3-004

ID: AUD-P3-004

SEVERITY: P3

SUBSYSTEM: Frontend package metadata

STATUS: DEFERRED

SUMMARY: Root `package.json` remains version `0.0.0`; release identity is expressed through Git/GitHub and plugin/Bridge versions instead.

EVIDENCE: `package.json`.

IMPACT: Low; tooling expecting an application SemVer cannot infer the AniMemo release version from npm metadata.

ROOT CAUSE: The package is private and not published to npm.

DEFERRED REASON: Choose an application versioning policy during Architecture Contract Hardening/release automation work.

TEST / VERIFICATION: Build/package workflows do not depend on this field.

### AUD-P3-005

ID: AUD-P3-005

SEVERITY: P3

SUBSYSTEM: Frontend compatibility residue

STATUS: DEFERRED

SUMMARY: `src/lib/api.js` still removes legacy local/session storage token keys on startup and auth changes.

EVIDENCE: `LEGACY_ACCESS_KEY`, `LEGACY_REFRESH_KEY` and `scrubLegacyTokens()` in `src/lib/api.js`.

IMPACT: Negligible runtime cost; it is a small historical compatibility branch.

ROOT CAUSE: Earlier builds stored tokens in browser storage and the secure migration preserved cleanup.

DEFERRED REASON: Keeping cleanup is safer through v1.0; removal can follow an explicit compatibility cutoff.

TEST / VERIFICATION: Auth tests confirm credentials are no longer persisted there.

P3 FOUND: 5

P3 DEFERRED: 5

## Security Boundaries

| Trust boundary | Credentials | Permissions | Data access | Execution / network | Trust level |
| --- | --- | --- | --- | --- | --- |
| Browser | password/OTP entered transiently; access token in memory; refresh HttpOnly cookie | current user | current user's API DTOs and public data | same-origin HTTP, approved external links | untrusted client |
| Web frontend | bearer access + CSRF/cookies through adapter | server-authoritative | no direct DB/storage | browser JS | untrusted for authorization |
| Django API | app secrets, DB/Redis/R2 credentials | DRF auth, owner querysets, staff capability | authoritative Core data | provider/R2/email network clients | trusted core |
| PostgreSQL | DB credential | DB role | all persisted business state | no app code execution | trusted persistence |
| Redis | Redis URL/password if configured | network/private deployment | cache, throttle, nonce, temporary state | no plugin execution | trusted ephemeral security state |
| R2 | encrypted access key/secret | superuser-configured backend | media objects only | S3/Cloudflare APIs | trusted external storage |
| Frontend Plugin | asset session / host APIs; no raw refresh secret | manifest exposure + user installation | host/API-visible same-origin data | same-origin JS/DOM/network | trusted publisher, not sandboxed |
| Backend Plugin | process identity; no Integration HMAC secret | approved/published; backend publish by superuser | actor-bound capabilities, plus trusted process access | in-process Python and declared network | trusted-superuser runtime |
| Integration Client | HMAC key id + secret | one connection | bound user routes/actions/events | signed HTTP | trusted external instance |
| AstrBot Bridge | Integration HMAC secret | configured plugin/admin commands | local route/state and delivered events | outbound HTTPS + private message delivery | trusted operator plugin |
| Staff | password + optional/required 2FA session | explicit roles/capabilities | scoped admin resources | staff API actions | privileged |
| Superuser | password + 2FA/security session | all staff capabilities | all application/admin data | backend plugin publish, media credentials, system config | highest trust |

## Data Correctness

- Dashboard server/client state, optimistic rollback, request generation, pagination and mutation invalidation remain covered and frozen.
- Watch History Core owns canonical history; importer owns source parsing/preview only.
- External account import/sync uses snapshots, owner isolation and idempotency.
- P1 transaction and concurrency defects in OAuth refresh and importer commit are closed.
- No destructive migration or incompatible data rewrite was introduced.

## Performance

- Dashboard and Staff query profiles have targeted tests and no release-blocking N+1 was found.
- Hot user lists are paginated/bounded; import/provider requests enforce limits.
- Integration long-poll, plugin list pagination and Bridge pending state remain P2 scale work.
- No meaningless microbenchmark or broad optimization refactor was performed.

## Frontend

Status: PASS with P2/P3 deferred.

React lifecycle, timers, request cancellation, stale response suppression, optimistic rollback, server-state invalidation and auth refresh sharing were reviewed. Critical E2E covers auth and Dashboard. Browser coupling, Dashboard concentration, global CSS and source-contract tests are deferred.

## Dashboard

Status: PASS / FROZEN.

Large-data pagination, server query identity, infinite append deduplication, total/loaded counts, stale-request suppression, create/update/delete reconciliation, quick-status rollback, settings/filter persistence and mutation-time query switching are covered. No P0/P1 was found after the completed Dashboard correctness phase, so no Dashboard code was changed in this audit.

## Watch History

Status: PASS.

Core owns canonical `WatchHistoryRecord`; owner isolation, validation, pagination, analytics summary and external identity integration have runtime tests. The importer remains a parser/resolution/preview workflow and now commits Core mutations, batch result and on-commit events consistently. Storage growth and concurrent duplicate commit P1s are closed.

## Backend

Status: PASS.

Views, services, serializers, hooks and error paths were reviewed across journal/auth/staff/plugin/integration/media. Confirmed P1 issues were fixed in narrow PRs. No owner-isolation or permission bypass remains open.

## Auth

Status: PASS.

JWT access, HttpOnly rotated refresh, CSRF, logout, session-version revocation, password reset/change, account deletion, TOTP, recovery codes, staff login and Turnstile boundaries have runtime tests. OAuth refresh concurrency is fixed.

## Authorization and Owner Isolation

Status: PASS.

Owner-scoped tests cover JournalEntry, WatchHistory, analytics, external identities/accounts/sync previews, plugin settings/storage and Integration bindings/events/actions. Staff capability and superuser hierarchy tests fail closed. Cross-user private resources generally return 404 to avoid metadata disclosure.

## Database

Status: PASS.

Constraints, uniqueness, owner foreign keys, additive indexes and concurrency paths were reviewed. New audit fixes add only `integrations.0002_add_receipt_cleanup_index` and `plugin_host.0003_add_plugin_data_retention_index`.

## Migration

Status: PASS.

`makemigrations --check --dry-run` reports no drift. The graph preserves historical journal migrations, including Core watch-history migration `journal.0004`; no history was rewritten or reversed. The two audit migrations are additive indexes and pass BASE -> CURRENT stateful upgrade validation. They were not applied to production during this audit.

## Redis

Status: PASS.

Security-sensitive rate limits and HMAC nonce keys are namespaced and TTL-bound. Authentication throttling is tested fail-closed when Redis is unavailable. No unbounded Core Redis collection was found.

## R2 / Media

Status: PASS.

Credentials are encrypted, API serialization exposes only configured flags, object keys/local paths are normalized, uploads are re-encoded and bounded, writes use independent availability/write-block semantics, deletes are on-commit/idempotent, and orphan auditing exists. No ordinary-user controlled SSRF or cross-user media overwrite was found.

## Plugin Platform

Status: PASS with P2 architecture debt.

Package inspection rejects traversal, absolute paths, symlinks/special files, excessive files/sizes and manifest/index mismatches. CAS and immutable `slug + version` identity are gated. USER installations are owner-scoped. Backend publish is superuser-only. Runtime isolation and contract portability remain future work.

## Integration Protocol

Status: PASS with P2 scale debt.

HMAC method/path/body signature, timestamp window, nonce replay protection, one-time pairing, connection/user binding, request idempotency, event privacy, cursor and ACK ownership are covered. Response and retention P1s are fixed. Long-poll scalability remains P2.

## AstrBot Bridge

Status: PASS with one P2 queue item.

Retries/backoff, HMAC, route redaction, diagnostics, persistent state, ACK-after-delivery, loader/package compatibility and poller cleanup are covered. The unbounded pending event deque is deferred.

## Staff/Admin

Status: PASS.

Role/capability fail-closed behavior, 2FA gate, audit log, user hierarchy, media/plugin management and query profiles were reviewed. No GET mutation or staff/superuser bypass remains open.

## CI / Release

Status: PASS.

Both workflows run on pull requests and main push. CI includes lint, build, frontend tests, critical E2E, Ruff, Django check, migration check, OpenAPI, backend/PostgreSQL/plugin/Bridge/runtime/bootstrap gates. Release Gate includes fresh Docker and stateful BASE -> CURRENT upgrade. PR #44 also corrected the fixture so each container derives the expected official plugin version from its own bundled manifest.

## Deployment

Status: PASS with P3 hardening debt.

Compose binds web to localhost, isolates its network, uses health dependencies, persistent scoped mounts and non-root API execution. Deployment scripts contain preflight/migration/scoped replacement/rollback logic. No production command was executed in this audit.

## OpenAPI

Status: PASS with one P2 contract gap.

Core schema generation and `--validate --fail-on-warn` pass. Bearer and cookie/CSRF auth semantics are documented. Dynamic plugin dispatch remains outside the static Core schema and is tracked as AUD-P2-006.

## Error Contract

Status: PASS.

Canonical errors use HTTP status plus stable `code`, readable `detail`, optional `fields`/metadata; frontend parsing does not require Chinese string matching for control flow. Domain-specific conflict/import/provider codes remain stable and production errors do not expose stack traces, SQL, paths or credentials.

## Logging

Status: PASS.

Sensitive provider failures, pairing commands and Bridge diagnostics are redacted; cleanup/maintenance output reports counts and non-secret identifiers. No tracked secret or production credential was found; the high-confidence private-key pattern match is a validator test string, not a credential.

## Dependency Audit

Status: PASS with P3 action maintenance.

Frontend lockfile is enforced by `npm ci`; `npm audit --omit=dev` on 2026-08-11 reported 0 vulnerabilities across the production tree. Backend direct dependencies and exact lock output are checked by `scripts/update_dependencies.py --check`; an ephemeral UTF-8 `pip-audit -r backend/requirements.txt` run reported no known vulnerabilities. Weekly Dependabot covers npm, pip and GitHub Actions. No dependency upgrade was performed solely because a newer version exists.

## Testing Coverage Map

| Area | Unit | Integration | Browser E2E | Production smoke |
| --- | --- | --- | --- | --- |
| Auth/registration/2FA | strong | Django API + concurrency | critical auth focus/login | previous production acceptance only |
| Dashboard | strong | Django API/query tests | critical initial request + mutations | previous production acceptance |
| Watch History | strong | Core/importer/runtime/PostgreSQL | not in `qa:critical` | NOT RUN this audit |
| Plugin Platform | strong | runtime/package/PostgreSQL/stateful | not in `qa:critical` | NOT RUN |
| Integration | strong | HMAC/runtime/PostgreSQL | not applicable | NOT RUN |
| AstrBot Bridge | strong | real AstrBot loader/runtime matrix | Plugin Page static/runtime checks | NOT RUN |
| Staff/R2 | strong | API/query/storage tests | not in `qa:critical` | NOT RUN |
| Deployment | script tests | fresh Docker + stateful upgrade | health/frontend curl smoke | no production deploy |

## Mobile Readiness

Status: DEFERRED / P2.

Primary blockers are cookie/CSRF-only refresh transport, browser globals in reusable frontend modules, unversioned Core API, web-specific plugin runtime and lack of a stable shared client package. No Mobile project was created.

## Plugin Runtime v3 Readiness

Status: DEFERRED / P2.

Primary blockers are direct Django/settings/storage imports in official plugins, in-process `sys.modules` loading, process-local hook/event registries and callback-only contracts. No Worker/RPC/Container implementation was started.

## Theme/UIUX Readiness

Status: DEFERRED / P2.

The 4,159-line global stylesheet and page-specific primitives need semantic tokens, component contracts and plugin extension slots. No UI redesign or design-system migration was performed.

## Fixed Findings

| Finding | PR | Merge SHA |
| --- | --- | --- |
| AUD-P1-001 | #42 修复外部账号 OAuth 令牌并发刷新竞态 | `ef21526e184c06ef99d3fd00fa42284b53958733` |
| AUD-P1-002, AUD-P1-003 | #43 限制 Integration 响应并接入回执清理 | `7d6fc3c36c7aa61e2017b86c29e0b72ff0b50c6f` |
| AUD-P1-004, AUD-P1-005, AUD-P1-006 | #44 限制观看记录导入批次并保证提交一致性 | `a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905` |

## Deferred Findings

P2/P3 的完整执行顺序、验收条件和禁止项见 `docs/v1.1-technical-backlog.md`。Architecture Contract Hardening 的设计输入见 `docs/architecture-contract-hardening-inputs.md`。

## Final Verification Matrix

本地最终矩阵在审计代码 SHA `a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905` 加三份纯文档变更上执行：

| Gate | Result | Evidence |
| --- | --- | --- |
| `npm ci` | PASS | 237 packages installed from lockfile |
| `npm run lint` | PASS | ESLint zero warnings/errors |
| `npm run build` | PASS | Vite production build; only recorded P2 chunk-size warning |
| `npm test` | PASS | 140 passed |
| `npm run qa:critical` | PASS | auth focus, Dashboard initial request, Dashboard mutations |
| Ruff correctness | PASS | `E9,F63,F7,F82` |
| Django check | PASS | 0 issues |
| Migration drift | PASS | `No changes detected` |
| Migration plan | PASS | additive `integrations.0002` and `plugin_host.0003`; local pre-migration DB shows both pending as expected |
| OpenAPI | PASS | spectacular validate/fail-on-warn |
| Dependency lock | PASS | reproducible re-resolution matched |
| Frontend dependency audit | PASS | npm production vulnerabilities: 0 |
| Backend dependency audit | PASS | pip-audit: no known vulnerabilities |
| Backend tests | PASS | 508 passed, 34 skipped |
| Script tests | PASS | 28 passed |
| Plugin validate/build | PASS | watch-history-importer 0.4.1 |
| Official plugin immutability | PASS | base 0.4.0 -> current 0.4.1 |
| Plugin pack | PASS | `.ajplugin` produced |
| AstrBot Bridge tests | PASS | 50 passed |
| Bridge validate/package | PASS | Bridge 0.1.3 |
| AstrBot real runtime | PASS | GitHub CI: v4.27.2 and pinned master snapshot |
| PR #44 CI / Release Gate | PASS | all jobs including PostgreSQL and stateful-upgrade |
| PR #44 post-merge CI / Release Gate | PASS | main push runs for `a7c6eb3` |
| `git diff --check` | PASS | no whitespace errors |

## V1.1 Backlog

建议优先顺序：

1. Architecture Contract Hardening：API versioning、Auth/API adapter、OpenAPI、stable identity。
2. Integration/Bridge scale：long-poll architecture、pending/dead-letter policy。
3. Plugin SDK/Runtime v3 readiness：去除 direct Django/process assumptions，但不立即容器化。
4. Testing/static correctness：三条低维护 browser journey、逐步替换行为关键 source-regex tests、扩大高置信度 lint。
5. Theme/UIUX contract 与 deployment defense-in-depth。

## V1.0 Recommendation

```text
P0 OPEN: 0
P1 OPEN: 0
P2 DEFERRED: 16
P3 DEFERRED: 5

FULL REPOSITORY AUDIT: PASS
V1.0 RELEASE BLOCKERS: PASS

PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN

ANI MEMO V1.0 NEXT STEP: Architecture Contract Hardening
```
