# AniMemo Restore Contract v1

**Status:** FROZEN FOR v1.1

**Version:** v1

**Scope:** 冻结从 Backup Format v1 恢复 AniMemo instance 的 VERIFY → COMPATIBILITY PLAN → RESTORE → VALIDATE 状态机、destination 分类、Release/Updater 交互、失败边界和 Memory Integrity 验证。

**Definitions:** Restore 是让一个 verified instance Backup 在经过兼容性裁决后成为可运行 AniMemo instance；`PUBLISHED` 表示 target 通过全部验证后被发布，`RECOVERY_REQUIRED` 表示 destructive restore mutation 已开始但未安全发布。

**Non-goals:** 本文不实现 Restore CLI/runtime、不执行生产恢复、不定义 in-place destructive restore、不实现 Backup、Migration、Export、Compatibility engine、Secret Envelope、Doctor 或全局 atomic rollback。

**Dependencies:** 直接依赖 Backup Contract v1 和 Compatibility Matrix v1；继承 Phase 1 Deployment Boundary、Filesystem Layout、Installer、Public Origin / Listen Contract，并与 Migration Bundle、Migration Secret Envelope、Doctor 接口一致。

**Security / Integrity implications:** Restore 会写数据库、protected config、plugin/media state 与 Updater recovery state，必须默认 fail closed；在 authentication epoch rotation 和全部验证通过前不得向公网提供服务。

**Compatibility:** 只允许 `COMPATIBLE`、`REQUIRES_UPGRADE`、`UNSUPPORTED`、`CORRUPT`。未知但完整、可认证的 format 是 `UNSUPPORTED`；structure、checksum、authentication 或 member integrity 失败是 `CORRUPT`。
**Change policy:** destination eligibility、compatibility outcome、Release Authority、database import、secret、R2、Updater state、publish 或 failure semantics 的变化必须提升 Contract 版本，并与 Backup、Compatibility Matrix、Release、Updater 和 Memory Integrity 联合评审。

## 1. Contract map

### Phase 1 dependencies

- [Deployment Boundary v1](deployment-boundary-v1.md)：Restore 只修改 AniMemo-owned roots/services，不接管 DNS/TLS/proxy/firewall。
- [Filesystem Layout v1](filesystem-layout-v1.md)：定义 target roots、ownership、可重建状态和 delete safety。
- [Installer Contract v1](installer-contract-v1.md)：定义 destination/locator collision；Restore 不把 foreign data 当空安装。
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)：恢复 application identity，不修改管理员公网基础设施。

### Phase 2 contracts

- [Backup Contract v1](backup-contract-v1.md)：定义 Restore 唯一接受的正式 artifact。
- [Restore Contract v1](restore-contract-v1.md)：本文。
- [Migration Bundle v1](migration-bundle-v1.md)：instance movement，不是 Restore shortcut。
- [Migration Secret Envelope v1](migration-secret-envelope-v1.md)：Issue #87 的 envelope/reference、KDF/AEAD 与 secret redaction。
- [Compatibility Matrix v1](compatibility-matrix-v1.md)：四态判定的跨组件 authority。
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)：preflight、recovery-required 与 post-restore 只读诊断。

[Release Contract v1](release-contract-v1.md) 和 [Update Agent v1](update-agent-v1.md) 继续拥有 exact Release verification、migration compatibility、CURRENT/PREVIOUS 与 operation recovery 语义。

## 2. Semantic separation

- **Backup:** 创建 instance disaster recovery artifact。
- **Restore:** 从 verified Backup 恢复 instance。
- **Migration:** 移动 instance identity/state。
- **Export:** portable user memory/data。

Restore 不接受 Staff export、Data Bundle、CSV、live PostgreSQL directory、Docker volume tar、source tree ZIP 或 Updater 单独的 pre-migration database safety backup作为完整 instance Backup。

## 3. Memory Integrity invariants

Restore 必须证明：

- **MI-1 — External metadata disappearance:** external metadata 缺失、为空或 provider 暂不可用时，Restore 仍保留用户记忆和最后已知 metadata/source；不得把缺失解释为删除指令。
- **MI-2 — Provider identity change:** provider ID、canonical URL、账号或 backend identity 改变时，Restore 不得静默 orphan memory；必须保留 stable internal identity 与历史 binding，未知映射进入 compatibility/repair。
- **MI-3 — Identity merge:** Restore 或后续 migration/upgrade 发生 identity merge、deduplication、canonicalization 时，全部历史 references、watch history、metadata provenance 和 user relations 必须保留。
- **MI-4 — Unsupported memory preservation:** 当前 release 不支持的 memory/schema/plugin payload 不得被静默丢弃、默认化或部分导入；保留原 state，并判定 `REQUIRES_UPGRADE` 或 `UNSUPPORTED`。
- **MI-5 — Destructive ambiguity:** destination、ownership、identity、member 或 merge 存在 destructive ambiguity 时必须 fail closed 或进入绑定 exact evidence 的显式 repair；不得猜测删除、覆盖、合并或重建。

任何 MI invariant 无法证明都不能输出 `PUBLISHED`。

未来 validation fixtures 必须分别证明：external metadata 缺失不会删除 memory；provider identity 改变不会 orphan stable relation；identity merge 保留旧 references/history；unsupported memory bytes 不被丢弃；ambiguous target/identity/member 必须 fail closed 或进入 explicit repair。只验证首页可访问或总 row count 不满足 MI-1..MI-5。

## 4. State machine

唯一 v1 状态机：

```text
VERIFY
  → COMPATIBILITY PLAN
  → RESTORE
  → VALIDATE
      → PUBLISHED
      → RECOVERY_REQUIRED
```

`VERIFY` 与 `COMPATIBILITY PLAN` 必须是零破坏 mutation：允许读取 backup、Release Authority、target metadata、host facts 和 external dependency status，不得创建/清空数据库、覆盖 target、停止 existing foreign service 或写 target locator。

只有 compatibility outcome 为 `COMPATIBLE`，或 `REQUIRES_UPGRADE` 且操作者显式接受完整、machine-readable upgrade plan 时，才能进入 RESTORE。

`CORRUPT` 和 `UNSUPPORTED` 是拒绝执行的 compatibility outcomes，不是可绕过 warning。

## 5. Destination classification

| Destination | Definition | v1 behavior |
| --- | --- | --- |
| Fresh | Canonical target roots/locator 均不存在，parent 安全且可创建 | 允许进入 plan；RESTORE 时创建 staging |
| Existing empty | 已验证 AniMemo-owned empty roots，尚无 published locator/database/state | 允许；必须证明 empty，不能按目录名猜测 |
| Existing instance | Matching locator 与已安装/运行 instance 存在 | 默认拒绝；future destructive restore 需要独立强确认、safety backup 与 rollback design |
| Foreign | 路径或服务属于非 AniMemo/另一 instance | 拒绝；不得停止、移动、adopt、overwrite 或删除 |
| Partial / ambiguous | locator、roots、Compose、systemd、database 或 state 不完整/冲突 | 拒绝并交给 Doctor；不得自动 reset 或从 env 猜测 |

v1 正式 Restore 默认只支持 Fresh 与 Existing empty。Existing instance destructive restore 是 future interface，本阶段不实现。

## 6. VERIFY

VERIFY 按顺序检查：

1. Backup root/object identity、`backup-manifest.json`、`checksums.sha256` 和 immutable `backupId`。
2. Format/schema：已知 v1 strict schema；未知 schema 但结构、authentication 和 checksum 可验证时为 `UNSUPPORTED`。
3. Member paths、allowlist、size/SHA、gzip、tree digest、canonical artifact binding record/digest、secret envelope authentication 与 external reference shape。
4. Database dump metadata、PostgreSQL/tool compatibility 与 non-empty logical plain SQL gzip。
5. Source instance/release/deployment/database/config/plugin identity 内部一致性。
6. Local media coverage、plugin CAS/durable inventory、private/updater selective state。
7. R2 captured/reference-dependent coverage 与 required external dependencies。
8. Destination classification、host/architecture/filesystem capacity 与 owner/mode feasibility。

以下为 `CORRUPT`：缺 member、claimed-v1 structure invalid、checksum mismatch、gzip failure、path traversal、duplicate/colliding path、secret authentication failure、Manifest 与 member identity 冲突。

以下为 `UNSUPPORTED`：完整的未知 format、缺少受支持 parser/tool、不可解析的 external secret reference、没有受支持平台/architecture，或有效 backup 所需的 release/dependency 没有安全路径。

VERIFY 不得 “修复” Manifest、重算 checksum 后接受、忽略 unknown claimed-v1 member 或从 filesystem 猜缺失 metadata。

## 7. COMPATIBILITY PLAN

Compatibility Plan 必须 machine-readable，并绑定：

- exact `backupId` 与 checksum-set digest；
- recomputed `artifactBindingDigest` 与 authenticated Envelope binding；
- target instance/destination classification；
- source Backup Format、release/deployment、database/config 与 plugin identities；
- target Restore tool、Updater、OS/architecture、PostgreSQL 与 supported release；
- secret mode/dependency、local/R2 media coverage；
- required release acquisition、database import、migration/bootstrap、filesystem restore、authentication rotation 与 validation steps；
- predicted terminal outcome和所有 operator confirmations。

四态定义：

### COMPATIBLE

Backup v1 完整有效；target 支持 format/platform；正式 Release Authority 可提供 verified exact application/deployment bytes；target app 直接接受 restored database/config contracts 和 enabled Plugin APIs；required secret/media dependencies 可用。

### REQUIRES_UPGRADE

Backup 完整有效，但不能由最终 target release 直接读取；存在从 source/recovery-compatible exact release 开始、由正式 manifests 证明的单调、有限、非 `breaking-blocked` migration path。Plan 必须逐跳列出 release、database/config transition、minimum Updater 和验证点。

### UNSUPPORTED

Backup 完整有效，但没有安全的 format parser、platform、secret/media dependency、exact release 或 migration path；或 plugin/runtime requirement 超出受支持边界。不得通过强制 flag 跳过。

### CORRUPT

Artifact structure、authentication、checksum、database stream 或 member identity 失败。不得进入 RESTORE。

未知不能归类为 “probably works”。Classification engine 由 [Compatibility Matrix v1](compatibility-matrix-v1.md) 冻结。

## 8. Restore plan acceptance

进入 RESTORE 前必须：

- 重新确认 backup checksum 未改变；
- 重新确认 destination 仍为 Fresh/Existing empty；
- 获取 AniMemo-scoped exclusive restore operation lock；
- 确认磁盘容量、target canonical paths 与 staging parent；
- 提供所需 secret envelope key/KMS authorization 或验证 external secret reference；
- 确认 exact Release Authority 可用；
- 对 `REQUIRES_UPGRADE` 显示并确认全部 migration steps；
- 记录 operation ID、backup ID、plan digest 与 operator intent。

Plan/cache/先前 rehearsal 不替代执行边界 re-verification。

## 9. RESTORE execution order

推荐且冻结的逻辑顺序：

```text
Acquire exact release/deployment bytes from Release Authority
→ Create private staging roots
→ Resolve protected config/secret into staging
→ Create fresh staged PostgreSQL target
→ Import database.sql.gz with fail-on-error
→ Restore plugins/local media/private allowlist/selective updater state
→ Execute approved migration path when required
→ Run idempotent bootstrap
→ Rebuild Redis/cache/runtime state
→ Rebuild target instance locator and scoped systemd/Compose alignment
→ Rotate authentication epoch
→ VALIDATE
→ Publish target
```

各步骤必须可由 operation journal 区分；发生失败不得声称全局 atomic rollback。

## 10. Release Authority and application bytes

- `/opt/animemo` application/deployment material从 GitHub Release + GHCR exact OCI digest 重建。
- Backup 中的 release metadata、embedded Manifest 或 cached image 都不是第二 Release Authority。
- 必须重新验证 tag/Release metadata、Manifest、checksums、deployment contract、provenance、attestation 与 exact digest。
- source release 无法从 authority 验证时，只有 Compatibility Matrix 证明可使用 recovery-compatible release 才能成为 `REQUIRES_UPGRADE`；否则是 `UNSUPPORTED`。
- Restore 不得使用 latest、mutable tag、source ZIP、backup 自报版本或本地 image cache绕过验证。

多年后离线 restore 是否受支持，以及是否需要新的 signed offline Release archive，属于 `RELEASE CONTRACT REVIEW NEEDED`；本 Contract 不把 Backup 改造成 release source。

## 11. Database restore

- 在 Fresh/staged PostgreSQL database 中导入 `database.sql.gz`；不得 pipe 到 active production database。
- 使用 fail-on-error import，并验证导入进程完整消费 gzip stream。
- 导入前 target database 必须为空；schema/object 已存在视为 destination conflict。
- `COMPATIBLE` 路径使用接受 source database/config contract 的 verified app。
- `REQUIRES_UPGRADE` 只执行 Plan 列出的 forward migration；不 reverse migrate、不跳过 intermediate contract、不伪造 migration applied state。
- Migration failure 后 target 保持 stopped 和 `RECOVERY_REQUIRED`；不得自动 restore 另一 database 或回写 Backup。

Redis 不从 Backup 恢复。Redis/cache/throttle state 重建会导致 cache 与限流窗口重置，但不能改变 authoritative database data。

## 12. Filesystem restore

所有 payload 先进入 target-owned private staging：

- protected config：验证 schema/mode，secret-bearing bytes 不落入 world/group-readable path；
- plugins：恢复 required CAS 与 durable content，验证 DB package references/digests；`runtime/` 从 DB deployment rows 与 verified CAS 重建；
- local media：恢复 path/bytes/mode，验证 MediaObject reference、size 与 SHA；
- private：只恢复 Manifest allowlist；`setup-code` 不从 Backup 恢复；
- Updater：只恢复 selective durable state，排除 cache/credentials/locks/socket。

不得恢复 app binaries、`postgres/` physical data、Redis、logs、nested backups 或 runtime socket。Unknown/extra member 不得静默丢弃后继续 publish。

## 13. Locator and Updater state

Backup 中的 source locator 是 evidence，不是 target locator bytes。Restore 必须根据：

- target canonical app/data/updater roots；
- target deployment profile；
- verified exact release identity；
- target listen/Public Origin；
- 实际 Compose 与 systemd allowlist

重建并原子发布 target `instance.json`。不得盲拷 source absolute paths、symlink identity 或旧 systemd allowlist。

Selective Updater state 必须通过其原生 schema 读取。Restore 不得：

- 合成或伪造 CURRENT/PREVIOUS；
- 把 unverifiable embedded Manifest 导入 CURRENT；
- 清空 operation history/PENDING transition；
- 重放 migration、bootstrap 或 update operation；
- 绕过 Updater fail-closed/reconcile barrier。

只有 exact Manifest/deployment identity 经 Release Authority 重新验证，且 restored runtime contracts 与 slots 一致时，才可采用对应 CURRENT/PREVIOUS。无法解释的 pending transition使 target保持 `RECOVERY_REQUIRED`。

## 14. Secret restoration

- 在 destructive mutation 前验证 envelope authentication/reference availability。
- Secret 只解密到 private staging 或直接注入受保护 target config；不得输出 plaintext。
- 必须恢复 encrypted database credentials所需的原 `CREDENTIAL_ENCRYPTION_KEY`，或执行另行冻结的 intentional re-encryption plan。
- 不得使用该 key 解密/加密包含其自身的 envelope。
- Host GitHub/GHCR credential 在 target 人工重新配置，不从 ordinary Backup 恢复。
- Secret reference 在 plan 后失效时，停止 Restore并进入 `RECOVERY_REQUIRED`；不得清空 database ciphertext。

Secret transport 和 redaction 服从 [Migration Secret Envelope v1](migration-secret-envelope-v1.md)。

## 15. Media and R2

### Local media

必须在 publish 前验证：

- DB stable reference 能解析到同一 MediaObject UUID；
- backend/object key 未被猜测或重写；
- file exists、size/SHA 匹配；
- owner/mode 和 approved local root 正确。

### R2 captured

只恢复 Manifest 中由 stable MediaObject identity 拥有的 exact object。目标 key 已存在时：

- bytes/checksum 相同可以 idempotent accept；
- bytes 不同必须停止，不能覆盖或重命名规避 identity conflict。

### R2 reference-dependent

验证同一 external backend/bucket/object dependency。Dependency 缺失时保留数据库 MediaObject rows与 stable references，不改写为 local/default media，不删除 metadata。若在 VERIFY 已知缺失则 `UNSUPPORTED`；若 RESTORE 中途失效则 `RECOVERY_REQUIRED`。

任何模式都不得 bulk-delete prefix、枚举并删除 unknown orphan，或因为对象不在当前 restored database 就推断删除权限。

## 16. Bootstrap, authentication and PENDING state

数据库 import及 approved migration 完成后运行 idempotent bootstrap：

- initialized source 必须保持 setup locked，不签发新管理员或新 setup code；
- uninitialized source 由 existing first-run state machine安全生成/轮换一次性 code；
- stale plaintext setup-code 必须清理。

在 API/Web 对公网或管理员 proxy 可达前，必须运行：

```text
python manage.py rotate_authentication_epoch --confirm-restore
```

并证明旧 snapshot access token、refresh token与 Django session均失效。

以下 PENDING 状态保留其冻结语义：

- Updater operation/pending contract transition；
- IntegrationActionReceipt；
- MediaWriteReservation。

Restore 不得静默完成、删除、重放、generic timeout takeover 或把 PENDING 改为 success。恢复后的既有 reconcile/maintenance contract决定后续人工处理。

## 17. VALIDATE

VALIDATE 至少覆盖：

1. PostgreSQL import完整、schema/migration check和database/config contract。
2. exact Release/deployment/API/Web image identity与正式 Manifest一致。
3. target locator、canonical roots、owner/mode、Compose、systemd allowlist一致。
4. protected config可解析，required encrypted credential可解密但不输出。
5. Redis/cache/runtime已重建，不含 source lock/socket。
6. Updater slots/runtime/operation state可读写，PENDING/recovery barrier保持。
7. plugins package CAS/runtime/deployment与 enabled Plugin SDK identity一致。
8. local/R2 media按coverage验证，stable references不变。
9. MI-1..MI-5 representative graph与invariant检查。
10. InstallationState/setup lock、旧 token/session rejection、新 login/refresh。
11. API/Web local health和durable post-restore write probe。
12. Public Origin/listen application identity一致；不要求 Restore 修改 DNS/TLS/proxy。

只有全部 required validation PASS，才可原子发布 locator/service activation boundary并输出 `PUBLISHED`。

## 18. Failure model

### Pre-restore failure

发生在 VERIFY/COMPATIBILITY PLAN/plan acceptance。必须零破坏 mutation，保留 source Backup与target现状，返回四态 classification或stable error。

### Mid-restore failure

发生在 staging、database import、filesystem/secret/media restore或migration。Target保持stopped；记录exact completed step、backup/plan digest和redacted evidence；终态为 `RECOVERY_REQUIRED`。

### Post-restore validation failure

数据已写入但尚未 publish。不得启动公网服务、不得伪造health、不得自动reverse migration或宣称回到原状态。终态为 `RECOVERY_REQUIRED`。

### After publish

`PUBLISHED` 后的运行故障属于新的instance operation。Restore Contract不承诺global atomic rollback；需要新的verified Backup/Restore plan或既有Updater recovery语义。

任何失败：

- 不修改 Backup；
- 不删除existing/foreign data或external object；
- 只可清理被本operation ID证明拥有的staging；
- 不打印secret/env value/token/credential；
- generic age/timeout不授权删除或takeover。

## 19. Future adoption / destructive restore interface

未来若支持 Existing instance restore，必须另行冻结：

- exact matching instance proof；
- service quiescence和pre-restore safety backup；
- explicit destructive confirmation绑定instance ID、backup ID和plan digest；
- old/new roots的publish boundary；
- database/filesystem/locator/systemd协调；
- failure后保留原instance或进入可诊断recovery state；
- retention与cleanup authority。

该future adoption interface不得被Installer的“目录存在”、`--force` 或手工删除实现。本轮默认拒绝Existing instance、Foreign和Partial/ambiguous destination。

## 20. CURRENT → TARGET gap

| Area | CURRENT | TARGET | Classification |
| --- | --- | --- | --- |
| Fresh A→B proof | 已有isolated logical restore rehearsal | 继续作为Restore Contract evidence | ALREADY SATISFIED |
| Empty target guard | 旧helper要求empty target | Formal destination classification | ALREADY SATISFIED |
| Authentication epoch | 命令与端到端test已存在 | publish前mandatory gate | ALREADY SATISFIED |
| Automatic restore | Updater明确不自动restore | 继续fail closed | ALREADY SATISFIED |
| Canonical state machine | 当前脚本是串行rehearsal | VERIFY→PLAN→RESTORE→VALIDATE | DOCUMENTATION GAP |
| Four-state compatibility | 当前Release只裁决live switch | Backup-aware Compatibility Matrix | IMPLEMENTATION DEFERRED |
| Locator/config roots | Phase 1只冻结future interface | rebuild target locator/config | IMPLEMENTATION DEFERRED |
| Database staging/journal | 当前shell外部执行fresh import | durable Restore operation state | IMPLEMENTATION DEFERRED |
| Selective filesystem/updater state | 当前helper整树复制 | strict allowlist与native schema adoption | IMPLEMENTATION DEFERRED |
| Secret envelope/reference | 当前rehearsal依赖预置相同secret | authenticated restore flow | IMPLEMENTATION DEFERRED |
| R2 | 当前未演练 | captured/reference-dependent validation | IMPLEMENTATION DEFERRED |
| Staff/Data Bundle wording | 有限Export历史使用backup/restore名称 | canonical docs 明确为Export/Import；未来产品文案继续收敛 | DOCUMENTATION GAP |
| Exact source release unavailable | 当前rehearsal使用当前candidate | offline/recovery-compatible authority policy | RELEASE CONTRACT REVIEW NEEDED |
| Existing instance destructive restore | 无安全contract | future explicit adoption interface | IMPLEMENTATION DEFERRED |
| Full runtime | 无Restore CLI/runtime | future implementation conforming to this Contract | IMPLEMENTATION DEFERRED |

旧 `scripts/dr_backup.py` / DR rehearsal 是noncanonical evidence，其差异不构成Phase 1 canonical Contract冲突，也不能直接成为Restore runtime。

## 21. Future contract tests

未来实现至少必须增加：

- `COMPATIBLE`、`REQUIRES_UPGRADE`、`UNSUPPORTED`、`CORRUPT` 的 table-driven vectors 与固定聚合结果；
- VERIFY/PLAN 对 Fresh、Existing empty、Existing instance、Foreign、Partial/ambiguous destination 的零 mutation证明；
- checksum/authentication/structure failure不创建target，未知完整format判为`UNSUPPORTED`；
- exact Release重新验证，latest/cache/embedded Manifest不能绕过authority；
- database只导入fresh staging，import/migration failure进入`RECOVERY_REQUIRED`且不reverse migrate；
- locator从target事实重建，CURRENT/PREVIOUS不能伪造，PENDING Updater operation/Integration receipt/MediaWriteReservation不replay/delete；
- config/plugin/local media/selective Updater state的owner/mode/digest与exclusion验证；
- R2 captured conflict、reference dependency missing和unknown orphan preservation；
- publish前authentication epoch rotation，旧JWT/refresh/session拒绝，新login/refresh与durable write probe通过；
- pre/mid/post failure只清理本operation staging，不修改Backup、foreign/previous instance或公网基础设施；
- MI-1..MI-5 独立fixtures和完整isolated A→B rehearsal。

以上 runtime vectors 属于后续实现；本轮只增加小型文档 invariant tests，不创建 Restore runtime 测试框架。

## 22. Deferred implementation

Restore CLI/runtime、operation journal、compatibility engine、database staging、secret resolution、R2 restore、locator publication、existing-instance adoption、production rehearsal和contract tests全部 **DEFERRED**。

本 Contract Freeze 不授权production mutation、Release、部署、读取secret、清空target、开始Migration或实现自动Restore。成功条件是语义冻结并与另外五份Phase 2 Contract完成一致性评审。
