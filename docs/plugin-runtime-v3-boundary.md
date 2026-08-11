# Plugin Runtime v3 Boundary

本阶段只记录 Runtime v3 的契约输入，不实现 Worker、Container 或 RPC 迁移。当前可发布运行时仍是受信任的 in-process Python Runtime Plugin。

## Current Invariants

- `slug + version` 是不可变 package identity；CAS 保存实际 archive bytes。
- Runtime registry 以一个 active candidate 对应一个已发布版本，切换失败会恢复 previous candidate。
- USER installation 与 actor-bound capability 在每次调用时校验；禁用插件立即撤销已绑定能力。
- Manifest、package index、backend entry 与 official package immutability gate 共同阻止越界文件、未声明能力和同版本内容改写。
- Plugin storage 只能通过 namespaced、bounded、retention-aware Host storage 使用；Core journal/watch-history/analytics 不提供 generic database capability。

## Deferred v3 Inputs

未来隔离运行时需要明确 serializable RPC message、trusted publisher/runtime identity、source commit provenance、canonical digest、review/revocation/rollback、filesystem/network/secret/resource limits，以及 Host-owned clock、logging 与 transactional batch contract。它们不属于本批次实现。

## Verification

Runtime/manifest/capability tests 与 plugin static gates 必须同时通过。Release Gate 继续验证 fresh install、stateful upgrade、official package identity、runtime reconciliation 与 existing plugin data；本阶段不部署生产，也不执行生产 mutation smoke。
