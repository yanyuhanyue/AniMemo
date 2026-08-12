# AniMemo v1.0 深度架构与技术债审计

审计日期：`2026-08-11`

## Executive Summary

本轮从 `6452b3dbfff39529c49c2bc69ede1f3d76236eee` 审计 AniMemo 当前架构。该提交也是审计开始时的 `main` 与 `origin/main`；`e68dc0c` 是其祖先提交，已被当前基线完整包含。

现有 API v1、Auth Core/Web Adapter、稳定资源身份、Plugin capability enforcement、Integration Protocol v1 和前端共享 Core 均有有效 contract 与测试保护。没有发现需要推翻 v1.0 公共 contract、身份模型或数据模型的 TD0 Structural Blocker。

审计确认 5 项 TD1。它们集中在四条 seam：Journal mutation、Plugin Host/RPC、Integration receipt lifecycle 与 Media write reservation。当前功能与安全门禁可以继续通过，但这些 seam 若留到 Runtime v3、Marketplace 或更高媒体写入量之后再处理，迁移成本会明显放大。因此原始审计结论是审计成功、架构债需要单独 Closure，不在 Audit PR 中静默重构。2026-08-12 的 Closure decision 已进一步收敛：DA-TD1-002、003、005 已 RESOLVED；DA-TD1-001、004 已记录为 ACCEPTED V1.0 DEBT EXCEPTION，不再处于 BLOCKED 或 UNDECIDED。

```text
TD0 OPEN: 0
ORIGINAL AUDIT TD1 OPEN SNAPSHOT (2026-08-11): 5
CURRENT TD1 OPEN: 0
CURRENT TD1 RESOLVED: 3
CURRENT TD1 ACCEPTED EXCEPTIONS: 2
CURRENT TD1 UNDECIDED: 0
TD2 DEFERRED: 14
TD3 DEFERRED: 5

DEEP ARCHITECTURE & TECHNICAL DEBT AUDIT: PASS
ARCHITECTURE DEBT CLOSURE REQUIRED: PASS
ORIGINAL ANI MEMO V1.0 NEXT STEP: Architecture Debt Closure
ARCHITECTURE DEBT CLOSURE: PASS WITH ACCEPTED DEBT
CURRENT ANI MEMO V1.0 NEXT STEP: Deployment / Updater Hardening
```

## Scope And Method

- 审计范围：Frontend、Backend、Auth、API、Domain Services、Models、Serializers、Plugin Host、Frontend/Backend Plugin Runtime、官方插件、Integration、AstrBot Bridge、R2/Media、External Accounts、Staff/Admin、CI、Release、部署脚本。
- 方法：代码和 import direction 阅读、事务与副作用追踪、contract/test 覆盖核对、热点与历史兼容清单、当前 GitHub Gate 证据核对。
- 判断口径：module、interface、seam、adapter、depth 与 locality；LOC 只作为调查信号，不作为 verdict。
- 未执行：生产部署、生产 mutation smoke、SSH、数据库/R2/Bridge/Cloudflare/Docker 全局变更。

## CURRENT ARCHITECTURE MAP

```text
Consumers
  Web UI / Future Mobile / Frontend Plugins / AstrBot Bridge
       |          |              |                 |
       v          v              v                 v
Contracts
  API v1 + Error Contract   Frontend Host SDK   Integration Protocol v1
       |                           |                 |
       v                           v                 v
Adapters / Transports
  DRF Views + Web Auth      Same-origin runtime   HMAC/Bearer gateway
       |                           |                 |
       +-------------+-------------+-----------------+
                     v
Domain / Application modules
  Journal / Watch History / Auth Tokens / External Accounts / Sync
  Integration dispatch / Plugin platform workflow / Media pool policy
                     |
                     v
Infrastructure
  Django ORM / PostgreSQL / Redis / filesystem / R2 / external HTTP

Extension path today
  Core Domain -> Plugin Hook Registry -> in-process Python callbacks
  Plugin request -> DRF request -> plugin handler
  Official plugin -> Host capability + Django/DRF/QuerySet/requests

Target extension path
  Core Domain -> stable event/policy seam -> transport adapter
  Plugin DTO/RPC -> Capability Gateway -> Domain Services / Providers
```

### Module Classification

| Area | Public contract | Internal module | Adapter / infrastructure | Consumer / provider |
| --- | --- | --- | --- | --- |
| Frontend | catalog/profile/admin UI contracts | pages, components, state hooks | React Router, Web transport, browser APIs | Web users |
| Backend | health/API/runtime process boundary | Django apps and configuration | Gunicorn, Django middleware, ORM | Web, plugins, integrations |
| API | `/api/v1/`, OpenAPI, error codes | serializers, query construction | DRF Views, Web transport | Web, future Mobile |
| Auth | token/session semantics | `auth_tokens.py` | `web_auth_adapter.py`, cookies/CSRF/Turnstile | Web, future native adapter |
| Domain Services | Journal, Watch History, account, sync invariants | `JournalEntryService` and domain modules | ORM repositories embedded in services | Views, Plugin capabilities, Integration |
| Models | stable identity, constraints, persisted state | Django model definitions | PostgreSQL/SQLite test adapter | Domain services and admin |
| Serializers | API validation and representation | DRF serializer classes | DRF request/response adapter | Web/Mobile clients |
| Plugin Host | Manifest v2, SDK v2, capability/error contract | registry, installer, marketplace workflow | package CAS, runtime loader | official/third-party plugins |
| Frontend Plugin Runtime | navigation/page/auth/event Host SDK | runtime registry/context | same-origin React/CSS/Router adapter | frontend plugins |
| Backend Plugin Runtime | routes/hooks/storage/settings/capabilities | runtime registry/context/dispatch | in-process Python/Django adapter | backend plugins |
| Official Plugins | Watch History Importer manifest and actions | parser/resolver/import workflow | current Django/DRF/requests assumptions | AniMemo users, Integration |
| Integration | Protocol v1, HMAC, actions/events/receipts | dispatch and receipt state | DRF, database polling | AstrBot Bridge, providers |
| AstrBot Bridge | Integration Protocol v1 client contract | routing, event state, renderers | AstrBot runtime and local JSON state | chat users/operators |
| R2 / Media | media object identity and pool policy | pool selection/quota accounting | local filesystem and R2 adapters | upload surfaces |
| External Accounts | provider/account/sync contracts | OAuth refresh and sync planning | Bangumi HTTP adapter, encrypted credentials | Journal sync users |
| Staff/Admin | staff capability and audit contracts | staff services and plugin review workflow | staff DRF views/admin UI | administrators/operators |
| CI / Release | required CI and Release Gate | workflows and fixtures | GitHub Actions, production-like Compose | maintainers/release operator |
| Scripts | package, upgrade, validation and deploy CLIs | Python/Node/shell scripts | filesystem, Git, Docker CLI | CI and operator |

## Dependency Direction

目标方向 `UI / Transport / Plugins / Integration -> Domain / Contracts -> Infrastructure` 大部分成立，但存在一条需要 Closure 的反向依赖：`journal.domain_services -> plugin_host.hooks`。这使 Core mutation 直接知道 Plugin Runtime，并把 callback lifecycle 带入事务语义。

已确认正确的方向：

- `apiCore.js` 与 `authSession.js` 不依赖 DOM、React、Axios 或 Web auth adapter。
- `auth_tokens.py` 不接收 DRF request，也不写 cookie/response。
- Plugin capabilities 调用 Journal/Watch History domain logic，而不是调用 View。
- AstrBot Bridge 只消费 Integration Protocol v1。

需要 Closure 的方向：

- Core Domain 直接调用 Plugin Hook Registry。
- Plugin-facing API 暴露 DRF request、Django user、QuerySet、transaction 与任意 Python callable；这些值不能跨 RPC seam。

## TD0

`TD0 OPEN: 0`

没有发现公共 API contract 实际失效、重大身份模型错误、必须破坏性迁移、或需要在 v1.0 前推翻 Core/Adapter 分层的 Structural Blocker。

## TD1 Findings

### DA-TD1-001 Backend Plugin SDK 不是可序列化的 Runtime interface

```text
ID: DA-TD1-001
CATEGORY: Plugin SDK / Runtime v3 Readiness
SEVERITY: TD1
PATH: backend/plugin_host/runtime/context.py; backend/plugin_host/runtime/dispatch.py;
      backend/plugin_host/storage.py; plugins/watch-history-importer/backend/
SYMBOL: PluginContext; PluginDispatch._dispatch; PluginStorage.collection;
        WatchHistoryImporterPlugin
```

**EVIDENCE:** `PluginContext.root` 暴露宿主路径，`request_json()` 直接使用 `requests`；dispatch 把 DRF request 交给 plugin handler；`PluginStorage.collection()` 返回 Django QuerySet；官方插件直接导入 Django settings/transaction/timezone、DRF Response/status、`requests`，并对 storage QuerySet 调用 `select_for_update()`。现有 architecture test 只禁止官方插件导入 `journal.*` 与 private `plugin_host.*`，没有禁止 Django、DRF、QuerySet 或任意网络。

**WHY THIS IS DEBT:** SDK 的 nominal capability boundary 存在，但 interface value 与 lifecycle 仍是 in-process Python。把 callback 改成 RPC 时，request、model/user、QuerySet、transaction context、filesystem root 与 Python exception 都无法直接序列化。

**CURRENT IMPACT:** 官方插件可绕过 Host 的 transaction、network 与 storage abstraction；测试通过依赖同进程和 Django 环境。

**FUTURE IMPACT:** Runtime v3 worker/container 需要同时改 Host、官方插件、storage、request transport、network broker 与测试，形成 flag-day migration。

**AFFECTED:**
- Web
- Plugin
- Integration
- Runtime v3
- Production

**RECOMMENDATION:** 冻结 JSON-compatible request/response/error DTO；用 Host storage operations 取代 QuerySet；用显式 transaction/batch capability 取代 `django.db.transaction`；由 `host.http` broker 执行网络；官方插件先迁移为 reference consumer。

**FIX BEFORE V1:** YES

### Closure disposition (2026-08-12)

**Original Finding:** 上述非序列化对象与 trusted in-process implementation 会阻塞未来 Runtime v3 worker/container/RPC 的自然迁移。

**Original Reason:** 直接把 request、Response、QuerySet、transaction、filesystem root 与 Python callback 改成 RPC 会形成 Plugin SDK/package breaking change。

**Resolution or Exception:** **ACCEPTED V1.0 DEBT EXCEPTION**。SDK API 2、Manifest v2、actor-bound capability DTO、权限/安装边界和官方插件 0.4.2 在 v1.0 保持不变；当前 route/request/storage implementation 被明确标记为 trusted in-process Runtime v3 deferred debt。触发 worker/container/RPC、不受信任 publisher 或独立资源隔离前，必须进入 versioned SDK v3/Host adapter remediation。决策 dossier 见 `docs/v1.0-remaining-td1-decisions-20260812.md`。

**ESTIMATED BLAST RADIUS:** High；Plugin Host、官方插件、Integration action adapter 与 SDK tests。

**TEST REQUIREMENT:** SDK serialization contract、官方插件禁止 Django/DRF/requests/QuerySet 的 AST gate、in-process adapter parity、plugin package/immutability/runtime/integration regressions。

### DA-TD1-002 Core Domain 反向依赖 Plugin Runtime，hook 同步运行在事务和行锁内

```text
ID: DA-TD1-002
CATEGORY: Dependency Direction / Transaction Boundary / Hidden Side Effect
SEVERITY: TD1
PATH: backend/journal/domain_services.py; backend/journal/account_security.py;
      backend/plugin_host/hooks.py
SYMBOL: JournalEntryService.update_from_fields; delete_current_account;
        HookRegistry.run_hook/run_filter
```

**EVIDENCE:** `journal.domain_services` 顶层导入 `plugin_host.hooks.run_hook`；`update_from_fields()` 在 `transaction.atomic()` 和 entry row lock 内调用 `update()`，后者同步执行 `journal.after_update`。账户删除在用户/超级管理员行锁事务内执行 before filter 与 after hook。Hook Registry 每次执行会 runtime reconcile、查 PluginProject/安装状态并同步调用全部 Python callback。

**WHY THIS IS DEBT:** Domain mutation 的 transaction policy 被 Runtime callback 的延迟、失败和加载状态隐式控制；Domain 依赖了 adapter/runtime，而不是依赖稳定的 event/policy interface。

**CURRENT IMPACT:** 慢插件会延长锁；开放失败 hook 可能在数据库已写但事务未提交时观察状态；账户删除 callback 可以扩大最高风险事务。

**FUTURE IMPACT:** RPC/async hook 无法保持当前隐式时序，任何 transport 改动都会迫使重定义 commit、ordering 与 failure semantics。

**AFFECTED:**
- Web
- Plugin
- Runtime v3
- Production

**RECOMMENDATION:** 区分 transaction-critical policy filter 与 post-commit event；前者使用小型、可超时、无网络的 policy interface，后者用 `transaction.on_commit` 发布不可变 DTO，并由 runtime adapter 同步或异步消费。

**FIX BEFORE V1:** YES

**ESTIMATED BLAST RADIUS:** High；Journal、account deletion、hook registry、plugin tests。

**TEST REQUIREMENT:** commit/rollback ordering、hook failure mode、slow callback isolation、account deletion policy、Runtime unavailable、concurrent mutation regression。

### DA-TD1-003 Journal mutation 没有单一 authoritative seam

```text
ID: DA-TD1-003
CATEGORY: Domain Boundary / Multiple Sources Of Truth
SEVERITY: TD1
PATH: backend/journal/entry_views.py; backend/journal/import_export_views.py;
      backend/journal/data_bundle/services.py; backend/journal/external_sync/services.py
SYMBOL: JournalEntryViewSet.perform_*; CSV import commit; import_data_bundle;
        apply_sync_plan
```

**EVIDENCE:** API create/update 使用 `JournalEntryService`，DELETE 仍由 ViewSet 直接删除并调用 hook；CSV import 直接 `JournalEntrySerializer.save(user=...)`；Data Bundle 直接 `JournalEntry.objects.create()`；External Sync 直接给 entry 赋值并 `save(update_fields=...)`。

**WHY THIS IS DEBT:** create/update/delete、validation、invariant、hook/event 与 audit semantics 没有唯一 ownership。每个 adapter 可以在不知情时绕过另一条路径新增的规则。

**CURRENT IMPACT:** 当前各路径依靠专用测试维持一致，但 hook 触发与未来 invariant 已经不一致。

**FUTURE IMPACT:** Mobile、批量导入、Plugin Marketplace 与同步 provider 新增 mutation 时会继续复制规则；修复一条路径无法保证其他路径。

**AFFECTED:**
- Web
- Mobile
- Plugin
- Integration
- Runtime v3
- Production

**RECOMMENDATION:** 让 Journal application seam 拥有 create/update/delete 与明确的 restore/sync mode；CSV、Data Bundle、External Sync 和 Plugin capability 作为 adapter 调用同一 seam。保留各 domain 的 orchestration，不创建只转发 ORM 的泛化 service。

**FIX BEFORE V1:** YES

**ESTIMATED BLAST RADIUS:** High；Journal API、import/export、bundle、sync、hooks。

**TEST REQUIREMENT:** 所有 mutation adapter 的 invariant/event parity、owner isolation、rollback、bulk import、external sync concurrency、delete semantics。

### DA-TD1-004 Integration PENDING receipt 没有 lease、接管或终态恢复

```text
ID: DA-TD1-004
CATEGORY: Integration State Machine / Reliability
SEVERITY: TD1
PATH: backend/integrations/services.py; backend/integrations/models.py;
      backend/integrations/management/commands/cleanup_integration_events.py
SYMBOL: _claim_receipt; _wait_for_receipt; _stored_receipt_result;
        IntegrationActionReceipt
```

**EVIDENCE:** receipt claim 创建 `PENDING` 后才调用 handler；worker 在 claim 后退出会留下 `completed_at=NULL`。重试只等待固定时间，然后永久返回 `request_in_progress`。cleanup 只删除 COMPLETED/FAILED，不处理 PENDING；model 没有 lease owner、lease deadline 或 attempt metadata。

**WHY THIS IS DEBT:** idempotency key 的 ownership 没有可恢复 lifecycle；`PENDING` 实际是无期限锁，而不是有租约的执行权。

**CURRENT IMPACT:** 单次进程崩溃、kill 或 host restart 可永久冻结一个合法 request_id，operator 没有安全恢复路径。

**FUTURE IMPACT:** 多 worker、长任务与 Runtime v3 会提高 crash-after-claim 概率；Integration consumer 会被迫更换 request_id，破坏重试语义。

**AFFECTED:**
- Integration
- Plugin
- Runtime v3
- Production

**RECOMMENDATION:** 通过 additive fields 引入 lease/claimed_at/attempt；过期 lease 可原子接管；完成写入验证当前 lease owner；cleanup/diagnostics 处理孤儿 PENDING；明确 handler 是否允许重放。

**FIX BEFORE V1:** YES

### Closure disposition (2026-08-12)

**Original Finding:** PENDING receipt 没有 lease、接管或终态恢复，crash-after-claim 可能留下永久 `request_in_progress`。

**Original Reason:** 没有 action-specific replay contract 时，通用 takeover 可能让已经完成外部副作用的旧 handler 被再次执行。

**Resolution or Exception:** **ACCEPTED V1.0 DEBT EXCEPTION**。Integration Protocol v1 保持 at-most-once safety-first 语义：同一 `(connection, request_id)` 唯一 claim；PENDING duplicate 只等待并在超时后返回 409；COMPLETED/FAILED 才 replay；常规 cleanup 不删除 PENDING。已确认的债务是 liveness/availability，而非当前 live-process duplicate execution、auth 或 data-integrity failure。进入多 worker crash recovery、长任务/异步 action 或 Runtime v3 前，必须先定义 action replay policy，再以 additive lease/token/conditional finalize 实现接管。决策 dossier 见 `docs/v1.0-remaining-td1-decisions-20260812.md`。

**ESTIMATED BLAST RADIUS:** Medium-High；Integration model、dispatch、cleanup、Bridge retry expectations。

**TEST REQUIREMENT:** crash-after-claim、lease expiry takeover、两 worker 竞争、stale owner finalize、completed replay、cleanup 与 stateful-upgrade gate。

### DA-TD1-005 Media 外部写入发生在全局数据库行锁内

```text
ID: DA-TD1-005
CATEGORY: Transaction Boundary / Infrastructure
SEVERITY: TD1
PATH: backend/site_config/media_storage/pool.py
SYMBOL: MediaStoragePool.write
```

**EVIDENCE:** `write()` 在 `transaction.atomic()` 内锁定唯一的 `MediaStoragePoolSettings` row，再锁定候选 backend row，并在锁未释放时执行 `candidate_adapter.write()`。该调用可能是 R2 网络 I/O 或本地文件 I/O；只有物理写入成功后才创建 MediaObject/更新 quota。

**WHY THIS IS DEBT:** 为保证 quota 正确性而使用的全局锁同时覆盖不可控外部 I/O，把 correctness lock 变成系统级串行化点。

**CURRENT IMPACT:** 一个慢 R2 请求会阻塞所有上传、backend selection 和部分管理 mutation；数据库连接和事务持续时间受外部服务控制。

**FUTURE IMPACT:** avatar/poster/Plugin media 增长后，吞吐和故障传播会显著恶化；多 backend failover 会在同一锁内累积超时。

**AFFECTED:**
- Web
- Plugin
- UI/UX
- Production

**RECOMMENDATION:** 建立短事务 reservation/finalization seam：锁内预留 quota 与 backend identity，锁外写物理对象，短事务 finalize；失败时幂等释放 reservation/清理孤儿对象。

**FIX BEFORE V1:** YES

**ESTIMATED BLAST RADIUS:** High；Media pool、quota、R2/local adapters、cleanup 与 migrations。

**TEST REQUIREMENT:** quota race、reservation expiry、write failure/failover、orphan cleanup、concurrent upload、backend mutation 与 PostgreSQL regression。

## TD2 Findings

以下 14 项有真实维护成本，但不构成 v1.0 Structural Blocker。Dashboard 项继续服从 Freeze 规则。

### DA-TD2-001 Dashboard coordination concentration

ID: DA-TD2-001
CATEGORY: Frontend State / Complexity
SEVERITY: TD2
PATH: `src/pages/DashboardPage.jsx`; `src/pages/useDashboardData.js`
SYMBOL: `DashboardPage`; `useDashboardData`

**EVIDENCE:** 页面共同协调 URL filters、server state、demo persistence、selection、modals、optimistic mutation 与 plugin navigation；行为已有专项回归保护。

**WHY THIS IS DEBT:** 多个变化轴汇聚在同一 controller，降低 state ownership 与 change locality；不是因为文件行数本身。

**CURRENT IMPACT:** Dashboard 变更需要同时理解页面状态、请求 generation、mutation reconciliation 与展示流程。

**FUTURE IMPACT:** UI/UX 2.0 解冻后，布局和 interaction 迭代会扩大回归面。

**AFFECTED:**
- Web
- UI/UX

**RECOMMENDATION:** 解冻后按真实 domain ownership 拆 controller/hook，保持共享 catalog components 与现有行为。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** High。

**TEST REQUIREMENT:** Dashboard query、mutation、pagination、infinite load、modal 与 critical E2E。

### DA-TD2-002 Dashboard query semantics are mirrored client-side

ID: DA-TD2-002
CATEGORY: Duplicate Business Logic / Multiple Sources Of Truth
SEVERITY: TD2
PATH: `src/pages/dashboardMutation.js`; `backend/journal/entry_views.py`
SYMBOL: `matchesDashboardQuery`; `JournalEntryViewSet.get_queryset/filter_queryset`

**EVIDENCE:** optimistic matcher 手工镜像 search/status/tag/year/activity/quick filter/sort；`needs-attention` 的 poster 判定已使用不同表达。

**WHY THIS IS DEBT:** membership 与 ordering 规则在 JavaScript 和 ORM 两处独立演进。

**CURRENT IMPACT:** mutation 后 visible rows/count 依赖前端 mirror 与服务器恰好一致。

**FUTURE IMPACT:** 新 filter 或排序语义可能造成 UI 暂时显示错误、错误 count 或额外 reload。

**AFFECTED:**
- Web
- UI/UX
- Production

**RECOMMENDATION:** 让 mutation response 提供 authoritative membership/page hint，或以共享 contract vectors 约束两端。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** backend filter vectors、optimistic mutation membership/count、pagination/infinite-load E2E。

### DA-TD2-003 Plugin Manifest vocabulary has multiple authorities

ID: DA-TD2-003
CATEGORY: Multiple Sources Of Truth / Contract
SEVERITY: TD2
PATH: `backend/plugin_host/manifest.py`; `plugins/plugin.schema.json`; `scripts/validate-plugins.mjs`; `scripts/pluginctl.py`
SYMBOL: Manifest extensions/capabilities/hooks/roles validators

**EVIDENCE:** extensions、core capabilities、hooks、roles 与 cross-field rules 在 Python、JSON Schema、Node validator 与 package CLI 多处手写。

**WHY THIS IS DEBT:** 同一 vocabulary 没有单一 authoritative source，validator parity 依赖人工维护。

**CURRENT IMPACT:** Manifest v2 当前靠重复 fixtures 和 CI 保持一致。

**FUTURE IMPACT:** Manifest v3、Marketplace 或新增 capability 会提高 drift 概率。

**AFFECTED:**
- Plugin
- Runtime v3
- Marketplace
- CI

**RECOMMENDATION:** 选择 schema/contract metadata 为 canonical source，其余 validator 消费或执行 exhaustive parity tests。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** 全 validator 共用 positive/negative fixtures、package build/validate/immutability gates。

### DA-TD2-004 Plugin platform workflow concentration

ID: DA-TD2-004
CATEGORY: Complexity Hotspot / Responsibility Boundary
SEVERITY: TD2
PATH: `backend/plugin_host/views.py`; `backend/plugin_host/services.py`
SYMBOL: marketplace/developer/review/publish/install/preview/GC workflows

**EVIDENCE:** 市场、开发者上传、审核、发布、安装、预览、扫描与 GC 集中在两个高变更 module。

**WHY THIS IS DEBT:** 多个 lifecycle owner 共享 transport/helper context，降低修改 locality；判断基于职责与依赖，不基于 LOC。

**CURRENT IMPACT:** 小范围 Marketplace 或 review 改动需要跨越大量无关 symbols。

**FUTURE IMPACT:** Marketplace 扩展、trusted publisher 与 Runtime v3 deployment 会增加交叉修改。

**AFFECTED:**
- Plugin
- Marketplace
- Runtime v3
- Staff/Admin

**RECOMMENDATION:** 按 marketplace、developer project/version、review、deployment 与 package lifecycle 分 module；不增加 forwarding-only layers。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium-High。

**TEST REQUIREMENT:** upload/review/publish/install/rollback/preview/GC workflow regressions 与 permissions。

### DA-TD2-005 Integration polling occupies synchronous workers

ID: DA-TD2-005
CATEGORY: Integration Performance / Resource Ownership
SEVERITY: TD2
PATH: `backend/integrations/services.py`; Integration event views
SYMBOL: `_wait_for_receipt`; event long-poll loop

**EVIDENCE:** receipt wait 使用 `time.sleep(0.05)` 轮询数据库，event long poll 也在同步 Django worker 内等待。

**WHY THIS IS DEBT:** idle wait 持有 request worker 和数据库访问预算，没有独立 wakeup seam。

**CURRENT IMPACT:** 当前低连接量可接受，但每个等待 consumer 占用同步容量。

**FUTURE IMPACT:** Bridge/Integration consumer 增加后会放大 worker 与 DB 压力。

**AFFECTED:**
- Integration
- Bridge
- Production

**RECOMMENDATION:** 评估 ASGI notification、Redis/pubsub、database notification 或 bounded short-poll。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium-High。

**TEST REQUIREMENT:** Protocol v1 cursor/ACK/replay tests、并发 idle load、DB query/worker measurements。

### DA-TD2-006 HMAC authentication writes observation state on every poll

ID: DA-TD2-006
CATEGORY: Hidden Side Effect / HTTP Semantics
SEVERITY: TD2
PATH: `backend/integrations/authentication.py`
SYMBOL: `IntegrationHMACAuthentication.authenticate`

**EVIDENCE:** 每次成功 HMAC authentication 都更新 `last_seen_at`，包括高频 GET event polling。

**WHY THIS IS DEBT:** authentication read path 隐式拥有持续持久化写入，观测职责与认证职责耦合。

**CURRENT IMPACT:** polling 产生写放大与 connection row contention。

**FUTURE IMPACT:** 多 Bridge/consumer 会让观测写成为热点。

**AFFECTED:**
- Integration
- Bridge
- Production

**RECOMMENDATION:** 节流、聚合或异步写入 liveness observation，不改变认证结果。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low-Medium。

**TEST REQUIREMENT:** HMAC vectors/replay、last-seen staleness bound、polling write-count 与 concurrency。

### DA-TD2-007 Dynamic Plugin routes lack a versioned schema artifact

ID: DA-TD2-007
CATEGORY: API Contract / Discoverability
SEVERITY: TD2
PATH: `backend/config/urls.py`; `backend/plugin_host/runtime/routes.py`
SYMBOL: `PluginDispatch`; dynamic plugin route registry

**EVIDENCE:** `/api/v1/plugins/{slug}/...` 明确排除 Core OpenAPI，route contract 只由 runtime validation 和 plugin implementation 表达。

**WHY THIS IS DEBT:** Plugin-owned API 没有机器可发现、可版本化的独立 contract artifact。

**CURRENT IMPACT:** Web official plugin 通过已知实现调用，工具与生成 client 无法发现 route DTO/error。

**FUTURE IMPACT:** Marketplace、Mobile 和第三方 Integration 难以安全消费 plugin APIs。

**AFFECTED:**
- Plugin
- Mobile
- Marketplace
- Integration

**RECOMMENDATION:** 定义独立 Plugin OpenAPI artifact、namespace/version/error publication，不污染 Core schema。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** plugin schema validation、route/manifest parity、enabled/disabled/revoked visibility。

### DA-TD2-008 Frontend Plugin runtime couples to host DOM/CSS/Router

ID: DA-TD2-008
CATEGORY: Frontend Plugin / Theme / Isolation
SEVERITY: TD2
PATH: `plugins/watch-history-importer/frontend/plugin.js`; `src/plugins/`
SYMBOL: frontend plugin registration, injected styles and navigation

**EVIDENCE:** 官方 frontend plugin 同源注入 React/CSS/Router，并依赖 Host component/style conventions；Theme Contract 与 semantic slots 尚未建立。

**WHY THIS IS DEBT:** Extension interface 同时包含稳定功能 contract 与宿主 presentation implementation details。

**CURRENT IMPACT:** 当前 trusted official plugin 与现有 UI 协同工作，但视觉/路由变化需 plugin-aware 修改。

**FUTURE IMPACT:** UI/UX 2.0、`.ajtheme` 或第三方插件可能因 selector/token/router 假设失效。

**AFFECTED:**
- Web
- Plugin
- UI/UX
- Marketplace

**RECOMMENDATION:** 先完成 trusted publisher/isolation ADR，再建立 semantic slots 与 theme tokens；不在冻结 Dashboard 内强拆。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** High。

**TEST REQUIREMENT:** Host SDK route/auth/event contract、plugin visual smoke、theme token/slot compatibility。

### DA-TD2-009 Behavior-critical tests rely on source assertions

ID: DA-TD2-009
CATEGORY: Test Architecture / Fragility
SEVERITY: TD2
PATH: `tests/*.test.mjs`; selected backend/static contract tests
SYMBOL: `readFileSync`/regex source assertions

**EVIDENCE:** 多个测试通过源码字符串或 regex 证明运行行为；package/security/static contract 中一部分使用合理，但 behavior claims 较脆。

**WHY THIS IS DEBT:** implementation text 被当作 runtime outcome，重构可能造成假失败或漏掉等价错误实现。

**CURRENT IMPACT:** 当前 CI 能保护已知形状，但维护者必须辨别 static contract 与 behavior contract。

**FUTURE IMPACT:** Runtime v3/UI 重构会放大无行为变化的 test churn。

**AFFECTED:**
- Web
- Plugin
- CI
- Maintainability

**RECOMMENDATION:** 逐项将 behavior claim 转为 runtime/contract test，保留确属 package/security 形状的静态断言。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** replacement test 必须证明相同 observable behavior，且不降低 package/security coverage。

### DA-TD2-010 Static correctness rules are intentionally narrow

ID: DA-TD2-010
CATEGORY: Static Quality / Test Architecture
SEVERITY: TD2
PATH: `eslint.config.js`; `.github/workflows/ci.yml`
SYMBOL: ESLint rules; Ruff invocation

**EVIDENCE:** ESLint 关闭 unused rules 且未启用 `react-hooks/exhaustive-deps`；Ruff 仅运行 fatal families。

**WHY THIS IS DEBT:** 一部分高置信度错误只能在 review/runtime 才暴露，缺少低成本自动反馈。

**CURRENT IMPACT:** 现有测试覆盖主要行为，但死赋值与 hook dependency 风险仍需人工识别。

**FUTURE IMPACT:** 前端 state 与 Python module 数量增长后，遗漏成本提高。

**AFFECTED:**
- Web
- Backend
- CI
- Maintainability

**RECOMMENDATION:** 以 isolated report 试启 `exhaustive-deps` 和 1-2 个 Ruff correctness families，按误报率决定 gate。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** rule trial report、零行为改写的 scoped fixes、full CI comparison。

### DA-TD2-011 Deployment builds mutable artifacts on the server

ID: DA-TD2-011
CATEGORY: Deployment / Updater Readiness
SEVERITY: TD2
PATH: `deploy/docker-compose.yml`; `deploy/backend.Dockerfile`; `deploy/frontend.Dockerfile`; `deploy/deploy.sh`
SYMBOL: Compose `build`; API container `CMD`; deploy image build/swap

**EVIDENCE:** 服务器从源码 build API/Web；API container 启动自动 migrate/sync；没有 GHCR digest、release manifest 或同 artifact promotion。

**WHY THIS IS DEBT:** release identity 绑定部署时 build 环境，而不是 CI 产出的 immutable artifact。

**CURRENT IMPACT:** 当前单 VPS deployer 有 checksum、rollback 与 production-like gate，但 RC/Stable 不是同一 digest promotion。

**FUTURE IMPACT:** 明确阻塞 Build Once Promote Many 与 restricted Update Agent。

**AFFECTED:**
- Production
- Deployment/Updater
- CI/Release

**RECOMMENDATION:** 在专门阶段构建 GHCR API/Web images、release manifest/signature，拆出 migration job，并按 digest promote。

**FIX BEFORE V1:** YES — in Deployment / Updater Hardening, not Architecture Debt Closure

**ESTIMATED BLAST RADIUS:** High。

**TEST REQUIREMENT:** immutable digest parity、RC-to-Stable promotion、migration/update/rollback、production-like Compose gate。

### DA-TD2-012 Official Plugin uses N+1 Host history calls

ID: DA-TD2-012
CATEGORY: Plugin Capability Granularity / RPC Cost
SEVERITY: TD2
PATH: `plugins/watch-history-importer/backend/plugin.py`; `backend/plugin_host/runtime/capabilities.py`
SYMBOL: `integration_history_get`; `BoundWatchHistoryCapability.list_history`

**EVIDENCE:** `history-get` 列表分支先 list entries，再对每个 entry 调一次 `list_history`。

**WHY THIS IS DEBT:** 当前函数调用在进程内便宜，但 capability 语义会在 RPC 后变成 N+1 transport calls。

**CURRENT IMPACT:** 100 entries 产生逐 entry history query/call；当前上限约束避免无限放大。

**FUTURE IMPACT:** Runtime v3/remote Integration 会增加 latency 与 serialization overhead。

**AFFECTED:**
- Plugin
- Integration
- Runtime v3

**RECOMMENDATION:** 提供 bounded batch/read-model capability 或一次查询 DTO。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** Host call/query count、owner isolation、DTO/error parity、official plugin integration tests。

### DA-TD2-013 Plugin list surfaces are unpaginated

ID: DA-TD2-013
CATEGORY: Performance / API Bound
SEVERITY: TD2
PATH: `backend/plugin_host/views.py`
SYMBOL: marketplace, installed, developer project/version list views

**EVIDENCE:** 多个 Plugin list surfaces 返回完整集合，没有统一 pagination/response bound。

**WHY THIS IS DEBT:** response size 与 query work 随 Marketplace/安装量线性增长，没有稳定 page contract。

**CURRENT IMPACT:** 当前官方插件和小规模项目数量可接受。

**FUTURE IMPACT:** Marketplace 扩展会使页面、Mobile client 与 staff review response 变重。

**AFFECTED:**
- Plugin
- Marketplace
- Web
- Mobile

**RECOMMENDATION:** 引入稳定排序、bounded pagination 与兼容 frontend migration。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** pagination contract、stable ordering、query count、legacy response migration tests。

### DA-TD2-014 AstrBot pending event state is unbounded

ID: DA-TD2-014
CATEGORY: Bridge Recovery / State Ownership
SEVERITY: TD2
PATH: `bridges/astrbot_plugin_animemo_bridge/animemo_bridge/state.py`
SYMBOL: `EventState.pending_event_ids`

**EVIDENCE:** pending deque 没有 maxlen、age、dead-letter 或 operator recovery；缺失 route 可长期阻塞 cursor。

**WHY THIS IS DEBT:** unresolved delivery state 没有 bounded lifecycle 或明确 operator ownership。

**CURRENT IMPACT:** 长期 route 故障会扩大 state file，并阻止 cursor 越过 pending event。

**FUTURE IMPACT:** 更多 routes/events 会增加积压与恢复复杂度。

**AFFECTED:**
- Bridge
- Integration
- Production

**RECOMMENDATION:** 增加有界 pending、age/expiry、dead-letter diagnostics 与显式恢复命令。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Medium。

**TEST REQUIREMENT:** overflow、expiry、route restoration、cursor/ACK、restart persistence 与 operator recovery。

其中 DA-TD2-011 的 target phase 是 Deployment/Updater Hardening；其他 TD2 已去重进入 `docs/v1.1-technical-backlog.md`。

## TD3 Findings

### DA-TD3-001 GitHub Actions majors are behind available updates

ID: DA-TD3-001
CATEGORY: CI Maintenance
SEVERITY: TD3
PATH: `.github/workflows/ci.yml`; `.github/workflows/release-gate.yml`
SYMBOL: `actions/checkout`; `actions/setup-node`; `actions/setup-python`

**EVIDENCE:** workflows 仍使用 checkout v4/setup-node v4/setup-python v5；Dependabot 已有 major PR，当前 runner 给出 Node 20 deprecation annotation。

**WHY THIS IS DEBT:** 这是维护窗口与 runtime deprecation 跟进，不是当前架构错误。

**CURRENT IMPACT:** Gate 仍 PASS，仅有 annotation。

**FUTURE IMPACT:** 长期不升级会增加 action runtime 兼容风险。

**AFFECTED:**
- CI/Release

**RECOMMENDATION:** 独立依赖维护 PR 升级并跑 main/Release Gate。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low。

**TEST REQUIREMENT:** PR CI、Release Gate、stateful base resolution。

### DA-TD3-002 Root package version is placeholder metadata

ID: DA-TD3-002
CATEGORY: Version Metadata
SEVERITY: TD3
PATH: `package.json`
SYMBOL: `version`

**EVIDENCE:** root package version 为 `0.0.0`，Git tags、Core commit 与 Plugin versions 分别承担 release identity。

**WHY THIS IS DEBT:** 诊断面缺少一条明确的 application version policy，但不影响 runtime contract。

**CURRENT IMPACT:** package metadata 不能单独回答 AniMemo release version。

**FUTURE IMPACT:** Artifact/Updater diagnostics 可能出现版本来源歧义。

**AFFECTED:**
- CI/Release
- Deployment/Updater

**RECOMMENDATION:** 决定 root version 是 authoritative、private placeholder 或 build-generated，并写入 release contract。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low。

**TEST REQUIREMENT:** release identity/diagnostics contract tests。

### DA-TD3-003 Reusable deployment surfaces carry environment defaults

ID: DA-TD3-003
CATEGORY: Operations Portability
SEVERITY: TD3
PATH: `deploy/`; Bridge examples/configuration
SYMBOL: `re-anime.cc`, `Asia/Shanghai`, 1Panel/OpenResty path defaults

**EVIDENCE:** 单 VPS production topology 的域名、时区和宿主路径作为多个默认值出现。

**WHY THIS IS DEBT:** 默认值对当前生产合理，但复用 surface 与特定环境耦合。

**CURRENT IMPACT:** 当前 operator 流程清晰；alternate environment 需要显式覆盖。

**FUTURE IMPACT:** self-hosted/alternate deployment 可能继承错误 topology。

**AFFECTED:**
- Operations
- Production
- Bridge

**RECOMMENDATION:** 逐步参数化 reusable surfaces，保留当前 production 作为 documented example。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low-Medium。

**TEST REQUIREMENT:** alternate config fail-clear tests 与 current production config parity。

### DA-TD3-004 Legacy browser-token scrub has no removal cutoff

ID: DA-TD3-004
CATEGORY: Legacy Compatibility
SEVERITY: TD3
PATH: `src/lib/webAuthAdapter.js`; related auth cleanup paths
SYMBOL: legacy local/session token removal

**EVIDENCE:** Web adapter 仍清理历史 localStorage/sessionStorage token keys，没有 release/date sunset。

**WHY THIS IS DEBT:** 兼容代码仍有安全价值，但 ownership 期限未定义。

**CURRENT IMPACT:** 少量启动清理逻辑；不保存新 browser token。

**FUTURE IMPACT:** 无截止会让 legacy path 永久留存并增加 auth adapter noise。

**AFFECTED:**
- Web
- Auth

**RECOMMENDATION:** 选择支持截止 release/date，确认 supported builds 均不写 browser token 后删除。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low。

**TEST REQUIREMENT:** legacy cleanup 与 current HttpOnly-cookie/session tests。

### DA-TD3-005 Legacy `/api/` aliases have no sunset policy

ID: DA-TD3-005
CATEGORY: API Legacy Compatibility
SEVERITY: TD3
PATH: `backend/config/urls.py`; `backend/config/api_urls.py`
SYMBOL: canonical `/api/v1/` and compatibility `/api/` route mounts

**EVIDENCE:** canonical v1 与 legacy aliases 解析到相同 callbacks，但没有 telemetry/sunset date。

**WHY THIS IS DEBT:** 兼容面有意存在且测试充分，缺口只是 removal ownership。

**CURRENT IMPACT:** 双 route surface 增加少量 contract tests；没有 legacy-only behavior。

**FUTURE IMPACT:** 无 sunset 会延长 API migration 和 documentation burden。

**AFFECTED:**
- Web
- Mobile
- API

**RECOMMENDATION:** 文档化 deprecation telemetry、support window 与 removal criteria；禁止新增 legacy-only endpoints。

**FIX BEFORE V1:** NO

**ESTIMATED BLAST RADIUS:** Low-Medium。

**TEST REQUIREMENT:** canonical/legacy route parity、OpenAPI canonical-only、supported client migration tests。

## ARCHITECTURE COMPROMISE LEDGER

| Compromise | Why it likely happened | Current cost | Future cost | Decision | Target phase |
| --- | --- | --- | --- | --- | --- |
| Trusted in-process Backend Plugin SDK | 先交付可用官方插件与 capability enforcement | Django/Python ambient authority | Runtime v3 flag-day migration | Redesign | Architecture Debt Closure |
| Core 直接调用 hook registry | 快速保证 mutation 后触发插件 | 事务与 runtime lifecycle 耦合 | RPC/async semantics 推翻 | Redesign | Architecture Debt Closure |
| 多条 Journal mutation path | import、restore、sync 各自演进 | invariant/hook 不一致 | 新客户端继续复制规则 | Remove | Architecture Debt Closure |
| PENDING receipt 作为永久 claim | 简化并发幂等 | crash 后 request_id 卡死 | 多 worker 可靠性下降 | Redesign | Architecture Debt Closure |
| 外部 media write 持全局锁 | 简单保证 quota 精确 | 串行化、长事务 | R2 延迟放大 | Redesign | Architecture Debt Closure |
| Dashboard orchestration 集中 | 快速迭代且行为已冻结 | 修改 locality 较差 | UI/UX 2.0 成本增加 | Keep temporarily | v1.1 after unfreeze |
| Plugin manifest 多实现 validator | Python/Node/package 工具独立运行 | vocabulary 重复 | Manifest v3 drift | Consolidate | v1.1 |
| 服务器源码 build 与启动 migrate | 单 VPS 部署简单 | artifact identity 不稳定 | 无法 RC->Stable promote | Remove | Deployment/Updater Hardening |

## Wrong Abstractions

- 未发现必须删除的 `UniversalService`、`GenericManager` 或无价值五层 forwarding chain。
- `api.js -> web transport -> API core` 每层有明确变化隔离，不属于 architecture lasagna。
- 真正问题是 **under-engineered authoritative seam**：Journal mutation 和 Plugin RPC contract；不是“Service 不够多”。

## Duplicate Abstractions

- 重复 abstraction 的主要风险来自 Manifest validators 与 Dashboard server-query mirror，均为 TD2。
- 未发现需要在 v1.0 前删除的 forwarding-only service/client chain。

## Multiple Sources Of Truth And Duplicate Business Logic

- Journal mutation semantics：DA-TD1-003，必须 Closure。
- Dashboard query membership：DA-TD2-002，保持现有回归，后续减少手工 mirror。
- Plugin manifest vocabulary：DA-TD2-003，选择 canonical source。
- API/Auth error 与 resource identity mirrors 目前由 OpenAPI/contract tests 约束，属于 intentional contract mirror，不升级为 debt。

## Implicit Side Effects And HTTP Semantics

- Integration HMAC auth 的 `last_seen_at` 写入是命名之外的持久副作用，TD2。
- lazy `get_or_create` 的 user/security/settings 初始化具有明确 ownership 与测试，属于合理 lazy initialization。
- GET 没有发现破坏性 mutation；观测时间写入需要节流，但不属于安全或 correctness blocker。

## Transaction Boundary

- FAIL：Plugin hooks 在 Journal/account transaction 内同步执行，DA-TD1-002。
- FAIL：Media external I/O 在全局 row lock transaction 内执行，DA-TD1-005。
- PASS：External Sync 在获取远端数据后才进入本地 row lock，避免网络 I/O 持锁。
- PASS：官方 import-completed Integration event 使用 `transaction.on_commit(..., robust=True)`。

## HEAVY VIEW HOTSPOTS

- `JournalEntryViewSet` 同时承担 owner query、复杂 annotation、facets、filter semantics 和 delete hook，是 hotspot，但主要债由 DA-TD1-003 与 DA-TD2-002 覆盖。
- CSV import View 的跨行 transaction 绕过 authoritative mutation seam，属于 DA-TD1-003。
- 未发现 Serializer 执行外部 HTTP、Plugin dispatch 或大规模跨模型 workflow 的新增 TD1。
- Models 主要保存 invariant/state；没有 Fat Model 承担外部网络或 Runtime orchestration。

### Heavy Serializer Audit

- Serializer 仍以 validation、schema、representation 和 basic create/update 为主。
- CSV commit 的跨行 transaction 位于 View，问题归入 DA-TD1-003，而不是误判为 Fat Serializer。

### Model Fatness Audit

- Models 保存约束、identity 与 state machine 字段，没有直接执行外部 HTTP、Plugin Runtime orchestration 或大 workflow。
- `IntegrationActionReceipt` 的问题是 lifecycle 欠建模，不是 Model 方法过胖。

## Plugin Architecture

### Contract status

| Surface | Status | Notes |
| --- | --- | --- |
| Manifest v2 / permission enforcement | STABLE | Host 与 tests 强制声明和 actor binding |
| Journal/Watch History/Analytics DTO capability | STABLE for in-process v2 | JSON value shape 可保留 |
| Storage/settings persistence ownership | STABLE ownership | QuerySet interface 必须替换 |
| Hook names/order/failure mode | STABLE semantics candidate | transport/timing 需要 Closure |
| DRF request/Response handler contract | DEPRECATED for Runtime v3 | 只可作为 Web adapter |
| Django transaction/time/settings imports | INTERNAL leakage | 不应成为 public SDK |
| arbitrary `requests`/root path | DEPRECATED / INTERNAL | 迁移到 broker/provider |
| Frontend same-origin runtime | EXPERIMENTAL trusted runtime | isolation/theme slots deferred |

### Capability mapping

Manifest declaration、Host construction、actor binding 与 runtime enforcement 当前一一对应，未发现“只在 UI 展示但不执行”的 permission。问题在 interface granularity 与 ambient authority，不在 enforcement 缺失。

### Hook semantics

- transaction-critical candidate：`user.before_delete`，需要 fail-closed policy、严格超时、无网络。
- request-critical sync candidate：少量需要立即影响响应的 filter。
- async/post-commit candidate：journal after create/update/delete、user after delete、column after publish/delete、integration notifications。

## Mobile Readiness

API v1、error contract、resource identity、Auth Core 与 browser adapter separation 足以支持未来 Mobile，不需要重写 token/session Core。Mobile 仍需要 native refresh credential adapter 与 secure storage threat model，但这属于已知 implementation work，不是新的 TD1。

`MOBILE READINESS: PASS`

## UI/UX 2.0 Readiness

Core catalog DTO 与 Web presentation 没有反向依赖；UI/UX 2.0 可以演进。主要风险是 Dashboard orchestration concentration 和 frontend plugin 同源 CSS/Router coupling。它们已进入 TD2，Dashboard 在 v1.0 继续冻结。

`UI/UX 2.0 CORE READINESS: PASS`

`THEME CONTRACT / PLUGIN UI SLOT READINESS: FAIL`（deferred debt，不是 TD0）

## Deployment / Updater Readiness

当前 Release Gate 能从源码构建并验证 production-like Compose/stateful upgrade，但部署器仍在服务器 build images，API 容器启动时自动 migrate/sync，没有 GHCR digest、release manifest 或同 artifact promotion。它可以安全服务当前单 VPS，却尚未满足已确定的 Build Once Promote Many 与 restricted Update Agent 模型。

`DEPLOYMENT/UPDATER READINESS: FAIL`

修复明确属于后续 `Deployment / Updater Hardening`，本轮不实现。

## Legacy / Dead Code

- 没有发现可证明无 caller、删除后有明确价值的高风险 dead module。
- `/api/` aliases、legacy browser token scrub 是有意兼容，不是 dead code；缺少 sunset 归 TD3。
- 大型 test modules、Auth Views、Models 与 Plugin files 不能仅因 LOC 判债。

## Over-Engineering / Under-Engineering

- Over-engineering：未发现需要 v1.0 前删除的 repository/service/DTO 层堆叠。
- Under-engineering：Journal authoritative mutation seam、Plugin portable interface、Integration lease 和 Media reservation 是本轮主要问题。
- KISS：Closure 应优先移动 ownership、缩短 transaction、删除 ambient authority；只有在隔离真实变化时新增 interface。

## TOP 20 ARCHITECTURAL HOTSPOTS

`QUALIFYING HOTSPOTS: 17`

| Rank | Hotspot | Reason |
| --- | --- | --- |
| 1 | `plugin_host/runtime/context.py` + official backend plugin | Public SDK 与 Django/Python runtime 混合 |
| 2 | `journal/domain_services.py` + `plugin_host/hooks.py` | Core->Runtime 反向依赖、同步 callback |
| 3 | Journal mutation adapters | create/update/delete/import/restore/sync 多 seam |
| 4 | `integrations/services.py` receipt lifecycle | claim 无 lease、同步等待 |
| 5 | `site_config/media_storage/pool.py` | global lock 覆盖外部 I/O |
| 6 | `plugin_host/storage.py` | persistence ownership 正确但泄漏 QuerySet |
| 7 | `plugin_host/runtime/dispatch.py` | DRF request/Response 直接成为 plugin contract |
| 8 | `DashboardPage.jsx` | UI workflow coordinator concentration |
| 9 | `useDashboardData.js` | server/demo/cache/request generation 协调 |
| 10 | `dashboardMutation.js` + `entry_views.py` | client/server query semantics mirror |
| 11 | `plugin_host/views.py` | marketplace/developer/staff/deployment transport concentration |
| 12 | `plugin_host/services.py` | upload/review/publish/install/GC workflow concentration |
| 13 | Manifest validators | schema vocabulary 多实现 |
| 14 | Frontend Plugin Runtime | same-origin CSS/Router/theme coupling |
| 15 | Integration HMAC/event polling | hidden write + sync worker occupancy |
| 16 | AstrBot `EventState` | pending 无界与无 recovery |
| 17 | Compose/Dockerfiles/deployer | server build、startup migrate、artifact identity |

## Technical Debt Register

| Severity | IDs | Owner subsystem | Reason | Recommended phase | Dependency |
| --- | --- | --- | --- | --- | --- |
| TD0 | none | - | no structural blocker | - | - |
| TD1 | 001 | Plugin Platform | non-serializable SDK | Architecture Debt Closure B | portable DTO/error contract |
| TD1 | 002 | Core + Plugin | reverse dependency/transaction hook | Closure A/B | event/policy semantics |
| TD1 | 003 | Journal | multiple mutation seams | Closure A | authoritative domain mutation API |
| TD1 | 004 | Integration | orphan PENDING receipt | Closure C | additive lease model |
| TD1 | 005 | Media | external I/O under global lock | Closure D | reservation/finalization |
| TD2 | 001-002 | Frontend/Journal | Dashboard concentration/query mirror | v1.1 after unfreeze | stable E2E |
| TD2 | 003-004,007-008,012-013 | Plugin Platform | contract duplication, hotspots, RPC cost, pagination/UI | v1.1 | Closure B stable contract |
| TD2 | 005-006 | Integration | sync wait/write amplification | v1.1 | receipt semantics |
| TD2 | 009-010 | Test/Static | brittle source claims/limited rules | v1.1 | scoped measurement |
| TD2 | 011 | Deployment | no immutable promotion | Deployment/Updater Hardening | GHCR/release manifest |
| TD2 | 014 | Bridge | pending recovery | v1.1 | Protocol v1 unchanged |
| TD3 | 001-005 | CI/Operations/Web | maintenance and sunset policy | normal backlog | no functional dependency |

## Architecture Debt Closure Plan

5 项 TD1 按依赖拆为四个 Batch：A 收敛 Journal mutation 与 hook transaction seam；B 冻结可序列化 Plugin Host contract 并迁移官方插件；C 为 Integration receipt 增加 lease/recovery；D 将 Media write 改为 reservation/finalization。每个 Batch 的 PR、风险、migration、tests、stop conditions 与 production impact 见 `docs/architecture-debt-closure-plan-20260811.md`。

## V1.1 Backlog

14 项 TD2 和值得追踪的 5 项 TD3 已与既有 `docs/v1.1-technical-backlog.md` 去重：复用原有 Dashboard、Integration polling、Plugin OpenAPI/UI、testing、pagination 与 Bridge 条目；只新增 Dashboard query mirror、Plugin platform hotspot、Manifest canonical vocabulary、batch Host read、Integration observation write 与 immutable artifact readiness 条目。

## Architecture Quality Scorecard

`FAIL` 表示该 architecture dimension 有已确认 debt，不等于本次 Audit 执行失败。

```text
CORE ARCHITECTURE: PASS
DEPENDENCY DIRECTION: FAIL
API CONTRACT ARCHITECTURE: PASS
AUTH ARCHITECTURE: PASS
DOMAIN BOUNDARY: FAIL
TRANSACTION BOUNDARY: FAIL
FRONTEND STATE ARCHITECTURE: PASS
PLUGIN SDK ARCHITECTURE: FAIL
PLUGIN CAPABILITY ARCHITECTURE: PASS
PLUGIN RUNTIME V3 READINESS: FAIL
INTEGRATION ARCHITECTURE: FAIL
MOBILE READINESS: PASS
UI/UX 2.0 READINESS: PASS
DEPLOYMENT/UPDATER READINESS: FAIL
LEGACY DEBT: PASS
ABSTRACTION QUALITY: FAIL
COMPLEXITY: PASS
MAINTAINABILITY: FAIL
```

## Q1-Q10

**Q1：是否还有“现在能跑、v1.0 后一定会后悔”的设计？**

有。非序列化 Plugin SDK、Core transaction 内 runtime hook、Journal 多 mutation seam、无 lease receipt 和外部 I/O 持锁都具有明确放大路径。

**Q2：是否存在最后低成本修复窗口的妥协？**

有。Runtime v3 与 Marketplace 尚未扩张、Mobile 尚未成为第二客户端、Media 写入量仍有限；此时修 seam 比扩张后兼容多方消费者成本低。

**Q3：是否存在看似架构化、实际多余的层？**

没有发现需要 v1.0 前删除的显著 forwarding lasagna。当前主要问题是关键 seam 不够深，而不是层太多。

**Q4：同一业务规则是否在多处独立维护？**

有。Journal mutation semantics、Dashboard query membership、Plugin manifest vocabulary。前者 TD1，后两者 TD2。

**Q5：当前 Plugin SDK 能否自然迁移到 RPC/container？**

不能。DTO capability shape 可保留，但 request、QuerySet、transaction、Django user/settings、filesystem root、callable 与 arbitrary requests 必须先收口。

**Q6：Web/Auth/API 是否迫使 Mobile 重写核心逻辑？**

否。Auth Core、API Core、error/resource contract 已独立；Mobile 需要新 adapter，不需重写 token/domain semantics。

**Q7：UI/plugin coupling 是否会让 UI/UX 2.0 使插件失效？**

存在中等风险。Backend contract 不受影响，但 frontend same-origin CSS/Router coupling 缺少 theme/semantic slots，属于 TD2。

**Q8：部署结构是否阻塞 GHCR/RC promotion/Update Agent？**

是。当前 source-build/startup-migrate 模式不能满足 build-once-promote-many，已明确交给 Deployment/Updater Hardening。

**Q9：哪些是真技术债？**

所有 TD1、14 项 TD2，以及有明确 sunset/maintenance owner 的 5 项 TD3。它们均有代码证据、影响路径和目标阶段。

**Q10：哪些只是架构审美？**

大文件本身、Auth Views/Models/test modules 的 LOC、一次性 lazy `get_or_create`、现有 facade 层、目录命名和不影响行为的 service 风格偏好，均不值得 v1.0 前处理。

## Original Audit Acceptance Matrix (2026-08-11)

以下矩阵保留原始深度审计完成时的历史证据；当前 Closure 的最终 SHA 与门禁以 `docs/architecture-debt-closure-report-20260812.md` 为准。

```text
BASE SHA: 6452b3dbfff39529c49c2bc69ede1f3d76236eee
ORIGINAL AUDIT FINAL MAIN SHA: fc94deb553fb1471e0bdc5419ac94847a9a0c870
PRIMARY AUDIT MERGE SHA: 1446cbfafd1fbabaf2982ddd7dbc706817ae64be
REPORT FINALIZATION MERGE SHA: fc94deb553fb1471e0bdc5419ac94847a9a0c870

ARCHITECTURE MAP: PASS
DEPENDENCY DIRECTION: FAIL
CORE ARCHITECTURE: PASS
API CORE: PASS
WEB TRANSPORT: PASS
AUTH CORE: PASS
WEB AUTH ADAPTER: PASS
ANTI-ABUSE BOUNDARY: PASS
DOMAIN SERVICE BOUNDARY: FAIL
TRANSACTION BOUNDARY: FAIL
MULTIPLE SOURCES OF TRUTH: FAIL
DUPLICATE BUSINESS LOGIC: FAIL
WRONG ABSTRACTIONS: PASS
REDUNDANT ABSTRACTIONS: PASS
IMPLICIT SIDE EFFECTS: FAIL
GOD MODULE AUDIT: PASS
COMPLEXITY HOTSPOT AUDIT: PASS
DEAD CODE AUDIT: PASS
LEGACY COMPATIBILITY AUDIT: PASS
OVER-ENGINEERING AUDIT: PASS
UNDER-ENGINEERING AUDIT: FAIL
RESOURCE IDENTITY: PASS
ERROR ARCHITECTURE: PASS
PLUGIN SDK ARCHITECTURE: FAIL
PLUGIN CAPABILITY ARCHITECTURE: PASS
PLUGIN STORAGE BOUNDARY: FAIL
PLUGIN HOOK SEMANTICS: FAIL
PLUGIN RPC READINESS: FAIL
PLUGIN RUNTIME V3 READINESS: FAIL
INTEGRATION ARCHITECTURE: FAIL
BRIDGE ARCHITECTURE: FAIL
MOBILE READINESS: PASS
UI/UX 2.0 READINESS: PASS
THEME CONTRACT READINESS: FAIL
PLUGIN UI SLOT READINESS: FAIL
DEPLOYMENT/UPDATER READINESS: FAIL
BUILD-ONCE-PROMOTE-MANY READINESS: FAIL
TEST ARCHITECTURE: PASS
CI ARCHITECTURE: PASS

TD0 OPEN: 0
TD1 OPEN: 0
TD1 RESOLVED: 3
TD1 ACCEPTED EXCEPTIONS: 2
TD1 UNDECIDED: 0
TD2 DEFERRED: 14
TD3 DEFERRED: 5
TOP ARCHITECTURAL HOTSPOTS: 17
```

## Verification And Production Status

本轮只修改文档，因此执行 required architecture verification、文档一致性检查、现有 CI/Release Gate 证据核对和 `git diff --check`；不重复无意义的完整功能回归。上面的 2026-08-11 数字保留为原始审计快照；当前 closure 状态以本节 Closure disposition、`docs/architecture-debt-closure-report-20260812.md` 和 `docs/v1.0-remaining-td1-decisions-20260812.md` 为准。

- Node architecture/plugin/release contract：12 passed。
- Django architecture dependency、Plugin capability、Journal hook、Integration concurrency：14 tests，13 passed，1 skipped。
- Completion requirement matrix：PASS；24 findings × 14 required fields、18 architecture-map areas、Q1-Q10、Final Matrix、Closure/backlog scope 全部覆盖。
- Required headings、TD register/backlog definition uniqueness、trailing whitespace：PASS。
- `git diff --check`：PASS。
- main `6452b3dbfff39529c49c2bc69ede1f3d76236eee` CI：PASS，GitHub run `31489493491`。
- main `6452b3dbfff39529c49c2bc69ede1f3d76236eee` Release Gate：PASS，GitHub run `31489493488`。
- PR #51 CI：PASS，Release Gate：PASS。
- post-merge main CI：PASS，GitHub run `31499237274`。
- post-merge main Release Gate：PASS，GitHub run `31499237294`。
- PR #52 CI：PASS，Release Gate：PASS。
- report-finalization main CI：PASS，GitHub run `31502854586`。
- report-finalization main Release Gate：PASS，GitHub run `31502854526`。

原始审计矩阵中的 `ORIGINAL AUDIT FINAL MAIN SHA` 记录当时最后一个已完整通过 main CI/Release Gate 的审计树；本轮后续的 docs-only decision merge 已由 PR #59 合并，当前最终 main SHA 与对应门禁证据记录在 `docs/architecture-debt-closure-report-20260812.md`。

```text
NEW MIGRATION: NOT APPLICABLE
PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN
DATABASE PRODUCTION CHANGE: NOT APPLICABLE
R2 PRODUCTION CHANGE: NOT APPLICABLE
PLUGIN PRODUCTION CHANGE: NOT APPLICABLE
BRIDGE PRODUCTION CHANGE: NOT APPLICABLE
ASTRBOT / NAPCAT / OPENRESTY CHANGE: NOT APPLICABLE
CLOUDFLARE CHANGE: NOT APPLICABLE
DOCKER GLOBAL CHANGE: NOT APPLICABLE
```

## Final Gate

```text
FINAL STATUS:

DEEP ARCHITECTURE & TECHNICAL DEBT AUDIT: PASS
TD0 OPEN: 0
TD1 OPEN: 0
TD1 RESOLVED: 3
TD1 ACCEPTED EXCEPTIONS: 2
TD1 UNDECIDED: 0
V1.0 STRUCTURAL BLOCKERS: 0
ARCHITECTURE DEBT CLOSURE: PASS WITH ACCEPTED DEBT
ARCHITECTURE DEBT CLOSURE REQUIRED: PASS
ANI MEMO V1.0 NEXT STEP: Deployment / Updater Hardening
```
