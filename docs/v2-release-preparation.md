# v2.0 发行准备

当前产品目标为 `v2.0.0` / `DURABLE_DEPLOYMENT_REFERENCE` / **PREPRODUCTION_ONLY**。版本选择不授予 production lifecycle；首个正式生产里程碑仍须满足完整 Memory、运行、安全、备份恢复与发布质量门，计划编号是 v2.2.0。完整范围见 [Master Roadmap v12](AniMemo_更新路线_v12_版本路线对齐.md)。

## 当前版本来源和下一次参数

Release Drafter 只生成变更草稿；它的 `tag_name` 和 `target_commitish=main` 不能证明 Git ref、候选源码或发布事务。保持真实 `release/breaking` 标签。

正式 Producer 的 `.github/workflows/release.yml` 在 qualify preflight 从完整 `git tag --list v*` 与 `release/publication-reservations.json` 调用 `release.cli resolve-version` → `release.contract.resolve_prerelease`。当前唯一 Stable tag 为 v1.0.0，因此下一次获准 Producer 必须显式选择 **version_bump=major、channel=rc**；workflow 通用默认 patch 保留，不能把默认值或旧 minor 当作此次执行参数。已有 Stable 时禁止 bootstrap-only `target_version_override`。

2026-09-09 只读 tag refs 与已发布对象、仓库 publication reservations 对账后，resolver 结果是 `targetVersion=v2.0.0`、`releaseTag=v2.0.0-rc.1`。这不是锁号：动态任务开始时重新读取全部 refs、已发布对象、预留和可能未调和的写事务，再解析；不得从历史 v1.1.0-rc.19 推算新序号。

旧事务、旧报告、已发布 v1.0.0 与 v1.x RC 保留原身份。草稿 ID 373784357 是普通变更草稿，必须保持 draft=true、published_at=null、assets=[]；不得将它当作真实 Git tag 或已占用 RC 事务。

Candidate VM harness 与 R2 Origin 收据使用同一 canonical RC parser，从 verified Candidate 的实际 RC 推导 base、隔离目录、prefix 和对象键。它们不再限定 v1.1.0；仍拒绝 Stable/Beta、非规范编号或 base/prefix 不匹配。历史 rc14 专用入口保留其历史身份，不用于此次新目标。

## 消费链与证明边界

- Qualification 使用最终 exact source；Candidate 验证器绑定 candidate version/source/tree/制品 identity。
- Freshness 的实际三个必需输入及 Aggregate Receipt 以当前 workflow 和 [Candidate 合同](candidate-acceptance-contract-v1.md) 为准。
- publish 从 Candidate Acceptance Receipt 读取目标及 RC 身份，不能重新任意选号。Release Notes 使用准确 previous Stable 边界与冻结 PR population。
- RC → Stable 仍由 Promotion 验证相同 base、准确 RC authority 和既有 artifacts，零重建；编号不替代任何签名、摘要、准入或生产资格。

API v1、Plugin SDK v2、Release Manifest schema 2、数据库 `animemo-db-v1`、配置 `animemo-config-v1`、`v1.1-instance-scoped` profile 和 installer profile 均保持自己的版本。历史冻结合同中的 v1.1 产品措辞由本映射承接，其协议标识不改。

本次只做准备。VM/Clone/Guest、sudo、Qualification/Candidate/Freshness、Tag/Release/Attestation/GHCR/R2/Mirror/Formal/Stable 均待新的明确授权。
