# AniMemo Architecture Debt Closure Report — 2026-08-12

## Result

本轮完成 5 项 TD1 的安全审计与可实施部分：DA-TD1-002、DA-TD1-003、DA-TD1-005 已合并；DA-TD1-001、DA-TD1-004 按 stop condition 保持 BLOCKED，没有擅自改变公共契约或 receipt replay 语义。

生产部署、生产 smoke、SSH、生产数据库/R2/Cloudflare 操作均未执行。

## TD1 acceptance matrix

| Finding | 状态 | 实现 / 决策 | 证据 |
| --- | --- | --- | --- |
| DA-TD1-001 Plugin SDK/runtime 非可序列化边界 | BLOCKED | DTO + Host operations 需要 Plugin SDK breaking/versioning ADR；本轮不改公共 SDK | closure plan 与 Plugin SDK 现状审计 |
| DA-TD1-002 Core 直接依赖 Plugin Runtime/事务内开放 hook | CLOSED | `JournalMutationContext`、policy port、`transaction.on_commit` event port；策略同步、开放事件提交后执行 | PR #55，`backend/journal/mutation_ports.py`，89 项 targeted tests |
| DA-TD1-003 Journal 缺少唯一 mutation seam | CLOSED | Journal create/update/delete 统一进入 `JournalEntryService`；CSV、Bundle、External Sync、External Account/Media adapter 改走 application seam | PR #54，adapter AST guard，84 项 targeted tests |
| DA-TD1-004 PENDING receipt 无 lease/takeover | BLOCKED | 当前协议只保证 pending 期间不重放副作用，且明确不删除 pending；没有 action replay contract，不能安全 takeover | `docs/integration-protocol-v1.md` §Action receipt |
| DA-TD1-005 Media external IO 持有全局 DB lock | CLOSED | additive `MediaWriteReservation`：短事务 reserve → 事务外 adapter write → 短事务 finalize；配额计入 active reservation，过期对账不删远程对象 | PR #56，`site_config/migrations/0002_media_write_reservation.py`，媒体套件 + PostgreSQL gate |

## Compatibility and migration

- `MediaWriteReservation` 是 additive migration；历史 `MediaObject`、`media-objects/<uuid>` reference、storage adapter API 保持不变。
- 活动 reservation 保护 backend physical identity；物理写入不在 pool/backend 行锁事务内执行。
- `reconcile_media_write_reservations` 只把过期 pending 标为 `abandoned`，不自动删除 R2/远程对象；远程孤儿必须进入后续 operator acceptance。
- Plugin SDK 与 Integration receipt 的公共语义没有未经 ADR 的 breaking change。

## Verification

- A1 targeted suite：84 passed；A2 targeted suite：89 passed。
- Media storage suite：54 passed，5 个 PostgreSQL-only concurrency tests 在 SQLite 本地环境跳过。
- CI classifier self-test：6 passed；workflow YAML parse：PASS。
- PR #54：CI + Release Gate 全部通过并 squash merge。
- PR #55：CI + Release Gate 全部通过并 squash merge。
- PR #56：changed-files、fast-fail、frontend、backend、PostgreSQL、plugin、Bridge/runtime、bootstrap、Docker、stateful-upgrade 全部通过并 squash merge。
- main after merge：`47f404b`；post-merge workflow 以该 SHA 的最终运行结果为准。

## Remaining decisions

1. 为 Plugin SDK 选择 versioned DTO/Host API 的 breaking boundary，并迁移官方插件后，才能解除 DA-TD1-001。
2. 为每类 integration action 写明 at-most-once、可重试/可 replay 语义；在此之前不为 DA-TD1-004 增加 lease takeover。
3. 进入 Production Acceptance 前，单独审查 media reservation 的 crash/orphan operator runbook；本轮不执行远程清理。
