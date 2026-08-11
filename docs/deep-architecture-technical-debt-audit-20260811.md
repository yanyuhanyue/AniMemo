# AniMemo v1.0 深度架构与技术债审计

审计日期：`2026-08-11`

## Executive Summary

本轮从 `6452b3dbfff39529c49c2bc69ede1f3d76236eee` 审计 AniMemo 当前架构。该提交也是审计开始时的 `main` 与 `origin/main`；`e68dc0c` 是其祖先提交，已被当前基线完整包含。

现有 API v1、Auth Core/Web Adapter、稳定资源身份、Plugin capability enforcement、Integration Protocol v1 和前端共享 Core 均有有效 contract 与测试保护。没有发现需要推翻 v1.0 公共 contract、身份模型或数据模型的 TD0 Structural Blocker。

审计确认 5 项 TD1。它们集中在四条 seam：Journal mutation、Plugin Host/RPC、Integration receipt lifecycle 与 Media write reservation。当前功能与安全门禁可以继续通过，但这些 seam 若留到 Runtime v3、Marketplace 或更高媒体写入量之后再处理，迁移成本会明显放大。因此本轮结论是审计成功、架构债需要单独 Closure，不在 Audit PR 中静默重构。

```text
TD0 OPEN: 0
TD1 OPEN: 5
TD2 DEFERRED: 14
TD3 DEFERRED: 5

DEEP ARCHITECTURE & TECHNICAL DEBT AUDIT: PASS
ARCHITECTURE DEBT CLOSURE REQUIRED: PASS
ANI MEMO V1.0 NEXT STEP: Architecture Debt Closure
```

## Scope And Method

- 审计范围：Frontend、Backend、Auth、API、Domain Services、Models、Serializers、Plugin Host、Frontend/Backend Plugin Runtime、官方插件、Integration、AstrBot Bridge、R2/Media、External Accounts、Staff/Admin、CI、Release、部署脚本。
- 方法：代码和 import direction 阅读、事务与副作用追踪、contract/test 覆盖核对、热点与历史兼容清单、当前 GitHub Gate 证据核对。
- 判断口径：module、interface、seam、adapter、depth 与 locality；LOC 只作为调查信号，不作为 verdict。
- 未执行：生产部署、生产 mutation smoke、SSH、数据库/R2/Bridge/Cloudflare/Docker 全局变更。

## Current Architecture Map

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
| API | `/api/v1/`, OpenAPI, error codes | serializers, query construction | DRF Views, Web transport | Web, future Mobile |
| Auth | token/session semantics | `auth_tokens.py` | `web_auth_adapter.py`, cookies/CSRF/Turnstile | Web, future native adapter |
| Journal | DTO and mutation semantics | `JournalEntryService`, Watch History services | ORM, import/sync adapters | Web, Plugin capabilities |
| Plugin | Manifest v2, SDK v2, capabilities/hooks | registry, installer, marketplace workflow | in-process runtime, package CAS | official/third-party plugins |
| Integration | Protocol v1, HMAC, actions/events/receipts | dispatch and receipt state | DRF, database polling | AstrBot Bridge, providers |
| Media | media object identity and pool policy | pool selection | local/R2 adapters | upload surfaces |
| Release | CI and Release Gate | scripts and fixtures | Docker Compose/deployer | operator |

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

**AFFECTED:** Web、Plugin、Integration、Runtime v3、Production。

**RECOMMENDATION:** 冻结 JSON-compatible request/response/error DTO；用 Host storage operations 取代 QuerySet；用显式 transaction/batch capability 取代 `django.db.transaction`；由 `host.http` broker 执行网络；官方插件先迁移为 reference consumer。

**FIX BEFORE V1:** YES

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

**AFFECTED:** Web、Plugin、Runtime v3、Production。

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

**AFFECTED:** Web、Mobile、Plugin、Integration、Runtime v3、Production。

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

**AFFECTED:** Integration、Plugin、Runtime v3、Production。

**RECOMMENDATION:** 通过 additive fields 引入 lease/claimed_at/attempt；过期 lease 可原子接管；完成写入验证当前 lease owner；cleanup/diagnostics 处理孤儿 PENDING；明确 handler 是否允许重放。

**FIX BEFORE V1:** YES

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

**AFFECTED:** Web、Plugin、UI/UX、Production。

**RECOMMENDATION:** 建立短事务 reservation/finalization seam：锁内预留 quota 与 backend identity，锁外写物理对象，短事务 finalize；失败时幂等释放 reservation/清理孤儿对象。

**FIX BEFORE V1:** YES

**ESTIMATED BLAST RADIUS:** High；Media pool、quota、R2/local adapters、cleanup 与 migrations。

**TEST REQUIREMENT:** quota race、reservation expiry、write failure/failover、orphan cleanup、concurrent upload、backend mutation 与 PostgreSQL regression。

## TD2 Findings

以下 14 项有真实维护成本，但不构成 v1.0 Structural Blocker。Dashboard 项继续服从 Freeze 规则。

| ID | Category / Path / Symbol | Evidence and debt | Current/Future impact | Recommendation / Phase / Tests |
| --- | --- | --- | --- | --- |
| DA-TD2-001 | Frontend state; `DashboardPage.jsx`, `useDashboardData.js` | 页面同时协调 URL filter、server state、demo persistence、selection、modals、optimistic mutation 和 plugin navigation。职责多但行为测试充分。 | UI/UX 2.0 修改 locality 较差。 | Dashboard 解冻后按 domain ownership 拆分；保留 query/mutation E2E。`FIX BEFORE V1: NO`，blast radius high。 |
| DA-TD2-002 | Duplicate business rule; `dashboardMutation.js`, `entry_views.py` | optimistic matcher 手工镜像 search/status/tag/year/activity/quick filter/sort；`needs-attention` 已有不同 poster 判定表达。 | 后端筛选新增语义时客户端 count/visible reconciliation 易漂移。 | 让服务器返回 mutation membership/authoritative page hint，或生成共享 contract vectors。v1.1，contract + E2E。 |
| DA-TD2-003 | Multiple truth; `manifest.py`, `plugin.schema.json`, `validate-plugins.mjs`, `pluginctl.py` | extensions、capabilities、hooks、roles 和交叉约束多处手写。 | Manifest v3/Marketplace 扩展会产生 validator drift。 | 选择 schema/contract metadata 为 canonical source，其他 validator 消费或 parity test。v1.1，all-validator fixtures。 |
| DA-TD2-004 | Complexity hotspot; `plugin_host/views.py`, `services.py` | 市场、开发者上传、审核、发布、安装、预览、扫描、GC 共用两个高变更 module。不是单纯 LOC 问题。 | Marketplace 扩展会降低 change locality。 | 按 marketplace/developer/review/deployment/package lifecycle 拆 module，不新增 forwarding layer。v1.1，workflow regressions。 |
| DA-TD2-005 | Integration performance; `integrations/services.py` | `_wait_for_receipt()` 使用 `time.sleep(0.05)` 轮询 DB；event long poll 同样占用同步 Django worker。 | Bridge/consumer 增加后占 worker 与数据库连接。 | 评估 ASGI wakeup、Redis/pubsub 或 bounded short-poll。v1.1，protocol/cursor/ACK load tests。 |
| DA-TD2-006 | Hidden write; `integrations/authentication.py` | 每次成功 HMAC authentication 都更新 `last_seen_at`，包括 GET event polling。 | 高频 polling 产生持续写放大与 row contention。 | 节流/异步观测写入，不改变认证结果。v1.1，auth replay 与 observability tests。 |
| DA-TD2-007 | API contract; dynamic plugin routes | `/api/v1/plugins/{slug}/...` 明确排除 Core OpenAPI，插件 route 只有 runtime validation。 | Mobile/Marketplace 工具无法发现 plugin-owned contract。 | 定义独立、版本化的 Plugin OpenAPI artifact。v1.1，schema validation。 |
| DA-TD2-008 | Frontend plugin/UI | 官方 frontend plugin 同源注入 CSS/React/Router，依赖宿主 component/style conventions；Theme/Semantic Slot 未建立。 | UI/UX 2.0 可能破坏第三方插件外观或路由假设。 | Trusted publisher ADR、semantic slots、theme tokens；不在 v1.0 Dashboard 内重写。v1.1，host/plugin visual and routing contract tests。 |
| DA-TD2-009 | Test architecture | 多个测试用 source text/regex 断言运行行为。静态 package/security contract 合理，但行为 claim 较脆。 | 重构会制造无行为回归的 test churn。 | 逐项把 behavior claim 转为 runtime test，保留真正静态 contract。v1.1。 |
| DA-TD2-010 | Static quality | ESLint 关闭 unused rules 且未启用 `react-hooks/exhaustive-deps`；Ruff 只跑 fatal families。 | 隐性依赖/死赋值较晚暴露。 | 小范围启用并测量误报，不做全仓风格改写。v1.1。 |
| DA-TD2-011 | Deployment/Updater | Compose 在服务器从源码 build；API container 启动自动 migrate/sync；没有 GHCR digest/release manifest/promote same artifact。 | 明确阻塞 Build Once Promote Many 与 restricted Update Agent。 | 进入 Deployment/Updater Hardening；构建 immutable API/Web images，独立 migration job，manifest/digest promotion。不是 Audit PR。 |
| DA-TD2-012 | Plugin capability granularity | `history-get` 在列表分支对每个 entry 调 `list_history`。 | RPC 后变成 N+1 Host calls。 | 提供 batch/read model capability 或一次查询 DTO。v1.1，call-count test。 |
| DA-TD2-013 | Pagination | marketplace、installed、developer project/version list 未分页。 | Marketplace 数据增长后响应和 query 不受界。 | 稳定排序和兼容分页。v1.1，query-count/response bound tests。 |
| DA-TD2-014 | Bridge recovery; `EventState.pending_event_ids` | pending deque 无 maxlen、age、dead-letter 或 operator recovery；一个缺失 route 可长期阻塞 cursor。 | 长期 route 故障会扩大 state file 并阻塞后续进度。 | 有界 pending、expiry/dead-letter、诊断与恢复命令。v1.1，overflow/route restoration tests。 |

所有 TD2 的 `AFFECTED`、owner 与依赖已写入 Technical Debt Register 和 `docs/v1.1-technical-backlog.md`。其中 DA-TD2-011 的 target phase 是 Deployment/Updater Hardening，而不是普通 v1.1 cleanup。

## TD3 Findings

| ID | Evidence / impact | Recommendation |
| --- | --- | --- |
| DA-TD3-001 | GitHub Actions 仍使用 checkout v4/setup-python v5/setup-node v4；Dependabot 已有 major PR。当前 Gate 正常。 | 常规依赖维护窗口升级；`FIX BEFORE V1: NO`；CI/Release Gate 验证。 |
| DA-TD3-002 | root `package.json` version 为 `0.0.0`，但 Git/Plugin release identity 另有 contract。 | 记录 authoritative application version policy；不是运行时 bug。 |
| DA-TD3-003 | `re-anime.cc`、Asia/Shanghai、1Panel/OpenResty 路径存在部署默认值。当前单环境合理。 | 可复用面逐步参数化，保留当前生产作为示例。 |
| DA-TD3-004 | Web adapter 仍清理 legacy local/session token，没有移除截止。 | 设兼容 release/date 后删除；当前是安全性兼容代码。 |
| DA-TD3-005 | `/api/` compatibility aliases 与 `/api/v1/` 并存，没有 sunset。 | 文档化 deprecation telemetry/截止；禁止新增 legacy-only route。 |

上述 TD3 的 `CATEGORY` 为 Cosmetic/Low-value compatibility，`AFFECTED` 主要是 CI、Operations、Web transport；`ESTIMATED BLAST RADIUS` low-medium，均 `FIX BEFORE V1: NO`。

## Architecture Compromise Ledger

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

## Wrong And Duplicate Abstractions

- 未发现必须删除的 `UniversalService`、`GenericManager` 或无价值五层 forwarding chain。
- `api.js -> web transport -> API core` 每层有明确变化隔离，不属于 architecture lasagna。
- 真正问题是 **under-engineered authoritative seam**：Journal mutation 和 Plugin RPC contract；不是“Service 不够多”。
- 重复 abstraction 的主要风险来自 Manifest validators 与 Dashboard server-query mirror，均为 TD2。

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

## Heavy View / Serializer / Model Audit

- `JournalEntryViewSet` 同时承担 owner query、复杂 annotation、facets、filter semantics 和 delete hook，是 hotspot，但主要债由 DA-TD1-003 与 DA-TD2-002 覆盖。
- CSV import View 的跨行 transaction 绕过 authoritative mutation seam，属于 DA-TD1-003。
- 未发现 Serializer 执行外部 HTTP、Plugin dispatch 或大规模跨模型 workflow 的新增 TD1。
- Models 主要保存 invariant/state；没有 Fat Model 承担外部网络或 Runtime orchestration。

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

## Top Architectural Hotspots

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

## Final Acceptance Matrix

```text
BASE SHA: 6452b3dbfff39529c49c2bc69ede1f3d76236eee
FINAL MAIN SHA: 1446cbfafd1fbabaf2982ddd7dbc706817ae64be

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
TD1 OPEN: 5
TD2 DEFERRED: 14
TD3 DEFERRED: 5
TOP ARCHITECTURAL HOTSPOTS: 17
```

## Verification And Production Status

本轮只修改文档，因此执行 required architecture verification、文档一致性检查、现有 CI/Release Gate 证据核对和 `git diff --check`；不重复无意义的完整功能回归。

- Node architecture/plugin/release contract：12 passed。
- Django architecture dependency、Plugin capability、Journal hook、Integration concurrency：14 tests，13 passed，1 skipped。
- Required headings、TD register/backlog definition uniqueness、trailing whitespace：PASS。
- `git diff --check`：PASS。
- main `6452b3dbfff39529c49c2bc69ede1f3d76236eee` CI：PASS，GitHub run `31489493491`。
- main `6452b3dbfff39529c49c2bc69ede1f3d76236eee` Release Gate：PASS，GitHub run `31489493488`。
- PR #51 CI：PASS，Release Gate：PASS。
- post-merge main CI：PASS，GitHub run `31499237274`。
- post-merge main Release Gate：PASS，GitHub run `31499237294`。

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
TD1 OPEN: 5
V1.0 STRUCTURAL BLOCKERS: 0
ARCHITECTURE DEBT CLOSURE REQUIRED: PASS
ANI MEMO V1.0 NEXT STEP: Architecture Debt Closure
```
