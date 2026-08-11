# AniMemo Architecture Debt Closure Plan

计划日期：`2026-08-11`

基线：`6452b3dbfff39529c49c2bc69ede1f3d76236eee`

来源：`docs/deep-architecture-technical-debt-audit-20260811.md`

## Purpose

本计划只承接深度架构审计确认的 5 项 TD1。它不是 UI/UX 2.0、Mobile、Plugin Marketplace、Runtime v3、Deployment/Updater 的实现计划，也不授权在当前 Audit 分支中偷偷完成大规模重构。

```text
TD0 OPEN: 0
TD1 OPEN: 5
ARCHITECTURE DEBT CLOSURE REQUIRED: PASS
PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN
```

## Closure Rules

- 每个 Batch 使用独立中文 commit 与 PR 标题。
- 先写 contract/acceptance tests，再做 scoped implementation。
- 需要公共 API、Plugin SDK、Auth/security、identity 或 migration decision 时停止自动修改，先记录 ADR/决策。
- 不做 destructive migration；receipt/media 只能 additive migration。
- 每个 Batch 完成后跑对应 subsystem regression；四个 Batch 完成后跑完整 CI/Release Gate。
- 全部 required gates PASS、PR mergeable 且无 blocking review 后，可自动 Squash Merge；仍不部署生产。

## Dependency Graph

```text
Batch A: Journal mutation + hook transaction seam
       |
       +--> Batch B: Portable Plugin SDK / Runtime boundary

Batch C: Integration receipt lease  (independent, shares protocol tests)
Batch D: Media reservation/finalization (independent, migration + cleanup)
```

## Batch A — 收敛 Journal Mutation 与 Hook Transaction Seam

### Covers

- `DA-TD1-002` Core Domain 反向依赖 Plugin Runtime、同步 hook 在事务内运行。
- `DA-TD1-003` Journal mutation 多 authoritative seam。

### Proposed PRs

#### PR A1：建立 Journal authoritative mutation contract

建议中文标题：`收敛 Journal 领域变更入口与适配器契约`

**Scope**

- 由一个 domain/application seam 拥有 Journal create/update/delete 的 invariant、owner check、DTO 与 mutation result。
- CSV import、Data Bundle restore、External Sync apply、Plugin capability 均通过显式 mode 调用，不直接 `JournalEntry.objects.create/save/delete`。
- Data Bundle 的 external identity/watch history orchestration 继续留在 bundle domain，但 Journal row mutation 通过 seam 完成。
- 保留 soft-delete 与现有 public ID；不重写 migration history。

**Risk**

High。导入、同步、插件、DELETE 和 hook 触发次数都会变化；错误处理和 rollback 需要逐路径确认。

**Migration**

默认无新 migration。若需要记录 restore/sync source，只能 additive nullable metadata，不在同一 PR 强行改变 public DTO。

**Tests**

- API create/update/delete 与 legacy alias parity。
- CSV preview/commit、Data Bundle restore、External Sync apply、Plugin create/update 的 invariant/event parity。
- owner isolation、soft-delete、rollback、duplicate guard、concurrent update。
- hook contract 断言同一 mutation 只产生一次对应 event。

**Production impact**

不部署生产；需要 future production acceptance 才能验证真实 importer/sync data。

#### PR A2：拆分 transaction-critical policy 与 post-commit plugin events

建议中文标题：`隔离 Plugin Hook 事务策略与提交后事件`

**Scope**

- `user.before_delete` 等必须在 commit 前决定的策略使用小型、可超时、无网络的 policy interface。
- `journal.after_*`、`user.after_delete`、`column.after_*` 默认在 `transaction.on_commit` 后发布不可变 DTO。
- 明确 open/closed failure policy、ordering、deduplication、missing-runtime 行为。
- Domain 不再 import `plugin_host.hooks`；只依赖 domain-owned event/policy port，runtime 负责 adapter。

**Risk**

High。改变 callback 观察时序，必须逐个 hook 标记 criticality。

**Migration**

无数据库 migration；可能需要 registration/event metadata 的 additive fields。

**Tests**

- commit/rollback/`on_commit` ordering。
- slow callback 不持有业务 row lock。
- closed/open failure policy、runtime unavailable、duplicate delivery。
- account deletion final-superuser/2FA/owner isolation regression。

**Production impact**

不执行生产 hook smoke；必须在后续 Production Acceptance 重放 operator-approved scenario。

## Batch B — Portable Plugin SDK 与 Runtime v3 Boundary

### Covers

- `DA-TD1-001` Backend Plugin SDK 非 RPC 可序列化。
- `DA-TD1-002` 的 runtime adapter 部分。

### Proposed PRs

#### PR B1：冻结 JSON-compatible Host SDK DTO

建议中文标题：`冻结 Plugin Host 可序列化能力契约`

**Scope**

- request/response/error/context 只允许 JSON-compatible primitives、arrays、objects、stable IDs 和 ISO timestamps。
- 去掉 plugin-facing `request`、DRF `Response`、Django User/Model、QuerySet、callable、filesystem root 的公共暴露。
- 新建 adapter 将 Web request 转换为 `PluginRequestDTO`，将 plugin result 转换为 Host response。
- `PluginStorage.collection()` 改为 bounded list/get/set/delete/query DTO，不暴露 QuerySet。

**Risk**

High。官方插件、Integration action、测试 fixture 和 dynamic route response 都受影响。

**Migration**

需要 SDK versioning/compatibility decision；不自动创建“万能兼容 wrapper”。若必须保留过渡期，需 ADR、明确截止版本和 package validator。

**Tests**

- AST gate 禁止 official plugin 导入 Django/DRF/requests/private runtime 与使用 QuerySet。
- serialization round-trip、error code/status、size limits、actor binding。
- official package validate/build/package/immutability/runtime/integration。

#### PR B2：迁移官方 Watch History Importer

建议中文标题：`迁移官方观看记录插件至可移植 Host 能力`

**Scope**

- 用 Host settings/storage/journal/watch_history/integration DTO 替换直接 Django transaction、DRF response/status、settings、requests。
- `Bangumi` 网络调用改经 `host.http` 或 Official Provider；保留现有 timeout、response limit、normalization 语义。
- `history-get` 列表查询改为 batch/read-model capability，避免未来 N+1 RPC。

**Risk**

High。官方插件 package identity 不能无意变更；需要版本策略。

**Migration**

若 DTO/SDK breaking，必须提升 SDK/package version 并通过 official immutability gate；禁止覆盖已发布 `0.4.2`。

**Tests**

- importer preview/resolve/select/commit/history-get/history-add。
- Bangumi error/timeout/normalization。
- package SHA/manifest/runtime/install/enable/disable。

#### PR B3：Hook transport adapter

建议中文标题：`为 Plugin Hook 增加提交后事件适配层`

**Scope**

- 对接 Batch A 的 policy/event ports。
- 保持 hook name/payload/failure semantics，允许当前 in-process adapter 与未来 worker/RPC adapter 并存。

**Risk / Tests**

Medium-High；复用 Batch A ordering/failure tests，补 runtime reconcile/unload/rollback。

## Batch C — Integration Receipt Lease 与恢复

### Covers

- `DA-TD1-004` PENDING receipt crash recovery。

### Proposed PR

建议中文标题：`为 Integration 动作回执增加租约接管与恢复`

**Scope**

- `IntegrationActionReceipt` additive fields：`claimed_at`、`lease_until`、`claim_token`、`attempt_count`（最终字段名以 ADR 为准）。
- claim 使用条件更新；过期 PENDING 可安全接管；完成/失败只允许当前 claim token 写入。
- cleanup/diagnostics 处理过期 PENDING；明确 retry 返回 `request_in_progress`、`request_reclaimable` 或重新执行的 contract。
- Bridge 不改变 Protocol v1 cursor/ACK 语义。

**Risk**

Medium-High。错误 lease 可能导致重复外部副作用；必须先定义哪些 action 可 replay。

**Migration**

Additive migration only。旧 PENDING 行按 `created_at` 计算兼容 lease；不得删除或重写现有 receipt history。

**Tests**

- crash-after-claim fixture。
- 双 worker claim/lease takeover/stale finalize。
- completed/failed replay、cleanup、stateful-upgrade 与 PostgreSQL concurrency。
- HMAC idempotency 与 action response size limits。

**Production impact**

不执行生产清理或数据库修改；迁移后需要单独 Production Acceptance 和 operator recovery runbook。

## Batch D — Media Reservation / Finalization

### Covers

- `DA-TD1-005` 外部 media write 在全局 row lock 内。

### Proposed PR

建议中文标题：`将媒体写入改为预留与提交两阶段`

**Scope**

- 短事务内锁定 pool/backend、预留 quota 与 immutable object identity。
- 事务外执行 local/R2 write；短事务 finalize MediaObject 与 quota。
- 失败、超时、进程 crash 由 idempotent release/expiry/cleanup 处理。
- backend selection、preferred backend 与 existing media key contract 保持兼容。

**Risk**

High。涉及 quota correctness、孤儿对象、R2/local failure semantics。

**Migration**

可能需要 additive reservation model/fields；禁止重写历史 MediaObject、禁止生产 R2 清理作为开发步骤。

**Tests**

- concurrent quota reservation、same backend contention、backend failover。
- physical write fail/timeout、finalize retry、reservation expiry、orphan cleanup。
- local adapter/R2 adapter、upload API、avatar/poster/media admin regression。

**Production impact**

必须在后续 Production Acceptance 执行受控 upload smoke；本轮不碰 R2 或生产数据库。

## Closure Ordering And Gates

1. A1/A2：Journal mutation 与 hook semantics；必须先稳定 event/policy contract。
2. B1/B2/B3：Plugin DTO、官方插件迁移与 runtime adapter。
3. C：Receipt lease 可独立实施，但需与 Plugin Integration action tests 对齐。
4. D：Media reservation；migration 与 cleanup 需单独审查。

每个 PR：

```text
targeted tests: PASS
subsystem regression: PASS
lint/build/ruff/check/migrations/openapi: PASS
plugin/bridge gates where affected: PASS
CI: PASS
Release Gate: PASS
mergeable/no blocking review: PASS
```

四个 Batch 合并前的 Major Phase Gate：

```text
FRONTEND TESTS / CRITICAL E2E / LINT / BUILD: PASS
BACKEND TESTS / DJANGO CHECK / MIGRATION CHECK / OPENAPI: PASS
PLUGIN GATES: PASS
BRIDGE TESTS / VALIDATE / PACKAGE / RUNTIME: PASS
FULL REGRESSION: PASS
CI + RELEASE GATE + POST-MERGE: PASS
PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN
```

## Stop Conditions

遇到下列任一情况，停止自动实现并要求明确 ADR/用户决策：

- Plugin SDK breaking version、public API breaking change、Auth/security semantics change。
- Resource identity 或 migration history 需要重写。
- receipt action 是否可 replay 无法由当前 product contract 决定。
- Media reservation 需要 destructive R2/DB cleanup。
- 需要 SSH、生产数据库/R2/Bridge/Cloudflare/Docker 全局操作。

## Exit Criteria

```text
TD0 OPEN: 0
TD1 OPEN: 0
All TD1 acceptance tests: PASS
No new migration drift: PASS
CI + Release Gate + post-merge: PASS
Production deploy: NOT RUN until separate acceptance phase
```

完成以上条件后，下一步才是 `Deployment / Updater Hardening`，而不是在 Closure 中顺便实现 Mobile、Marketplace 或 UI/UX 2.0。

## Execution Update (2026-08-12)

本轮执行结果记录在 `docs/architecture-debt-closure-report-20260812.md`：DA-TD1-002、DA-TD1-003、DA-TD1-005 CLOSED；DA-TD1-001、DA-TD1-004 因 stop condition BLOCKED。PR #54/#55/#56 均已通过 CI/Release Gate 并 squash merge；生产部署与 smoke NOT RUN。
