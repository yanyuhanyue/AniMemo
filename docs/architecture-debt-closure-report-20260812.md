# AniMemo Architecture Debt Closure Report — 2026-08-12

## Result

本轮完成 5 项 TD1 的安全审计与 closure decision：DA-TD1-002、DA-TD1-003、DA-TD1-005 已 RESOLVED；DA-TD1-001、DA-TD1-004 已正式记录为 ACCEPTED V1.0 DEBT EXCEPTION。没有擅自改变 Plugin SDK、Integration Protocol 或 receipt replay 语义。

```text
REMAINING TD1 DECISION: PASS
ARCHITECTURE DEBT CLOSURE: PASS WITH ACCEPTED DEBT
V1.0 STRUCTURAL BLOCKERS: 0
```

生产部署、生产 smoke、SSH、生产数据库/R2/Cloudflare 操作均未执行。

## TD1 acceptance matrix

| Finding | 状态 | 实现 / 决策 | 证据 |
| --- | --- | --- | --- |
| DA-TD1-001 Plugin SDK/runtime 非可序列化边界 | ACCEPTED V1.0 DEBT EXCEPTION | v1.0 保持 SDK API 2、Manifest v2 与 trusted in-process runtime；Runtime v3 前以 versioned SDK v3/Host adapter 迁移，不改写 official plugin 0.4.2 | `docs/v1.0-remaining-td1-decisions-20260812.md`；Plugin SDK contract/runtime boundary；runtime/capability/package gates |
| DA-TD1-002 Core 直接依赖 Plugin Runtime/事务内开放 hook | CLOSED | `JournalMutationContext`、policy port、`transaction.on_commit` event port；策略同步、开放事件提交后执行 | PR #55，`backend/journal/mutation_ports.py`，89 项 targeted tests |
| DA-TD1-003 Journal 缺少唯一 mutation seam | CLOSED | Journal create/update/delete 统一进入 `JournalEntryService`；CSV、Bundle、External Sync、External Account/Media adapter 改走 application seam | PR #54，adapter AST guard，84 项 targeted tests |
| DA-TD1-004 PENDING receipt 无 lease/takeover | ACCEPTED V1.0 DEBT EXCEPTION | v1.0 保持 at-most-once safety-first：PENDING bounded wait + 409、terminal-only replay、PENDING 不清理；action replay policy 与 additive lease/takeover 进入 trigger-based v1.1 remediation | `docs/v1.0-remaining-td1-decisions-20260812.md`；`docs/integration-protocol-v1.md` §Action receipt；PostgreSQL concurrency proof |
| DA-TD1-005 Media external IO 持有全局 DB lock | CLOSED | additive `MediaWriteReservation`：短事务 reserve → 事务外 adapter write → 短事务 finalize；配额计入 active reservation，过期对账不删远程对象 | PR #56，`site_config/migrations/0002_media_write_reservation.py`，媒体套件 + PostgreSQL gate |

## Compatibility and migration

- `MediaWriteReservation` 是 additive migration；历史 `MediaObject`、`media-objects/<uuid>` reference、storage adapter API 保持不变。
- 活动 reservation 保护 backend physical identity；物理写入不在 pool/backend 行锁事务内执行。
- `reconcile_media_write_reservations` 只把过期 pending 标为 `abandoned`，不自动删除 R2/远程对象；远程孤儿必须进入后续 operator acceptance。
- Plugin SDK 与 Integration receipt 的公共语义没有未经 ADR 的 breaking change。
- `DA-TD1-001` 与 `DA-TD1-004` 的例外不降低 TD1 严重度；它们分别等待 Runtime v3 portability trigger 与 action replay/recovery trigger。

## Verification

- A1 targeted suite：84 passed；A2 targeted suite：89 passed。
- Media storage suite：54 passed，5 个 PostgreSQL-only concurrency tests 在 SQLite 本地环境跳过。
- CI classifier self-test：6 passed；workflow YAML parse：PASS。
- PR #54：CI + Release Gate 全部通过并 squash merge。
- PR #55：CI + Release Gate 全部通过并 squash merge。
- PR #56：changed-files、fast-fail、frontend、backend、PostgreSQL、plugin、Bridge/runtime、bootstrap、Docker、stateful-upgrade 全部通过并 squash merge。
- main after report merge：`ed6a673`；CI run `31514756504` PASS（昂贵 product jobs skipped），Release Gate `31514756716` PASS（仅 `post-merge-sanity`，Docker/stateful skipped）。

## Remaining decisions

1. `DA-TD1-001` 的 Accepted Exception 触发条件、兼容约束与 expected remediation 见 `docs/v1.0-remaining-td1-decisions-20260812.md`；它不是 v1.0 blocker，也不是已完成 Runtime v3。
2. `DA-TD1-004` 的 Accepted Exception 继续禁止无 replay contract 的 lease takeover；触发后必须先定义 action replay policy，再实现 additive recovery。
3. 进入 Production Acceptance 前，单独审查 media reservation 的 crash/orphan operator runbook；本轮不执行远程清理。

## Remaining TD1 Closure

| Finding | Final state | Target phase | Trigger |
| --- | --- | --- | --- |
| DA-TD1-001 | ACCEPTED V1.0 DEBT EXCEPTION | v1.1 Plugin Runtime v3 preparation | worker/container/RPC、untrusted backend publisher 或独立资源隔离 |
| DA-TD1-004 | ACCEPTED V1.0 DEBT EXCEPTION | v1.1 Integration reliability / Runtime v3 preparation | crash recovery、多 worker、长任务/异步 action 或自动 retry |

```text
TD0 OPEN: 0
TD1 RESOLVED: 3
TD1 ACCEPTED EXCEPTIONS: 2
TD1 UNDECIDED: 0
TD2 DEFERRED: 14
TD3 DEFERRED: 5
```

## Final acceptance matrix

```text
BASE MAIN SHA: df51876f0311beec52159edd4cf33028110d78c8
FINAL MAIN SHA: df51876f0311beec52159edd4cf33028110d78c8
PRODUCTION STABLE BASELINE: 6452b3dbfff39529c49c2bc69ede1f3d76236eee

DA-TD1-001: ACCEPTED V1.0 DEBT EXCEPTION
DA-TD1-004: ACCEPTED V1.0 DEBT EXCEPTION

TD0 OPEN: 0
TD1 RESOLVED: 3
TD1 ACCEPTED EXCEPTIONS: 2
TD1 UNDECIDED: 0
TD2 DEFERRED: 14
TD3 DEFERRED: 5

PLUGIN SDK CONTRACT: PASS
PLUGIN SDK BREAKING CHANGE: NOT APPLICABLE
RECEIPT REPLAY CONTRACT: PASS
RECEIPT BREAKING CHANGE: NOT APPLICABLE
API CONTRACT: PASS
AUTH CONTRACT: PASS
RESOURCE IDENTITY: PASS

FRONTEND TESTS: NOT APPLICABLE (docs-only fast path)
BACKEND TESTS: NOT APPLICABLE (docs-only fast path)
PLUGIN GATES: NOT APPLICABLE (docs-only fast path)
INTEGRATION TESTS: NOT APPLICABLE (docs-only fast path)
BRIDGE TESTS: NOT APPLICABLE (docs-only fast path)
CRITICAL E2E: NOT APPLICABLE (docs-only fast path)
RUFF: NOT APPLICABLE (docs-only fast path)
LINT: NOT APPLICABLE (docs-only fast path)
DJANGO CHECK: NOT APPLICABLE (docs-only fast path)
MIGRATION CHECK: NOT APPLICABLE (docs-only fast path)
OPENAPI: NOT APPLICABLE (docs-only fast path)

NEW MIGRATION: NOT APPLICABLE
PR FAST GATE: NOT APPLICABLE
MERGE GROUP FULL REGRESSION: NOT RUN
RELEASE GATE: PASS
POST-MERGE LIGHTWEIGHT VERIFY: PASS

PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN

FINAL STATUS:
REMAINING TD1 CLOSURE: PASS WITH ACCEPTED DEBT
ARCHITECTURE DEBT CLOSURE: PASS WITH ACCEPTED DEBT
ANI MEMO V1.0 NEXT STEP: Deployment / Updater Hardening
```

`FINAL MAIN SHA` is the last verified merged main tree. The remaining changes in this task are docs-only working-tree changes; they are not represented as a new main SHA until a normal review/merge occurs. Release Gate and post-merge lightweight evidence are inherited from the verified `df51876` main closure tree; no new code or migration gate was required for this decision-only update.
