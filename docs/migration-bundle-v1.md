# AniMemo Migration Bundle v1

Status: FROZEN FOR v1.1

Version: v1

Scope: 冻结 AniMemo instance 在两个运行环境之间移动时的 bundle identity、成员、完整性、source consistency、target activation、配置与媒体策略。Migration 保持同一个 instance 的连续性，不创建第二个同时活动的 clone。

Definitions: Migration 是把一个 AniMemo instance 移动到另一运行环境；Migration Bundle 是承载该移动所需、可验证的 instance artifact；source 是生成 bundle 的 instance；target 是接收并验证 bundle 的环境；activation handoff 是 source 停止拥有活动身份、target 开始拥有活动身份的显式边界。

Non-goals: 本 Contract 不实现 migration runtime、Backup、Restore、Portable Export、Secret Envelope crypto、R2 byte transfer、DNS/TLS/proxy 自动化、完整 Installer 或 repair 工具；不发布 Release、不连接或修改生产。

Dependencies: 本 Contract 依赖 Filesystem Layout v1、Backup Contract v1、Migration Secret Envelope v1 与 Compatibility Matrix v1。数据库 dump 可以复用 Backup primitive，但 Backup 不因此等于 Migration，Migration 也不能冒充 Restore。

Security / Integrity implications: Bundle 可包含完整用户记忆、账号、插件、媒体、配置与 encrypted credential state，必须按最高机密性和完整性处理。secret 只允许进入 authenticated encrypted envelope；manifest、checksums、日志、报告和 locator 均不得包含 secret value。

Compatibility: Bundle format、Backup format、Restore format、Release Manifest、database/configuration contract 与 Plugin SDK API 各自独立 version。兼容结果只允许 `COMPATIBLE`、`REQUIRES_UPGRADE`、`UNSUPPORTED`、`CORRUPT`；不得使用 `UNKNOWN`、`probably works` 或静默降级。

Change policy: `format`、`formatVersion`、member allowlist、instance continuity、compatibility outcome、secret boundary、same-R2 判定、activation ownership 或 Memory Integrity 语义的改变均为 Contract 变更，必须记录兼容方案并评审；不得通过实现细节静默改变。

## Canonical contract set

Phase 1：

- [Deployment Boundary v1](deployment-boundary-v1.md)
- [Filesystem Layout v1](filesystem-layout-v1.md)
- [Installer Contract v1](installer-contract-v1.md)
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)

Phase 2：

- [Backup Contract v1](backup-contract-v1.md)
- [Restore Contract v1](restore-contract-v1.md)
- [Migration Secret Envelope v1](migration-secret-envelope-v1.md)
- [Compatibility Matrix v1](compatibility-matrix-v1.md)
- [Migration Bundle v1](migration-bundle-v1.md)
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)

## 1. Semantic boundary

| Operation | Meaning | Migration Bundle relation |
|---|---|---|
| Backup | 同一 instance 的 disaster-recovery copy | Migration 可复用其 verified logical database primitive，但目的、manifest 和 activation 语义不同 |
| Restore | 从 Backup 重建 instance | 不承担 source→target ownership handoff |
| Migration | 移动 instance 并保持 identity、memory 与 references 连续 | 本 Contract 的唯一用途 |
| Export | 用户拥有的 portable data copy | 不承诺重建完整 instance，不是 Migration Bundle |

现有 Data Bundle、CSV、Staff export 或 isolated DR helper 都不能仅因能够复制部分数据就被称为 Migration Bundle。

## 2. Format identity

Canonical identity：

```text
format: animemo-migration-bundle
formatVersion: 1
```

每个 finalized bundle 必须有唯一 `bundleId`，使用不可预测 UUID。每个 source instance 必须有稳定、non-secret 的 `instanceId`：

- fresh install 生成新的 `instanceId`；
- migration 保留原 `instanceId`；
- reconfigure Public Origin、listen 或 host path 不生成新 identity；
- 两个同时活动且使用相同 `instanceId` 的实例是 split-brain，不是成功 migration；
- 将 bundle 导入测试环境时必须保持 inactive，或使用未来明确设计的 clone/fork contract；v1 Migration 不授权 clone。

`bundleId` 标识一次 artifact，`instanceId` 标识长期 instance；两者不得互换。

## 3. Canonical members

Finalized bundle 是 member allowlist，不得携带未知顶层内容：

| MEMBER | REQUIRED | SEMANTICS |
|---|---:|---|
| `manifest.json` | YES | canonical migration manifest；non-secret |
| `checksums.sha256` | YES | 绑定每个允许 member 的 canonical relative path、size 与 SHA-256；不使用绝对路径 |
| `database.sql.gz` | YES | PostgreSQL logical dump；不是 live PGDATA archive |
| `database.metadata.json` | YES | dump 时间、compressed/uncompressed size 与 checksum、database/configuration compatibility metadata |
| `plugins/manifest.json` | YES | plugin project/version/deployment、enabled SDK APIs 与所需 CAS digest inventory；不替代数据库 |
| `plugins/cas/` | CONDITIONAL | 所有不可从 source-of-truth 安全重建的 `.ajplugin` CAS bytes |
| `media/manifest.json` | YES | MediaObject/backend inventory 与每个 backend 的 `SAME_R2`、`LOCAL_INCLUDED` 或 `TRANSFER_REQUIRED` strategy |
| `media/local/` | CONDITIONAL | Local backend 的 authoritative bytes |
| `config/non-secret.json` | YES | non-secret managed config、Public Origin/listen policy 与 config contract identity |
| `secrets/secret-envelope.json` | YES when secrets exist | 唯一 Migration Secret Envelope；包含 authenticated outer header/ciphertext，manifest 只记录 version/suite、artifact binding 与完整 Envelope file checksum |
| `private/manifest.json` | YES | allowlisted private-state inventory；禁止递归复制未知 private files |
| `updater/state.json` | YES | selective logical Updater state；不是 `/var/lib/animemo-updater` 的 raw copy |

未知 member、重复 path、绝对 path、`..`、symlink、junction、hard link、device、FIFO、socket、稀疏逃逸或超出实现公布限制的 file count/size/compression ratio 必须拒绝。Critical extension 或未知 incompatible major version 的结果是 `UNSUPPORTED`；member/checksum/shape/integrity 失败的结果是 `CORRUPT`。

## 4. Manifest minimum schema

`manifest.json` 至少必须绑定：

- `format`、`formatVersion`、`bundleId`、`createdAt`；
- stable `instanceId`；
- source `v1.1-standard` deployment profile；
- source canonical app/data roots，且只作为 source metadata，不强迫 target 复用；
- source locator schema、managed config location 与 filesystem contract version；
- source database/configuration contract、Plugin SDK APIs 和 migration bundle compatibility metadata；
- exact release identity：version、channel、release commit、provenance identity、Manifest/deployment identity、API/Web repository@digest；
- source Public Origin 与 listen；
- 每项 environment-dependent configuration 的 disposition：`PRESERVE`、`RECONFIGURE` 或 `TARGET-LOCAL`；
- plugin CAS inventory；
- media backend inventory、physical identity 与 transfer strategy；
- canonical artifact binding record 与无循环依赖的 `artifactBindingDigest`；该 record 排除 Envelope bytes/checksum、final Manifest checksum 与 finalize timestamp；
- Secret Envelope version/suite/binding metadata，不含 secret；
- selective updater-state generation/identity；
- canonical members 与 checksums identity；
- source quiescence and consistency evidence；
- required target compatibility outcome。

Manifest 不得包含 password、token、credential、setup code、encryption key、passphrase、Authorization header、credential-bearing URL 或 decrypted provider configuration。

## 5. Database primitive

Canonical database member：

```text
pg_dump logical dump
+ gzip
+ checksum
+ metadata
```

禁止 tar、rsync、snapshot 或复制 live `/data/animemo/postgres` 作为正式 Migration database member。创建方必须验证 dump command 成功、gzip 可完整解压、compressed checksum 和 uncompressed checksum/size 一致；target 必须在任何 database import 前重新验证。

Migration 可以复用 Backup Contract 的 logical dump primitive，但 Migration manifest 还必须绑定 instance identity、filesystem members、release identity、secret envelope、source quiescence 与 activation ownership。复用 primitive 不允许把 Backup artifact 无验证地改名成 Migration Bundle。

## 6. Source consistency and quiescence

Bundle 必须代表一个一致 snapshot。创建前必须：

1. 验证 locator、managed config、Compose mounts、systemd allowlist、Updater CURRENT 与 running release identity 一致。
2. 验证不存在 active update、database migration、plugin publish/rollback、local media write、pending MediaWriteReservation 或其他会改变 bundle members 的 operation。
3. 进入显式 AniMemo-scoped quiescence；不得停止共享 PostgreSQL/Redis、Docker daemon 或其他 Compose project。
4. 记录 source consistency boundary；从该边界开始禁止新的用户、plugin、media 与 config mutation。
5. 在同一 boundary 内生成 database dump、CAS、local media、config、private、secret envelope 与 updater logical state。
6. 对 DB references 与 filesystem members 做 cross-check，再 finalize。

source 从 quiescence 后恢复写入会使 bundle 失去 activation 资格；必须重新创建 bundle。实现不得以“差异可能很小”为由继续 target activation。

## 7. Staging, verification and finalize

固定状态机：

```text
Preflight
→ Quiesce Source
→ Create Private Staging
→ Capture Non-secret Members
→ Cross-check References
→ Build Canonical Artifact Binding Record And Digest
→ Create The Single Secret Envelope
→ Write Checksums
→ Write Final Canonical Manifest
→ Verify Entire Staging
→ Atomic Finalize
→ Keep Source Quiesced For Handoff
```

staging 必须是本次 operation 唯一拥有、非 link、`0700` 的目录；member 默认 `0600`。`checksums.sha256` 覆盖全部 payload 与实际存在的 Secret Envelope，但不递归包含自身；final Manifest 记录 exact checksum-set bytes 的 digest、binding record 与 `artifactBindingDigest`。Manifest 与 checksums 必须 canonical serialization，写入后 fsync file 和 parent，再用 atomic rename 发布。Final output 已存在、非空、foreign-owned 或与 source/member overlap 时必须 fail closed。

任何失败只能清理本次 operation 创建且由唯一 staging identity 证明 ownership 的路径。不得删除 source data、已有 bundle、backup、local media、plugin CAS、remote object 或 unknown file。Finalized bundle 必须在 transport 后再次完整验证。

## 8. Plugin lifecycle

Plugin continuity 同时依赖 PostgreSQL 与 filesystem：

- PluginProject、PluginVersion、PluginDeployment、UserPluginInstallation、PluginData 与 plugin config 由 database dump 保存；
- `plugins/packages/sha256` 中数据库引用的 CAS package bytes 必须按 digest 收集和验证；
- user-uploaded package 不得假定可重新下载；
- `plugins/runtime` 是由 verified CAS、Manifest snapshot 与 deployment rows 重建的 material，不是 authoritative migration member；
- `plugins/previews`、`plugins/staging`、`plugins/.locks` 是 recreatable/ephemeral，不迁移；
- target 必须验证 current/previous package、Manifest snapshot、CAS digest、enabled SDK APIs 和 target release compatibility；
- retention 到期或 disabled 状态不得成为迁移时删除 PluginData 或 CAS 的理由；
- 缺失 package、未知 Plugin SDK API、不可解释的 database/filesystem mismatch 必须 `UNSUPPORTED` 或 `CORRUPT`，不得静默停用后继续。

## 9. Media and R2

Database 中的 stable MediaObject reference、backend identity、object key、size/hash 与所有 user memory references 必须保留。

### 9.1 Verified same-R2

只有 source/target 经过规范化的 physical identity 完全一致时，才能判为 `SAME_R2`。Identity 至少包含 backend type、normalized endpoint/account identity 与 bucket；credential 是否相同不是 physical identity。

`SAME_R2`：

- 不复制 poster/media bytes；
- 保存 MediaObject rows、stable references、backend configuration 与 Secret Envelope 中的必要 credential；
- target validation 必须证明每个 managed reference 仍指向同一 physical backend；
- 不枚举或删除 unknown remote object。

### 9.2 Different or indeterminate R2

不同 R2 必须使用未来显式、verified remote byte transfer。该 runtime 未实现时结果为 `UNSUPPORTED`；不得假装同 bucket、只复制数据库后激活。

无法证明 physical identity、credential unavailable、object/reference mismatch 或 transfer incomplete 时必须 fail closed。Unknown R2 orphan 永远不得自动删除，数据库中暂时缺少 reference 也不是删除 authority。

### 9.3 Local media

Local backend 的 authoritative bytes 必须进入 `media/local/`，并与 MediaObject inventory、relative object key、size/hash cross-check。Target path 由 target data root 重新解析；source absolute path 不得直接写入 target。

## 10. Protected configuration and secret envelope

non-secret config metadata 与 secret material 必须分离：

- `config/non-secret.json` 可包含 schema、feature flags、Public Origin、listen、trusted non-secret identity 与 target policy；
- database dump 可以包含 encrypted credential ciphertext；
- 解密这些 ciphertext 所需的 credential encryption key、host/application secrets 与 allowlisted lifecycle-bound private secret state 只能进入同一个 Secret Envelope；
- 不能使用一个 key 加密同时包含该 key 自身的 envelope；
- Migration 使用 external passphrase 或 one-time key、modern KDF 与 authenticated encryption；
- wrong key/authentication failure 必须发生在 target mutation 前，并使用稳定、redacted error；
- temporary plaintext、command arguments、process environment、logs 与 reports 的规则由 Migration Secret Envelope v1 冻结。

Unknown private file 不得 raw-copy。Initialized instance 不迁移已消费或 stale setup code；未初始化 instance 的 setup lifecycle 必须由明确 allowlist 与 Secret Envelope 保持，无法证明状态时为 `UNSUPPORTED`。

## 11. Public Origin / Listen matrix

Public Origin 与 listen 始终是两个独立字段：

| MODE | PUBLIC ORIGIN | LISTEN | PUBLIC EDGE | ACTIVATION RULE |
|---|---|---|---|---|
| `PRESERVE` | 保留 source canonical value | 保留 source endpoint；冲突则失败 | 管理员自行保持/切换 DNS、TLS、proxy | 两项均验证且 target local health PASS 后才能 handoff |
| `RECONFIGURE` | 仅使用管理员显式 target value；未提供则 preserve | 可独立使用管理员显式 target value；未提供则 preserve | AniMemo 不修改 DNS/TLS/proxy/provider callback | Validate→Atomic Config Update→AniMemo-scoped Reload→Local Health→Commit；失败回滚 |
| `TARGET-LOCAL` | 保留 source application identity | 使用显式 target loopback endpoint，默认仍为 loopback | 不进行 public activation | 用于 target 本地验证；后续 public handoff 必须另行显式批准 |

不得从 target IP、listen、Host header、DNS 或 proxy 猜测 Public Origin。`TARGET-LOCAL` 不把 Public Origin 改为 localhost，也不默认开放 `0.0.0.0`。RECONFIGURE 成功后必须提示管理员自行核对 DNS、TLS、reverse proxy 与 external provider callbacks。

## 12. Selective Updater state

禁止 raw-copy `/var/lib/animemo-updater`。`updater/state.json` 只允许逻辑表达：

- source CURRENT exact verified Manifest/identity；
- source runtime database/configuration contracts 与 enabled Plugin SDK APIs；
- PREVIOUS/history 的 exact identity 与必要 audit metadata；
- 已完成 operation 的必要 durable evidence；
- state schema/generation。

必须排除 cache、download artifacts、plans、locks、socket、temporary files、GitHub/GHCR credentials 和其他 host credential stores。存在 PENDING、manual recovery required 或无法解释的 operation state 时，source 不得生成 activatable bundle。

Target 不得通过复制文件伪造 Updater CURRENT/PREVIOUS。顺序必须是：重新验证正式 Release Authority、实际部署并验证 exact current release、建立 target runtime contracts，然后通过 Updater 已批准的 bootstrap/reconciliation interface 建立 CURRENT。PREVIOUS 只有在 target 实际取得并验证对应 release、且 rollback compatibility 成立时才可建立；否则保留为历史 evidence，不发布为可回退 slot。

## 13. Release identity is evidence, not authority

正式 Release Authority 继续只有：

```text
GitHub Release in yanyuhanyue/AniMemo
+ GHCR exact API/Web repository@sha256:digest
```

Bundle 内 Manifest、checksums、OCI digest 或 release metadata 只说明 source 声称运行什么；target 必须重新从正式 Authority 取得 exact Release metadata、验证 tag、Manifest、deployment contract、checksums、provenance、attestations 与实际 OCI digests。

Bundle 不携带 `/opt/animemo` binaries，不使用 `latest`，不允许版本替换。Exact source release 不可取得时，根据 Compatibility Matrix 返回 `REQUIRES_UPGRADE` 或 `UNSUPPORTED`，不得把 bundle 变成第二 Release Source。若未来 standalone migration program 不能由现有 exact Release identity 绑定，必须进行 Release Contract review。

## 14. Canonical source to canonical target

Migration Bundle v1 只接受由有效 `instance.json` 证明的
`v1.1-standard` source，以及 Filesystem Layout v1 的 exact canonical roots。
Source locator、Compose、Updater CURRENT、data root 与 running exact release
identity 必须一致；缺失 locator、unknown profile 或 noncanonical root 为
`UNSUPPORTED` 或 target/source ambiguity failure，不得扫描路径或从 env 猜测。

Target 同样只使用 canonical roots，并从 Release Authority 重建 application
material、生成 target-specific locator/allowlist。不得复制 source app tree、
盲拷 absolute path、使用 symlink 伪装 root，或提供 custom/legacy fallback。

pre-v1.1 filesystem/config reader、panel source profile、old bundle reader 与
历史 data-path migration 均不属于 v1 Runtime。Unknown existing data 保持不动
并 fail closed；clean break 不授权删除或覆盖 user-owned bytes。

## 15. Target validation and activation ownership

Target 导入顺序：

```text
Verify Bundle Without Mutation
→ Resolve Compatibility
→ Verify Secret Envelope
→ Prepare Empty/Owned Target Roots
→ Reacquire Exact Release From Authority
→ Restore Database/CAS/Local Media/Config Into Staging
→ Validate References And Contracts
→ Build Runtime Material
→ Start Target In Local/Inactivated Mode
→ Local Health And Release Identity Verification
→ Atomically Build Locator And systemd Allowlist
→ Explicit Source→Target Activation Handoff
```

Locator 不能从 source 原样复制。它必须在 target paths、config、release identity、Compose、systemd allowlist、Updater state 与 local health 均验证后，按 Installer/Filesystem Contract 原子生成或重建。任何 mismatch 均 fail closed。

Bundle creation 不自动关闭 source ownership。Handoff 必须显式确认：

- source 自 consistency boundary 后保持 quiesced；
- target 尚未公开接受写入；
- target validation 对 exact bundleId/instanceId PASS；
- 管理员授权 target 成为唯一 active instance；
- target activation 失败时 source rollback ownership 清晰，不能双写；
- target 激活后 source 只能作为 sealed rollback evidence，未经显式 reverse handoff 不得恢复写入。

Migration 不修改 DNS、TLS、reverse proxy 或 firewall。Public edge 切换由管理员负责，且不得被误报为 AniMemo local activation 成功的一部分。

## 16. Compatibility outcomes

| OUTCOME | MEANING | REQUIRED BEHAVIOR |
|---|---|---|
| `COMPATIBLE` | Bundle、source contracts、target runtime 与所有 critical extensions 可直接安全解释 | 可以进入 target staging；仍需全部 integrity/health gates |
| `REQUIRES_UPGRADE` | 有明确、machine-readable、受支持的 upgrade path | mutation 前展示 exact path；必须显式批准，不得静默升级 |
| `UNSUPPORTED` | unknown incompatible major、critical extension、different-R2 transfer 未实现、Plugin SDK/secret/config/runtime 不受支持 | 零 target data mutation；保留 bundle，报告稳定原因 |
| `CORRUPT` | checksum、shape、reference、archive、database dump、secret envelope authentication 或 immutable identity integrity 失败 | 零 target activation；不得尝试跳过损坏 member |

Format version 与其他 compatibility dimensions 必须分别报告。一个 dimension `COMPATIBLE` 不能覆盖另一个 dimension 的 `UNSUPPORTED/CORRUPT`。

## 17. Memory Integrity invariants

- **MI-1 External metadata loss never deletes memory.** Provider metadata、remote media 或网络不可用只能报告 degraded/unavailable；不得删除 Journal、Watch History、review、tag、local memory 或 stable reference。
- **MI-2 Provider identity change never silently orphans memory.** Provider/backend identity 改变必须显式映射；无法映射时 `UNSUPPORTED`，不能清除旧 identity 后继续。
- **MI-3 Identity merge retains historical references.** 任何显式 identity merge 必须保留历史 reference/provenance 和冲突记录；Migration v1 不执行自动 merge。
- **MI-4 Unsupported memory is never silently discarded.** 不支持的 Core/plugin/provider/media/config extension 必须阻断或走明确 upgrade path；不能丢字段、停用后删除或只迁移“已知部分”。
- **MI-5 Destructive ambiguity fails closed or requires explicit repair.** Duplicate ownership、missing CAS/media、unknown private file、orphan ambiguity、split-brain 或 locator mismatch 都不得自动删除/覆盖；repair 必须是未来独立、显式操作。

## 18. Failure and cleanup

- Source capture 失败：source 保持原 ownership；如果已 quiesce，可安全恢复到 capture 前状态，但不得伪报 finalized bundle。
- Transport/verification 失败：不触碰 target persistent state。
- Target staging 失败：只删除本次 target staging；不删除 source、bundle 或 existing target data。
- Database import 后失败：target 保持 inactive，进入 explicit recovery；不得自动 reverse migration 或覆盖 source。
- Locator/Updater/systemd mismatch：target 不激活，Updater fail closed。
- Handoff 后 health 失败：按已记录 ownership rollback；不能让 source 和 target 同时 writable。

所有错误输出只能包含 stable code、member/path label、checksum status、compatibility outcome 和 non-secret remediation。不得输出 secret、decrypted config、credential fragment 或 raw exception。

## 19. Current → Target gaps

| AREA | CURRENT | TARGET | CLASSIFICATION |
|---|---|---|---|
| Phase 1 roots/profile guard | canonical roots、lifecycle 与 locator interface 已重新冻结为 clean break | 直接复用 `v1.1-standard` only | ALREADY SATISFIED |
| Logical database primitive | 已有 `pg_dump` gzip、checksum、metadata 与验证 | 作为 Migration member primitive 复用 | ALREADY SATISFIED |
| DR helper | 复制 plugins/media/private/整个 updater state，排除 protected config，但已明确为noncanonical | 采用 strict member allowlist、Secret Envelope 与 selective state | DOCUMENTATION GAP |
| Bundle schema/atomic finalize | 无 canonical Migration format、bundleId、activation handoff | 本 Contract 冻结语义；runtime later | DOCUMENTATION GAP |
| Runtime | 无 locator/config reader、quiescence、bundle creator/importer | 后续实现 | IMPLEMENTATION DEFERRED |
| Plugin lifecycle | DB/CAS/runtime 已存在，但当前 DR 整树复制 | CAS authoritative、runtime rebuild、ephemeral excluded | DOCUMENTATION GAP |
| Media | stable MediaObject/R2 identity 与 no-orphan-delete 已存在 | same-R2/different-R2 matrix 与 local bytes | ALREADY SATISFIED |
| Secret transfer | encrypted DB fields存在，Migration envelope 未实现 | #87 external secret + AEAD | IMPLEMENTATION DEFERRED |
| Release binding | Updater 有 exact Manifest/OCI verification | bundle evidence必须在 target 重验 | ALREADY SATISFIED |
| Standalone tooling bytes | 当前 Release assets 不包含独立 migration tool | 若新增 asset/program，先扩展 exact byte binding | RELEASE CONTRACT REVIEW NEEDED |

## 20. Acceptance and STOP

Migration Bundle v1 只有在以下全部成立时才可宣称符合：格式独立且 versioned、instanceId 连续、source snapshot 一致、database/plugin/media/config/secret/updater members 完整、same-R2 被证明、different-R2 未实现时 fail closed、Release Authority 不变、canonical-only 且无 compatibility reader、target locator 最后生成、source/target 不双写、MI-1..MI-5 全部满足。

发现任何 unknown critical extension、integrity failure、missing authoritative memory、split-brain、locator mismatch、unverified Release、secret 明文、unknown orphan deletion proposal 或需要重跑历史数据迁移时，必须 STOP。#89 Backup Contract 与 #87 Migration Secret Envelope 未满足时，不得实现或宣称 Migration runtime 完成。
