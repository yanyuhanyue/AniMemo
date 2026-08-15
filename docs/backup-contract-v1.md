# AniMemo Backup Contract v1

**Status:** FROZEN FOR v1.1

**Version:** v1

**Scope:** 冻结 AniMemo instance disaster recovery backup 的逻辑成员、Backup Format v1、创建一致性、完整性验证、secret 与媒体覆盖语义、保留边界及与 Release/Updater 的接口。

**Definitions:** Backup 是为同一 AniMemo instance 创建可验证的 disaster-recovery artifact；Backup Format v1 是独立于 AniMemo app SemVer 的 `schemaVersion = 1` artifact contract。

**Non-goals:** 本文不实现 Backup CLI/runtime、Restore、Migration、Export、Secret Envelope、Compatibility engine、Doctor、自动 retention、生产备份或任何公网基础设施操作。

**Dependencies:** 依赖 Phase 1 Deployment Boundary、Filesystem Layout、Installer、Public Origin / Listen Contract；与 Phase 2 Restore、Migration Bundle、Migration Secret Envelope、Compatibility Matrix 和 Doctor Contract 共同组成 durability interface。

**Security / Integrity implications:** 正式 Backup 含数据库、配置、credential ciphertext、local media 与 plugin state，属于最高机密性与完整性资产；checksum 只证明 bytes 一致性，不替代 authenticated secret protection 或 Release Authority。

**Compatibility:** 只使用 `COMPATIBLE`、`REQUIRES_UPGRADE`、`UNSUPPORTED`、`CORRUPT` 四种 compatibility classification。未知但结构和完整性均成立的 format 是 `UNSUPPORTED`；结构、checksum、authentication 或成员完整性失败是 `CORRUPT`。
**Change policy:** v1.1 内可以追加不改变既有语义的澄清；成员、排除项、identity、secret、R2 coverage、verification 或 compatibility 语义的变化必须提升 Backup Format/Contract 版本并经过 Restore、Compatibility Matrix、Release 与 Memory Integrity 联合评审。

## 1. Contract map

### Phase 1 dependencies

- [Deployment Boundary v1](deployment-boundary-v1.md)：AniMemo 只管理 AniMemo-owned roots、Compose project 与 lifecycle。
- [Filesystem Layout v1](filesystem-layout-v1.md)：定义 persistent、re-creatable、backup/migrate/restore 与 delete-safety 分类。
- [Installer Contract v1](installer-contract-v1.md)：定义 instance locator、roots 与 exact release installation identity。
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)：定义 application identity；Backup 只记录配置，不管理 DNS/TLS/proxy。

### Phase 2 contracts

- [Backup Contract v1](backup-contract-v1.md)：本文，定义正式 instance backup。
- [Restore Contract v1](restore-contract-v1.md)：验证、兼容性计划、恢复与发布门禁。
- [Migration Bundle v1](migration-bundle-v1.md)：移动 instance，不等同 Backup。
- [Migration Secret Envelope v1](migration-secret-envelope-v1.md)：Issue #87 的 secret transport 与 envelope 语义。
- [Compatibility Matrix v1](compatibility-matrix-v1.md)：跨 format、release、database/config 与 runtime 的四态判定。
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)：只读发现、验证与诊断接口。

[Release Contract v1](release-contract-v1.md) 与 [Update Agent v1](update-agent-v1.md) 继续拥有 Release Authority、CURRENT/PREVIOUS、migration safety 与 fail-closed 语义。

## 2. Semantic separation

以下术语不可互换：

- **Backup:** 同一 instance 的 disaster recovery artifact。
- **Restore:** 从 verified Backup 恢复可运行 instance。
- **Migration:** 把 instance identity、数据与必要配置移动到另一环境。
- **Export:** 用户拥有的 portable memory/data；可以有损或排除 instance/security state。

Staff JSON/CSV、Data Bundle 和其他 Export 即使历史名称包含 “backup” 或 “restore”，也不是本 Contract 的 Backup，不得作为 disaster recovery evidence。

## 3. Memory Integrity invariants

正式 Backup 必须为 Restore 提供以下不变量：

- **MI-1 — External metadata disappearance:** 外部 metadata 消失、变空或暂时不可访问时，不得删除、降格或覆盖用户记忆；Backup 必须保留当前 authoritative memory 与最后已知 metadata/source 语义。
- **MI-2 — Provider identity change:** provider ID、canonical URL、账号或 backend identity 变化时，不得静默 orphan 用户记忆；稳定 internal identity 与 source/provider binding 必须可恢复和审计。
- **MI-3 — Identity merge:** identity merge、deduplication 或 canonicalization 必须保留全部历史 reference、watch history、metadata provenance 与 user-owned relation，不能只保留当前显示 identity。
- **MI-4 — Unsupported memory preservation:** upgrade、import、Restore 或 Migration 遇到当前版本不支持的 memory/schema/plugin payload 时，不得静默丢弃、默认化或部分消费；必须保存原 bytes/state 并判定 `REQUIRES_UPGRADE` 或 `UNSUPPORTED`。
- **MI-5 — Destructive ambiguity:** ownership、identity、path、member、merge 或 recovery target 存在 destructive ambiguity 时必须 fail closed，或进入绑定 exact evidence 的显式 repair；不得猜测后删除、覆盖、合并或重建。

Backup 是完整 physical/logical state capture，不按当前 UI 可见集合过滤，不把 Staff export 的有限 dataset 当作 MI-1 证明。

未来 contract fixture 必须分别覆盖：external metadata 已消失但 memory 仍存在；provider identity 改变但 stable binding 不 orphan；identity merge 后旧 references/history 仍可达；future/unsupported memory payload 被原样保留并判为 `REQUIRES_UPGRADE`/`UNSUPPORTED`；ambiguous identity/path/member 使 Backup fail closed。单一 happy-path row-count 不能替代 MI-1..MI-5。

## 4. Backup Format v1 identity

Backup Format v1 与 AniMemo app version 独立。每个 backup set 必须拥有：

- globally unique `backupId`，使用规范 UUID；
- UTC `startedAt` 与 `completedAt`；
- stable directory/object prefix `backup-<UTC timestamp>-<backup UUID>`；
- `schemaVersion: 1` 与固定 `format: animemo-instance-backup`；
- source instance、release、deployment、database、configuration 与 plugin identity；
- 完整 inclusion/exclusion、coverage、checksum 与 secret protection metadata。

重复验证、复制或上传同一 backup 不得改变 `backupId`、`completedAt`、member bytes 或 checksum。内容变化必须生成新的 backup identity。

## 5. Logical member layout

正式 v1 logical layout：

```text
backup-<UTC timestamp>-<backup UUID>/
├── backup-manifest.json
├── checksums.sha256
├── database.sql.gz
├── filesystem/
│   ├── config/
│   ├── plugins/
│   ├── media/
│   └── private/
├── updater-state/
└── secrets/
    ├── secret-envelope.json
    └── secret-reference.json
```

`secret-envelope.json` 与 `secret-reference.json` 二选一；不能同时存在。没有 secret-bearing state 的 profile 仍必须在 Manifest 中显式声明 `secretMode: none`，不得通过成员缺失猜测。

`checksums.sha256`：

- 使用 UTF-8、LF、lowercase SHA-256 与 canonical POSIX relative path；
- 按 path byte order 稳定排序；
- 覆盖 `database.sql.gz`、所有 filesystem/updater payload 和实际存在的 secret member；
- 不递归包含自身；
- `backup-manifest.json` 记录 exact `checksums.sha256` bytes 的 SHA-256。

Manifest 和 checksum set 不允许 absolute path、`..`、重复 path、Unicode/大小写归一化冲突、symlink、junction、device、FIFO、socket 或 hard-linked sensitive member。

## 6. Source discovery and preconditions

Backup 必须从经过验证的 `instance.json` 和 deployment profile 发现 roots，不能从当前目录、面板路径、环境猜测或 symlink 推断。

v1.0 compatibility profile 可以读取其既有 app-root protected configuration，但必须把逻辑成员映射为本 Format 的 protected config/secret 成员；旧文件位置不能成为新 Backup 的 layout。

开始前必须：

1. 验证 locator、canonical roots、owner/mode、Compose project 与 running exact release identity 一致。
2. 获取 AniMemo-scoped durable operation lock。
3. 拒绝 active Installer、Update、Migration、Restore 或另一 Backup。
4. 检查目标容量、私有权限和 local/external destination 可写性。
5. 确认 database、configuration 与 enabled Plugin SDK identity 可读取。
6. 拒绝 foreign files、path escape、link/reparse point、unknown protected member 或不一致 deployment profile。

只读 preflight 失败不得创建正式 backup identity；已创建的 staging 必须保持不可被消费者发现。

## 7. Consistency and quiescence

Backup 必须捕获一个可解释的一致性点：

1. 阻止新的 application writes，并等待在途 API/plugin/integration/media mutation 到达已知边界。
2. 保持 PostgreSQL 可读，停止或隔离会改变 filesystem payload 的 AniMemo writers。
3. 执行 logical `pg_dump`。
4. 在应用 writes 仍被阻止时捕获 config、plugins、local media、private allowlist 与 selective updater state。
5. 记录 quiescence method、开始/结束时间与任何 external dependency coverage。
6. 完成 VERIFY 或失败后才解除 quiescence。

仅依赖 “文件复制很快”、live directory tar、Redis pause 或 filesystem mtime 不能证明数据库与 media/plugin graph 一致。无法建立 quiescence 时整个 Backup 失败。

## 8. PostgreSQL member

`database.sql.gz` 的唯一 v1 正式形式是：

```text
pg_dump logical plain SQL
→ gzip
→ checksum
→ versioned metadata
```

必须：

- 对目标 instance 的 authoritative PostgreSQL database 执行 `pg_dump --format=plain --no-owner --no-privileges`；
- 记录 PostgreSQL server major、pg_dump tool version、database/config contract 与 dump command profile；
- 捕获未压缩 bytes SHA-256/size 和压缩 bytes SHA-256/size；
- 要求 subprocess 成功、输出非空、gzip stream 完整；
- 使用私有 staging file、`0600`、fsync 与 failure-safe finalize。

`pg_dump` 非零退出、timeout、broken pipe、空输出、gzip/checksum 失败都使整个 Backup 失败。禁止 tar、rsync、snapshot 或复制 live `postgres/` 目录冒充正式 database backup。

## 9. Filesystem inclusion allowlist

| Source class | Included | Excluded / rule |
| --- | --- | --- |
| Protected config | Canonical non-secret config metadata；secret-bearing bytes 进入 secret envelope/reference | 未识别 config、临时文件、旧 atomic temp、world-readable secret 必须阻断 |
| Plugins | DB-referenced package CAS、user-uploaded package bytes 与明确登记的 plugin durable content | `runtime/` 由 DB + verified CAS 重建；`previews/`、`staging/`、`.locks/` 与临时解压不迁移 |
| Local media | `media/` 下所有 regular files，逐文件 path/size/SHA/mode；验证 DB-referenced MediaObject bytes | 不删除 unknown local file；未分类文件必须在 Manifest 中标为 preserved-unreferenced，不能静默遗漏 |
| Private | 仅由 Backup Contract registry 识别的 durable private member | plaintext `setup-code`、临时文件与未知 private member 不进入 Backup；未知项阻断 finalize |
| Updater locator | source locator identity/digest，供 Restore 规划 | source absolute roots 不能直接发布为 target locator |
| Release slots/history | 仅 validated Manifest/deployment/checksum identity 与必要 history | 不把 embedded metadata 当 Release Authority |
| Operation/plan/runtime state | durable journal、PENDING/recovery barrier、runtime contracts 与 enabled Plugin APIs | 不丢弃或改写状态；raw logs 不进入 Backup |
| Bootstrap state | 仅未完成 bootstrap 且 schema 明确要求的 durable identity；正常 initialized instance 不包含 | stale bootstrap input 不得导致 CURRENT 重导入 |

固定排除：

- `postgres/` physical data；
- `redis/`；
- `logs/`；
- `backups/` 及 nested backup；
- Updater/network cache；
- `update.lock` 与其他 locks；
- `/run/animemo-updater`、socket 与其他 runtime state；
- Host credential stores；
- `/opt/animemo` application/deployment binaries；
- Docker images、volumes snapshot、public proxy/DNS/TLS/firewall 配置。

## 10. Selective Updater state

Backup 只允许纳入：

- validated source locator evidence；
- CURRENT/PREVIOUS/release history 的完整、内部一致 state；
- durable operations、plans、runtime contracts 与 recovery barrier；
- 为判断 source exact release 和 pending transition 必需的 non-secret metadata。

必须排除 `gh/`、`.docker/`、download cache、temporary assets、locks、socket 与 generic Host credentials。

Backup 不得合成、修补或 “简化” CURRENT/PREVIOUS。slots/history 不一致、Manifest 未通过既有 schema 验证或 PENDING transition 无法解释时必须失败；不得为了产生 Backup 删除 journal。

## 11. Secret handling

Formal Backup 必须保留恢复 encrypted database credentials 所需的 protected configuration，包括 `CREDENTIAL_ENCRYPTION_KEY` 的恢复能力，但不得：

- 把 secret 明文写入 `backup-manifest.json`、`checksums.sha256`、普通 filesystem payload、日志或输出；
- 使用 `CREDENTIAL_ENCRYPTION_KEY` 加密一个同时包含该 key 自身的 envelope；
- 把 GitHub/GHCR Host credential 当作 instance secret；
- 因缺少 secret material 仍宣称 backup 为完整、可恢复。

允许两种模式：

- **Envelope:** secret-bearing config 使用 external backup passphrase、one-time key 或 KMS key 进行 authenticated encryption；key 不在 backup 中。
- **Reference:** Backup 保存 non-secret external secret reference、provider/version 与 verification metadata；Restore 必须能在目标环境解析同一 secret。

KDF、AEAD、reference 解析和 redaction 语义由 [Migration Secret Envelope v1](migration-secret-envelope-v1.md) 共同冻结。Reference 类型不受 target 支持或不存在安全 resolution path 时是 `UNSUPPORTED`；已支持 reference provider 只是暂时不可用时是 operational evaluation error、无 compatibility decision。两者都不是 artifact `CORRUPT`。

## 12. R2 and external media coverage

每个 R2/external backend 必须选择并记录：

- **captured:** Backup 包含所有由数据库稳定 MediaObject identity 拥有的 remote bytes 及 size/SHA；
- **reference-dependent:** Backup 只保存数据库 reference 与必要 secret/config，恢复依赖同一个 external bucket/object identity。

Reference-dependent backup 不覆盖 bucket/object loss，必须在输出和 Manifest 中明确。Restore 时 dependency 缺失不得删除 MediaObject rows、改写 stable references 或替换默认图片。

Reference-dependent metadata 必须绑定 normalized physical identity：backend type、endpoint/account identity、bucket 与 exact object keys；credential 是否相同不能用来判定是否为同一 external dependency。复用同一 verified dependency 时不复制 remote bytes。

Backup 只以数据库拥有的 exact backend/object key 为读取范围；不得以 bucket listing 推导删除权限。Unknown R2 orphan 永远不得自动删除，也不能因为未被捕获而改变其存在状态。

## 13. Manifest identity and metadata

`backup-manifest.json` 至少包含：

- format/schema、backup UUID、started/completed UTC；
- canonical artifact binding record 与 `artifactBindingDigest`；该 record 排除 Envelope bytes/checksum、final Manifest checksum与finalize timestamps，避免AAD循环依赖；
- source stable instance identity、deployment profile 与 source locator digest；
- exact release version/channel/commit、Manifest/deployment contract identity 与 API/Web OCI digests；
- database/config contract、enabled Plugin SDK APIs、plugin package identity inventory；
- PostgreSQL server/tool/dump profile；
- logical member allowlist、exclusions、counts、sizes 与 checksum-set digest；
- quiescence method 与 consistency boundary；
- local media、R2 captured/reference-dependent coverage；
- secret mode、envelope/reference schema 与 non-secret KDF/AEAD/provider metadata；
- Backup producer version、supported OS/architecture facts；
- verification status与最后验证时间；
- known external dependencies 和 Restore prerequisites。

Release metadata 只是 source evidence。Restore 必须回到正式 GitHub Release + GHCR exact digest authority 重新验证，不能信任 Backup 自报的 image、Manifest 或 tag。

## 14. Lifecycle: STAGING → VERIFY → FINALIZE

### STAGING

- 在 destination 内创建 private、unique、不可发现 staging prefix。
- Local backup root 与所有 staging/final directories 必须为 `0700`，普通成员默认 `0600`；不得 group/world-readable。记录的 source mode 只用于 Restore validation，不放宽 backup storage mode。
- 使用 exclusive create；拒绝覆盖同名或 unknown object。
- 每个 member 写完后 fsync/上传并重新读取必要 metadata。
- non-secret payload cross-check完成后，先生成canonical artifact binding record/digest，再据此创建唯一Secret Envelope；随后checksum set覆盖完整Envelope file。
- 任何失败只留下明确 incomplete staging，不产生有效 backup。

### VERIFY

- 验证 strict schema、member allowlist、checksum、gzip、tree/path、owner/mode 与 secret authentication。
- 对 source identity、database/config/plugin identity 和 coverage 做内部一致性检查。
- 对 external destination 完成 read-after-write verification。

### FINALIZE

- 生成稳定 `checksums.sha256` 和 final Manifest。
- local filesystem 在同一受保护父目录 atomic rename staging root；不支持 atomic rename 的 object storage 最后发布 immutable `backup-manifest.json` 作为 commit marker。
- fsync final parent 或验证 external final object。
- finalized backup immutable；不能原地补 member 或改 Manifest。

## 15. Verification levels

- **Structural verification:** 验证 schema、paths、member set、checksums、gzip、secret authentication、metadata 与 coverage 内部一致性。它不证明应用真的能启动。
- **Restore rehearsal:** 把该 exact backup 恢复到 isolated fresh target，执行 compatibility plan、database import、filesystem restore、security rotation 与 MI-1..MI-5 validation。

二者必须分别记录。`STRUCTURALLY_VERIFIED` 不得宣传为 restore-rehearsed；rehearsal 结果必须绑定 exact `backupId` 和 checksum-set digest。

## 16. Retention and destination semantics

- `/data/animemo/backups` 可以存放 local backup，但永不递归包含自身。
- External destination 必须提供 private access、immutable object identity、完整上传验证与明确 lifecycle ownership。
- Backup 在传输中必须使用认证加密通道；destination 必须提供与数据敏感度相称的 at-rest confidentiality，不能公开读取数据库、媒体或 metadata。
- Local/remote copy 保持同一 `backupId` 与 checksum；复制不得创建不同内容的同 ID backup。
- Retention 默认由管理员显式决定；AniMemo 可列出 inventory、age、size、verification 与 coverage。
- 不得自动删除唯一 recovery point、unknown/unverified backup、正在复制/验证的 backup，或仍被 operation/rehearsal 引用的 backup。
- 删除必须基于 exact backup ID、重新验证 ownership、保留至少一个符合 policy 的 recovery point，并生成 audit evidence。

## 17. Failure and output contract

Backup 任一步失败：

- 输出 stable error class 与 redacted detail；
- 不解除源数据保护前先终止 writer/copy process；
- 不删除 source data、existing backup 或 unknown destination object；
- 不把 partial staging 标记为 valid；
- 不打印 secret、env value、token、credential、setup code、Authorization header 或带 credential URL。

失败后的 staging 只能由 future scoped cleanup 根据 exact operation/backup ID 处理；generic age timeout 不提供删除授权。

## 18. Existing noncanonical tools

当前 Updater 的 pre-migration `pg_dump` 是 **Update Safety Backup**，只服务 Update Agent migration gate。它的 freshness/checksum/gzip 验证继续保持，但不能被称为 Backup Format v1 instance backup。

`scripts/dr_backup.py` 与 isolated A→B DR rehearsal 是 noncanonical test/evidence tool。它证明 logical dump、local filesystem、fresh-target restore、authentication rotation 与部分 Memory Integrity，但其 whole-private/whole-updater copy、config exclusion、permission 与 metadata schema 不是本 Format。不得直接把该 helper 暴露为 v1.1 Backup runtime。

## 19. CURRENT → TARGET gap

| Area | CURRENT | TARGET | Classification |
| --- | --- | --- | --- |
| Semantic separation | Phase 1 已分离 Backup/Restore/Migration/Export | 本文沿用且要求产品文案消歧 | ALREADY SATISFIED |
| PostgreSQL | Updater 已有 atomic logical dump safety point | Backup Format v1 正式 database member | ALREADY SATISFIED |
| Redis/logs/cache | 当前 rehearsal 排除 | 固定排除并记录重建语义 | ALREADY SATISFIED |
| Canonical format | 只有旧 DR manifest | `backup-manifest.json` + checksum + strict layout | DOCUMENTATION GAP |
| Protected config/secret | 当前 helper 排除，v1.0 config 仍在 legacy location | profile-aware protected capture + envelope/reference | IMPLEMENTATION DEFERRED |
| Plugin/private/updater state | 当前 helper 整树复制 | 逐项 allowlist 与 selective state | IMPLEMENTATION DEFERRED |
| R2 | 当前 rehearsal 未覆盖 | captured/reference-dependent | IMPLEMENTATION DEFERRED |
| Export naming | Staff/Data Bundle 历史使用 backup/restore 字样 | canonical docs 明确为 Export/Import；未来产品文案继续收敛 | DOCUMENTATION GAP |
| Compatibility metadata | Release 只定义 live app switch compatibility | Artifact 自带独立版本，#90 跨 format 裁决；不修改 Release Manifest | DOCUMENTATION GAP |
| Retention | 只有 freshness，无 conservative inventory policy | exact-ID、保留 recovery point、无 silent delete | DOCUMENTATION GAP |
| Full runtime | 无正式 Backup CLI/runtime | future implementation conforming to this Contract | IMPLEMENTATION DEFERRED |

旧 helper 是 noncanonical evidence；其差异不构成 Phase 1 canonical Contract 冲突，也不授权在本阶段修改 runtime。

## 20. Future contract tests

未来实现至少必须增加：

- strict Manifest/checksum golden vectors，以及 unknown schema、claimed-v1 extra field/member、path traversal、duplicate path、symlink/junction/hard-link corruption vectors；
- `pg_dump` non-zero、timeout、empty output、disk-full、gzip/checksum failure 均不产生 finalized backup；
- quiescence 期间 concurrent database/media/plugin write 被阻止或使 Backup 失败；
- protected config 与 secret envelope/reference 必需，secret 不进入 public metadata/output，wrong key/authentication failure 判为 `CORRUPT`；
- Redis/log/cache/lock/socket/app binary/nested backup排除，private与Updater只含allowlist；
- local media graph/bytes/checksum、R2 captured/reference-dependent 与 unknown orphan preservation；
- STAGING crash、partial external upload与 finalize retry不产生同ID不同内容；
- retention不删除唯一、unknown、unverified或in-use recovery point；
- MI-1..MI-5 独立 fixtures，以及 exact backup ID 的 isolated restore rehearsal。

以上 runtime vectors 属于后续实现；本轮只增加小型文档 invariant tests，不创建 Backup runtime 测试框架。

## 21. Deferred implementation

Backup CLI/runtime、secret encryption、R2 capture、external upload、inventory/retention、scheduled execution、production rehearsal 与 contract tests全部 **DEFERRED**。本 Contract Freeze 不授权创建 backup、读取生产数据、发布 Release 或开始 Restore/Migration implementation。
