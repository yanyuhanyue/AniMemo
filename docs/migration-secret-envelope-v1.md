Status: FROZEN FOR v1.1

Version: v1

# AniMemo Migration Secret Envelope v1

Scope: 冻结 AniMemo Backup/Restore/Migration 中 secret-bearing instance configuration 的 envelope identity、外层/内层语义、external secret 来源、KDF/AEAD 安全属性、bundle binding、secret disposition、生命周期、错误、临时文件和日志边界。

Definitions: Migration Secret Envelope v1 的独立 identity 是 animemo.migration-secret-envelope/v1。Envelope 是 non-secret authenticated header 与 authenticated ciphertext 的组合；payload 是 ciphertext 解密后、versioned 且严格 allowlisted 的 secret object。External migration secret 是 envelope 之外提供的 migration passphrase 或 independent one-time key。

Non-goals: 本文不实现 crypto runtime，不选择具体 KDF/AEAD primitive 或参数值，不创建 migration bundle，不读取/轮换生产 secret，不实现 KMS/reference provider，不重新加密数据库 CredentialCipher ciphertext，也不修改 Release Manifest v1。

Dependencies: 依赖 Phase 1 Filesystem Layout、Installer 和 Deployment Boundary 的 protected-config/secret boundary；与 Phase 2 Backup、Restore、Migration Bundle、Compatibility Matrix 和 Doctor Basic Contract 共同工作。

Security / Integrity implications: Envelope 保护 migration secret 在 artifact storage/transport 中的 confidentiality、integrity 和 bundle binding。它不保护已被攻陷的 source/target host、弱 passphrase、恶意终端、内存抓取或 external key channel，也不把 checksum 变成 authentication。实现不得声明可靠 secure erase。

Compatibility: 保持现有 CredentialCipher v1 ciphertext 和 CREDENTIAL_ENCRYPTION_KEY 的含义不变。v1 migration 默认原样保留数据库 ciphertext并迁移所需 key；Envelope version/suite 与 CredentialCipher version、Backup Format、Migration Bundle、AniMemo release SemVer 相互独立。

Change policy: Envelope identity、outer/inner required fields、secret dispositions、authentication result、AAD binding、input channel 或 plaintext lifecycle 的破坏性改变必须提升 Envelope 版本。具体 suite selection 是 Phase 3 implementation decision；producer 在首次输出 v1 前必须 pin 一个 reviewed suite及参数 policy并由 contract tests 固定，不能在同一 v1 producer中按环境静默切换。

## 1. Contract map

### Phase 1

- [Deployment Boundary v1](deployment-boundary-v1.md)
- [Filesystem Layout v1](filesystem-layout-v1.md)
- [Installer Contract v1](installer-contract-v1.md)
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)

### Phase 2

- [Backup Contract v1](backup-contract-v1.md)
- [Restore Contract v1](restore-contract-v1.md)
- [Migration Bundle v1](migration-bundle-v1.md)
- Migration Secret Envelope v1（本文）
- [Compatibility Matrix v1](compatibility-matrix-v1.md)
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)

[Release Contract v1](release-contract-v1.md) 与 [Update Agent v1](update-agent-v1.md) 继续拥有 Release/Updater identity；Envelope 不成为 Release artifact 或第二 Release Authority。

## 2. Independent identity 与 versioning

Envelope identity 固定为：

    animemo.migration-secret-envelope/v1

它必须与以下 identity 分开：

- CredentialCipher.version；
- Backup Format version 与 backupId；
- Migration Bundle version 与 bundleId；
- Release Manifest schema、release version/commit/digests；
- Compatibility Matrix version；
- protected config schema。

Outer header 必须能在不解密 secret payload 的情况下识别 format/version/mode/suite和 artifact binding。Unknown envelope version 或 unknown suite 是 UNSUPPORTED；不能把 unknown version 交给旧 parser试解。

Claimed v1 缺 required field、字段类型/边界错误、binding 冲突或 authentication failure 是 CORRUPT。

## 3. Threat model 与不变量

Envelope v1 必须保证：

- 没有 external migration secret 就不能取得 secret payload plaintext；
- 任意 ciphertext、authenticated header 或 bundle binding 变化被 authentication 检出；
- 一个 backup/bundle 的 envelope 不能被无声替换到另一个 artifact；
- wrong key、wrong passphrase、tamper 和 truncated authenticated data 不产生部分 plaintext或 target mutation；
- CREDENTIAL_ENCRYPTION_KEY 在需要时保持 exact bytes；
- existing CredentialCipher ciphertext 不因 migration 被改写；
- public metadata、logs、reports、CI evidence 和 instance locator 不含 secret；
- producer/consumer 对 unknown version/suite fail closed。

Envelope v1 不保证：

- source/target host 被攻陷后的 confidentiality；
- 管理员选择的 passphrase 足够强；
- OS swap/core dump/terminal recorder 不泄露内存或输入；
- unlink 后在 journaling filesystem、snapshot 或 SSD 上完成 forensic secure erase；
- external secret channel 的安全性。

## 4. Logical envelope format

Envelope v1 以 canonical artifact path `secrets/secret-envelope.json` 承载两个逻辑部分。该文件是 strict JSON；ciphertext 使用固定声明的 base64url encoding，具体 canonical JSON/AAD byte serialization由 Phase 3 pinned suite profile和contract tests固定。

### 4.1 Non-secret outer header

Outer header 至少包含：

    format: animemo.migration-secret-envelope
    schemaVersion: 1
    mode: passphrase | one-time-key
    suiteId
    kdf metadata
    aead metadata
    binding:
      artifactType: backup | migration-bundle
      artifactId: backupId | bundleId
      artifactBindingDigest: sha256:...
    ciphertextEncoding: base64url

Outer header、KDF salt/parameters、AEAD nonce、suite identifier、artifact ID、binding digest与ciphertext encoding是 non-secret，但必须作为 canonical AAD 被认证。`ciphertext` 字段本身不属于 AAD；AEAD tag直接认证其内容，外层 Backup/Migration checksum再覆盖完整 `secret-envelope.json` bytes。这样不会把由ciphertext派生的size/checksum放回其自身AAD。

`artifactBindingDigest` 不是最终 Manifest digest。它是对预先冻结的 canonical artifact binding record 计算的 SHA-256；该 record 至少包含 artifact type/format/schema、artifact ID、source instance identity、exact release identity、database/filesystem payload digest root 与 secret profile identity，并明确排除 Envelope ciphertext、Envelope checksum、最终 Manifest checksum 和 finalize timestamps。最终 Manifest 必须记录完整 binding record、`artifactBindingDigest` 与 Envelope checksum。这样避免 Manifest 包含 Envelope checksum、而 Envelope AAD 又依赖最终 Manifest digest所形成的循环依赖。

Outer header不得包含：

- secret value、单项 secret length、prefix/suffix或hash；
- configured provider/secret name inventory；
- CREDENTIAL_ENCRYPTION_KEY；
- migration passphrase或one-time key；
- database/Redis/GitHub/GHCR credential；
- OAuth token/state、setup code或recovery code；
- plaintext payload checksum。

### 4.2 Authenticated ciphertext

同一 JSON 的 `ciphertext` 字段解码后必须得到 AEAD ciphertext；解密后得到 strict、versioned inner payload。Plaintext parser 只能在 AEAD authentication成功后运行。

Inner payload 至少在语义上包含：

    payloadSchemaVersion
    source instance identity
    artifact binding identity
    allowlisted secret entries
    each entry disposition

Inner payload不允许 arbitrary env dump、unknown key passthrough、shell fragment或自由路径。Unknown secret name必须使 producer以 UNCLASSIFIED_SECRET fail closed，不能自动 include或exclude。

Exact canonical JSON/AAD byte serialization由 Phase 3 implementation pin并测试，但不得改变本节 logical fields、single-file path和binding。

## 5. CREDENTIAL_ENCRYPTION_KEY

CREDENTIAL_ENCRYPTION_KEY 是现有 CredentialCipher v1 解密数据库 credential ciphertext 的 root secret。

它在 migration/restore 需要 credential continuity 时 MUST 被原样保留。

它 MUST NOT：

- 作为 Envelope AEAD key；
- 作为 passphrase；
- 作为 KDF input、salt或参数；
- 派生 external migration key；
- 加密一个包含 CREDENTIAL_ENCRYPTION_KEY 自身的 payload或bundle；
- 出现在 outer header、Manifest、checksum、logs、reports、instance.json或CLI output。

### 5.1 Required detection

存在任一 non-empty CredentialCipher-protected database field、protected config声明 credential continuity，或 producer无法证明不存在 encrypted credential时，CREDENTIAL_ENCRYPTION_KEY 必须视为 required。

不得只依赖 credential_key_version 或 v1: ciphertext prefix决定 key是否需要。当前兼容数据可能没有 prefix，且并非所有 encrypted field 都有独立 key-version column。

Producer 必须在创建 Envelope 前验证 source key 可以解密所有 required allowlisted CredentialCipher records，但不得输出 plaintext、ciphertext、record identifier或secret count。

如果 source key缺失或任一 required ciphertext无法解密，Envelope generation必须失败；不得产生“成功但需要重新授权”的完整 migration bundle。

### 5.2 Ciphertext continuity

Migration Secret Envelope v1 不抽取、解密后重写或重新加密数据库内现有 CredentialCipher ciphertext。

Target 在启动 API/Web 前必须：

1. 从 authenticated payload把原 key写入受保护 target config staging；
2. 使用 staged key验证 existing required ciphertext可解密；
3. 不输出任何 decrypted value；
4. 验证全部成功后才原子 publish config并允许服务启动。

验证失败必须保持旧 target config不变并返回稳定 authentication/credential continuity error。

## 6. External migration secret modes

Envelope v1 只定义两种 inline-envelope key source：

### 6.1 Passphrase mode

- Passphrase 来自管理员的独立 migration input，不得复用任一 AniMemo instance secret。
- Producer 必须使用 mature cryptographic library提供的 memory-hard password KDF。
- 每个 Envelope 使用 CSPRNG 生成、至少 128-bit 的 random salt。
- KDF algorithm identifier和全部参数必须在 outer header声明并被 AAD认证。
- 参数必须经过上下界验证；consumer 在执行高成本 KDF前拒绝缺失、溢出或超出实现资源上限的值。
- 实现不得使用 raw hash、单次 SHA、CredentialCipher key或未经review的自制KDF。

### 6.2 One-time-key mode

- One-time key必须由独立 CSPRNG生成，具有所选 AEAD suite要求的完整高熵安全强度。
- 不从 hostname、timestamp、release identity、instance ID、password、CREDENTIAL_ENCRYPTION_KEY或其他instance secret派生。
- Password KDF不得用于把低熵输入伪装成one-time key。
- Key通过bundle之外的受保护channel交付，永不写入Envelope或public metadata。

### 6.3 Reference/KMS boundary

Backup Contract允许 secret-reference.json 表达 external secret reference/KMS模式。该模式不是 inline Migration Secret Envelope v1。

Reference resolution、authorization和provider lifecycle由独立 reference implementation/contract负责；不能把unknown reference当作passphrase/one-time-key Envelope。Unknown/unsupported reference provider 或不存在安全 resolution path 时，对有效 artifact 是 UNSUPPORTED；已支持 provider 只是暂时 unavailable 时是 operational evaluation error、无 compatibility decision。两者都不是 Envelope CORRUPT。

## 7. KDF 与 AEAD suite policy

本文冻结安全属性，不设计home-grown primitive或在Phase 2猜测具体参数。

Phase 3 producer在首次输出 animemo.migration-secret-envelope/v1 前必须：

1. 从维护成熟、接受安全评审的library选择一个memory-hard passphrase KDF；
2. 选择一个mature standard AEAD，整体security strength至少128 bit；
3. 冻结suiteId、key length、nonce length、tag semantics、KDF参数policy和consumer bounds；
4. 记录选择依据和library/API版本；
5. 通过known-answer、wrong-key、tamper、nonce、bounds和cross-artifact swap tests；
6. producer每次只输出该pinned reviewed suite。

Consumer只接受其显式实现并qualification过的suiteId。Unknown suite/version返回UNSUPPORTED，不尝试fallback或“best effort decrypt”。

Suite migration必须通过新的Envelope version或显式REQUIRES_UPGRADE转换完成；不得在相同suiteId下改变primitive、key derivation或nonce semantics。

## 8. AEAD 与 canonical AAD

AEAD要求：

- key来自第6节external source；
- 每次encryption使用符合suite要求的CSPRNG unique nonce；
- 同一derived/one-time key下不得reuse nonce；
- plaintext在authentication成功前对caller不可见；
- tag failure不返回partial plaintext；
- ciphertext/tag/nonce/header任何变化均导致authentication failure。

Canonical AAD至少绑定：

- animemo.migration-secret-envelope/v1；
- mode与suiteId；
-完整KDF和AEAD non-secret parameters；
- artifactType；
- exact backupId或bundleId；
- exact canonical `artifactBindingDigest`；
- canonical Envelope path与ciphertextEncoding。

AAD canonicalization必须由contract tests固定。不得依赖JSON object insertion order、locale、平台path separator或非canonical whitespace。

Checksums可以检测transport damage并参与Backup/Migration Manifest，但checksum不是secret authentication。Consumer必须同时验证最终 artifact checksum、重算 `artifactBindingDigest` 并验证 AEAD。

## 9. Secret allowlist 与 disposition

Producer必须按名称和用途建立strict allowlist；不得导出整个env或整个credential directory。

### 9.1 PRESERVE

- CREDENTIAL_ENCRYPTION_KEY：required时exact preserve；
- 现有database CredentialCipher ciphertext：保留在logical database member中，不复制到Envelope、不改写；
- 为保持同一instance cryptographic/application identity而经Backup/Migration Contract审核为required的application signing material。

### 9.2 PRESERVE_OR_EXPLICIT_RECONFIGURE

- DJANGO_SECRET_KEY；
- application/provider client secret；
- Resend/mail credential；
- Turnstile secret；
- R2 access/secret/analytics credential；
- Integration connection secret；
- 其他由protected config schema明确列出的durable application secret。

每项必须由Backup/Migration Plan声明preserve还是target reconfigure。选择reconfigure时，Plan必须证明不会使existing encrypted state、callback、media reference或Memory Integrity静默失效。

### 9.3 TARGET_LOCAL

以下默认由target管理员重新配置，不进入Envelope：

- target PostgreSQL password/DSN credential；
- target Redis credential；
- Host GitHub credential；
- Host GHCR/Docker credential；
- host/provider control-plane credential。

若未来Contract要求迁移其中任一项，必须先改变allowlist/disposition并进行独立security review，不能通过unknown env passthrough。

### 9.4 NEVER_INCLUDE

- migration passphrase；
- one-time migration key；
- OAuth transient state、authorization code、PKCE verifier；
- first-run setup code；
- password reset/registration token；
- active session、access/refresh token snapshot；
- temporary CI/test credential；
- shell history、command line或raw env dump；
- GitHub/GHCR credential stores。

Public Manifest只声明secretMode、Envelope format/version/suite、binding、完整 Envelope file size/checksum和是否满足required secret profile。它不列出实际configured secret names、provider identities、values、lengths或hashes。

## 10. Input handling

Passphrase或one-time key只允许通过：

- no-echo interactive TTY；
- inherited/protected file descriptor；
- caller显式提供的root-owned regular file，mode 0600，且拒绝symlink、junction和hard link。

禁止：

- CLI argv；
- plain environment variable；
- command substitution；
- process title；
- shell history；
- stdin pipeline whose provenance/echo cannot be controlled；
-日志、exception、audit detail、CI output或artifact；
- world/group-readable file。

Interactive passphrase creation必须要求两次constant-behavior input并在本地比较，避免拼写错误生成不可恢复artifact。比较失败不创建Envelope。

Non-interactive operation必须使用protected FD或0600 file；--non-interactive本身不授权从普通env读取。

实现应限制input length并在KDF前验证，不得把unbounded passphrase/one-time-key material写入诊断。

## 11. Producer lifecycle

固定顺序：

    Discover exact source instance and artifact identity
    → Build canonical artifact binding record and artifactBindingDigest
    → Build audited secret inventory and dispositions
    → Validate required CREDENTIAL_ENCRYPTION_KEY continuity
    → Acquire external migration secret
    → Build inner payload in protected memory/staging
    → Derive/select AEAD key
    → Generate salt/nonce and canonical AAD
    → Encrypt
    → Authenticate by immediate in-process verification
    → Atomically publish encrypted Envelope
    → Add only public Envelope metadata/checksum to Backup/Migration Manifest
    → Cleanup plaintext and external-secret handles

Producer在Envelope finalization后不得修改 artifact ID、binding record、artifactBindingDigest、其他 AAD fields或ciphertext。任何这些字段变化必须重新生成Envelope和artifact identity。最终 Manifest只能在随后加入Envelope checksum、finalize timestamp等明确排除于binding record的字段；一经finalize即不可变。

Secret inventory、database dump和protected config必须来自同一Backup/Migration consistency window。若在capture期间检测到secret/config generation变化，整个operation失败并重试，不能把不同时点的key与ciphertext组合。

## 12. Consumer lifecycle

固定顺序：

    Verify Backup/Migration outer format and checksums
    → Parse bounded non-secret Envelope header
    → Evaluate Envelope version/suite compatibility
    → Recompute canonical artifact binding record、artifactBindingDigest and AAD
    → Acquire external migration secret
    → Run bounded KDF or validate one-time key
    → Authenticate and decrypt in protected memory/staging
    → Validate strict inner schema, allowlist and dispositions
    → Stage target protected config
    → Validate existing CredentialCipher decryptability
    → Atomically publish config
    → Start only AniMemo-scoped validation/runtime
    → Cleanup

在authentication与inner validation完成前，不得写target config、database、instance locator、systemd或service state。

Consumer不得因为target env中已经存在同名secret而跳过Envelope authentication。Existing target secret冲突必须由Restore/Migration Plan显式选择preserve target、replace或abort。

## 13. Wrong key、tamper 与 error contract

Wrong passphrase、wrong one-time key、ciphertext tamper、AAD mismatch和AEAD tag failure对外只返回：

    reasonCode: ENVELOPE_AUTHENTICATION_FAILED
    compatibility status: CORRUPT

不得区分：

- passphrase是否正确；
- Envelope是否被篡改；
- 哪个secret存在；
- 哪个record无法解密；
- plaintext是否通过部分解析。

该结果必须：

- 无partial plaintext；
- 无target secret/config mutation；
- 无fallback到plaintext、legacy cipher、other suite或target existing key；
- 无secret-bearingexception/log；
- 保持source和target执行前状态。

Operational input unavailable、permission error或external key channel unavailable不产生compatibility decision，按Compatibility Matrix evaluation error fail closed。

Unknown valid Envelope version/suite为UNSUPPORTED。存在显式、authenticated converter时可为REQUIRES_UPGRADE。只有完整验证并可直接消费时为COMPATIBLE。

## 14. Temporary files 与 cleanup

Plaintext优先只存在process memory。Python/OS无法保证一般memory zeroization，文档不得声称已secure erase。

若plaintext staging不可避免：

- 使用dedicated root-owned 0700 local staging directory；
- 使用exclusive create的0600 regular file；
- 拒绝symlink、junction、hard link、device、FIFO和socket；
- 不使用shared/world-readable temp；
- 不跨filesystem做非原子secret publish；
- fsync file，验证后atomic replace到protected config；
- success、failure、signal和exception路径都cleanup；
- cleanup failure必须报告manual recovery required并保留path的redacted locator，不显示内容。

Cleanup只能unlink本次operation以unique staging identity创建的文件。不得递归删除unknown directory或existing target config。

Encrypted Envelope本身是Backup/Migration artifact，可按其retention policy保留。External passphrase/one-time key不由AniMemo持久化。

## 15. Logging、report 与 redaction

允许输出：

- status/reasonCode；
- Envelope format/version/mode/suiteId；
- backupId/bundleId；
- Manifest/Envelope file checksum identity；
- stage名称；
-是否发生mutation；
- redacted recovery instruction。

禁止输出：

- passphrase、one-time key、CREDENTIAL_ENCRYPTION_KEY；
- secret payload、ciphertext全文或decrypted值；
- configured secret name/provider inventory；
- secret length/hash/prefix/suffix；
- raw env、argv、stdin/FD content；
- Authorization/Cookie/credential URL；
- plaintext temp content；
- exception chain中包含的crypto input。

Migration implementation必须在structured logging boundary显式deny secret fields，不能只依赖generic regex redactor。至少应把credential_ciphertext、所有encrypted/config secret fields、Envelope plaintext/ciphertext和external-secret handles作为sensitive types/keys。

Audit只能记录presence/result，例如PRESENT、MISSING、VALID、INVALID、AUTH PASS、AUTH FAIL，不记录值。

## 16. Compatibility Matrix mapping

| Condition | Matrix result | Reason |
| --- | --- | --- |
| v1 header/schema/suite supported，binding与authentication通过，required payload完整 | COMPATIBLE | ENVELOPE_COMPATIBLE |
| 完整authenticated Envelope需要已批准converter | REQUIRES_UPGRADE | ENVELOPE_UPGRADE_REQUIRED |
| 有效future version或unknown suite | UNSUPPORTED | ENVELOPE_VERSION_UNSUPPORTED / ENVELOPE_SUITE_UNSUPPORTED |
| Claimed-v1 malformed、binding mismatch、wrong key或tamper | CORRUPT | ENVELOPE_AUTHENTICATION_FAILED或ENVELOPE_STRUCTURE_CORRUPT |
| Key input/storage/verifier暂时不可用 | no decision | operational evaluation error |

CORRUPT优先于UNSUPPORTED；但consumer在不了解future suite时不得尝试authentication并伪称CORRUPT。可识别且完整的future version/suite保持UNSUPPORTED。

## 17. Existing noncanonical tools

scripts/dr_backup.py 与isolated DR rehearsal是historical test/evidence helper。它排除production env并要求operator另行提供original CREDENTIAL_ENCRYPTION_KEY；它不是Migration Secret Envelope producer/consumer，不得被包装后直接作为v1.1 runtime。

AniMemo Data Bundle v1是用户portable export/import并明确排除credential ciphertext、OAuth state和authenticated connections。它不是Backup/Migration secret carrier。

Updater的credential stores、GitHub CLI config、Docker config、runtime-images.env和Operation journal不是Envelope payload。整个updater-state目录不得作为secret bundle复制。

现有CredentialCipher/Fernet只保护database-backed credential at rest。它不能复用为一个包含CREDENTIAL_ENCRYPTION_KEY自身的migration envelope。

## 18. CURRENT → TARGET gaps

只使用以下分类：ALREADY SATISFIED、DOCUMENTATION GAP、IMPLEMENTATION DEFERRED、CONTRACT CONFLICT、RELEASE CONTRACT REVIEW NEEDED。

| Area | CURRENT | TARGET | Classification |
| --- | --- | --- | --- |
| Database credential at-rest protection | CredentialCipher v1 authenticated encryption已存在 | ciphertext原样保留 | ALREADY SATISFIED |
| Production key validation | 启动时要求有效CREDENTIAL_ENCRYPTION_KEY | source/target continuity precheck | ALREADY SATISFIED |
| Self-encryption prohibition | Phase 1已冻结key不得加密包含自身的bundle | 本文完整规范化 | ALREADY SATISFIED |
| Envelope format/AAD/errors | 无canonical Envelope | animemo.migration-secret-envelope/v1 | DOCUMENTATION GAP |
| Producer/consumer runtime | 无KDF/AEAD Envelope implementation | Phase 3实现 | IMPLEMENTATION DEFERRED |
| Concrete suite/parameters | 只有现有cryptography dependency，无migration suite | producer pin reviewed suite与tests | IMPLEMENTATION DEFERRED |
| Key rotation/key ring | 单一active key，version metadata不统一消费 | v1 preserve exact key；未来rotation另行设计 | DOCUMENTATION GAP |
| Secret inventory/dispositions | Secret分散于protected env/config与database fields | strict allowlist + disposition | DOCUMENTATION GAP |
| Secure temp primitives | Updater已有0600 atomic temp/cleanup模式 | Envelope专用staging复用安全属性 | ALREADY SATISFIED |
| Migration redaction | Updater通用redaction覆盖passphrase/encryption-key，但非全部ciphertext字段名 | structured denylist与sensitive types | IMPLEMENTATION DEFERRED |
| DR helper | 已明确noncanonical、env/key外置 | 继续不作为Envelope runtime | ALREADY SATISFIED |
| Data Bundle | 已排除credentials | 继续作为Export，不承载Envelope | ALREADY SATISFIED |
| Entire updater-state copy | 旧helper可整树复制，但已明确为noncanonical；Phase 1禁止credential store明文迁移 | Backup/Migration strict allowlist | DOCUMENTATION GAP |
| Release Manifest boundary | Envelope不是Release metadata，Manifest v1拒绝unknown fields | 保持artifact-owned version，不扩展Manifest v1 | ALREADY SATISFIED |

未来若提议把Envelope加入Release Manifest或Release assets，分类才是RELEASE CONTRACT REVIEW NEEDED。Backup/Migration artifact内使用本Envelope不需要改变Release Contract。

Deferred implementation本身不是Contract conflict。CONTRACT CONFLICT仅指把旧whole-updater-state复制语义用于新的canonical Backup/Migration。

## 19. Memory Integrity mapping

- **MI-1:** provider或external metadata不可用不得使producer删除对应credential ciphertext、secret entry或用户memory；Envelope只保护已审计allowlist，不解释业务删除。
- **MI-2:** provider/backend identity变化必须由Migration Plan显式映射；Envelope不得按secret name相似性重绑credential并静默orphan memory。
- **MI-3:** Envelope不执行identity merge；任何未来merge必须保留历史binding/provenance与全部required secret continuity。
- **MI-4:** unknown secret name、future payload schema或unsupported suite必须fail closed为UNCLASSIFIED_SECRET、UNSUPPORTED或REQUIRES_UPGRADE；不得静默遗漏后生成“完整”artifact。
- **MI-5:** source key、target secret冲突、artifact binding或disposition存在destructive ambiguity时必须在target mutation前停止并要求explicit plan；不得猜测replace/preserve。

## 20. Acceptance criteria

Migration Secret Envelope v1只有在以下条件全部满足时为PASS：

- identity精确为animemo.migration-secret-envelope/v1；
- outer header non-secret且与ciphertext共同authenticated；
- inner payload versioned、strict allowlisted；
- required CREDENTIAL_ENCRYPTION_KEY exact preserve；
-该key从不作为Envelope key/KDF input，也不自加密；
- external source只有no-echo passphrase或independent CSPRNG one-time key；
- passphrase KDF为mature-library memory-hard KDF，salt至少128 bit，参数声明且有bounds；
- one-time key具有suite要求的独立高熵；
- AEAD是mature standard primitive、至少128-bit security、nonce unique；
- canonical AAD绑定Envelope version、backupId/bundleId和无循环依赖的artifactBindingDigest；
- Phase 3 producer在emit v1前pin一个reviewed concrete suite并通过contract tests；
- unknown version/suite为UNSUPPORTED；
- wrong key与tamper对外相同ENVELOPE_AUTHENTICATION_FAILED/CORRUPT；
- authentication前无plaintext parse或mutation；
- input不进入argv、plain env、logs或CI；
- plaintext优先memory-only；fallback staging为0700/0600、exclusive、atomic、可清理；
-不声称secure erase；
- existing database ciphertext不改变，target启动前验证decryptability；
- target-local和never-include secret不进入Envelope；
- public metadata不泄露secret inventory/value/length/hash；
- DR helper、Data Bundle和entire updater-state明确noncanonical；
-没有runtime、Release或production mutation被本Contract Freeze隐式授权。

任一条件失败，Migration Secret Envelope v1 acceptance为FAIL。
