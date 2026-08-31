# Release Notes 主分类权威

每个面向用户的 Pull Request 在合并时必须且只能保留一个 `release/*` 主分类；使用 `release/internal` 或 `skip-changelog` 排除时必须保持零个主分类。唯一代码策略位于 `release/primary_category.py`，PR Fast、Trusted Pre-Merge、Release Producer、renderer、本地 operator audit 与后续只读验证均复用该策略；Freshness 只验证 Qualification 冻结的同一份权威，不再查询 live labels 或生产第二份分类。

Release Drafter 只负责根据已存在的显式标签渲染草稿，不再根据标题、分支或路径写入主分类。分类决策由任务合同、已封存证据或人工审计产生；标题和路径只能作为人工决策的输入，不能成为可叠加的权威。

## v1.1.x 后续收敛

当前 RC19 在 Release Preflight 冻结完整 PR population，并让后续阶段消费 run-scoped Artifact。后续 v1.1.x 应在 trusted merge 边界持久化不可变分类 receipt，使长期 Release Notes 不依赖合并后仍可变化的 live labels。迁移时可组合：

- 绑定 PR、merge commit、唯一主分类和排除语义的 merge-time receipt；
- 截止 `merged_at` 的 label event-ledger replay；
- 仅用于历史异常、经过审阅并纳入版本控制的 legacy override ledger。

在新的 ledger 合同完整覆盖历史版本前，禁止用启发式自动写入多个主分类，也不得把 Release Drafter 的 `exclusive` 展示规则当作标签集合互斥保证。
