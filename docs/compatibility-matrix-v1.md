Status: FROZEN FOR v1.1

Version: v1

# AniMemo Compatibility Matrix v1

Scope: 冻结 Installer、Updater、Backup、Restore、Migration 与 Doctor 共用的 compatibility vocabulary、机器判定结构、维度、求值顺序、聚合规则和 fail-closed 行为。

Definitions: Compatibility Matrix v1 的独立机器身份是 animemo.compatibility/v1。Compatibility decision 是对一个 exact artifact、一个 exact target 和一个具体 operation 的四态判定；它不是 Release、Backup、Migration Bundle 或 Secret Envelope 自身的版本。

Non-goals: 本文不实现 compatibility engine，不运行 Restore/Migration，不发布发行版或平台支持声明，不定义完整 Linux distribution、Docker、Compose 或 PostgreSQL version floor，也不修改 Release Manifest v1。

Dependencies: 继承 Phase 1 的 Deployment Boundary、Filesystem Layout、Installer、Public Origin / Listen Contract；与 Phase 2 的 Backup、Restore、Migration Bundle、Migration Secret Envelope 和 Doctor Basic Contract 共同定义 durability interface。

Security / Integrity implications: 错误的 compatibility decision 可能导致不可恢复的数据、credential 失效、错误 release 启动或不安全 migration。所有判定必须绑定 exact artifact identity，缺少证据时 fail closed；不得用 “probably works”、UNKNOWN 或人工猜测替代机器证据。

Compatibility: 保持 Release Contract v1、Update Agent v1 的 Safe Switch/Application Rollback/Unsafe Downgrade、Plugin SDK v2、First-run identity 与 updater fail-closed 行为不变。Matrix 是跨工具上层判定，不重命名或放松既有 Updater operation decision。

Change policy: 四种 public status、聚合优先级、固定求值顺序、matrix identity、dimension identity 或 machine-readable shape 的破坏性改变必须提升 Matrix 版本。新增 reason code 或 additive dimension 只有在旧消费者可以 fail closed 且不改变既有结果时才可保持 v1。

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
- [Migration Secret Envelope v1](migration-secret-envelope-v1.md)
- Compatibility Matrix v1（本文）
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)

[Release Contract v1](release-contract-v1.md) 与 [Update Agent v1](update-agent-v1.md) 继续拥有 exact Release identity、Release Manifest compatibility、CURRENT/PREVIOUS 和 update/rollback safety。

## 2. Identity 与 versioning

Matrix identity 固定为：

    animemo.compatibility/v1

下列版本彼此独立，不得混为一个数字或从 AniMemo SemVer 猜测：

- Matrix version；
- AniMemo release version；
- Release Manifest schema；
- deployment contract schema/digest；
- database/configuration contract IDs；
- Backup Format version；
- Migration Bundle version；
- Migration Secret Envelope version/suite；
- Installer Contract 与 instance locator schema；
- Updater binary/state schemas；
- Plugin Manifest/SDK API/runtime；
- PostgreSQL logical dump profile。

AniMemo release SemVer 是 immutable release identity 与排序输入，不单独证明 database、configuration、backup、migration、secret、plugin 或 platform compatibility。

Artifact format 必须拥有自己的 format/schema version。Compatibility Matrix、Backup、Migration Bundle 或 Secret Envelope 不得为了方便向 Release Manifest v1 添加字段。若确实需要改变 Release Manifest schema、Release asset allowlist 或已有 Release consumer 语义，必须单独进行 Release Contract review。

## 3. 唯一 public statuses

Compatibility Matrix 只允许以下四种 public status：

### COMPATIBLE

Artifact 完整、已认证，target 可以按当前 contract 直接安全消费，不需要格式转换、release hop、database/config migration 或 secret 重封装。

### REQUIRES_UPGRADE

Artifact 完整、已认证，target 不能直接消费，但存在明确受支持、有限、单调、可验证的转换或 release migration path。

REQUIRES_UPGRADE 必须同时给出完整 ordered actions；没有 exact path 时不能使用该状态。

### UNSUPPORTED

Artifact 完整且可验证，但 format、schema、contract、platform、runtime、plugin、secret suite、PostgreSQL profile 或 required dependency 不受 target 支持，且不存在已批准的安全路径。

有效的 future format 或 unknown suite 是 UNSUPPORTED，不是 CORRUPT。

### CORRUPT

Claimed format 的 required structure、checksum、authenticated integrity、member identity、path safety、gzip/database stream 或 cross-member binding 失败。

声称为 v1 却缺少 required field、重复 member、checksum mismatch、Manifest identity 冲突或 Secret Envelope authentication failure 都是 CORRUPT。

## 4. 禁止 UNKNOWN

不存在 UNKNOWN public status。

以下 operational evaluation failure 不产生 compatibility decision：

- artifact 因权限、I/O、网络、timeout 或 unavailable storage 无法读取；
- evaluator 自身依赖、Release Authority 或 verifier 暂时不可用；
- target 状态无法安全采集；
- evaluation 在完成 required dimensions 前中断。

此时必须返回独立的 evaluation error、overall status 缺失，并阻断 operation。工具不得把 operational failure 映射为 COMPATIBLE、UNSUPPORTED、CORRUPT 或 “unknown but continue”。

Artifact bytes 已经可读但 claimed-v1 framing/structure 截断或无效是 CORRUPT。具有有效 self-identifying framing、可验证 integrity 且声明 future version 的 artifact 是 UNSUPPORTED。

## 5. 固定 evaluation order

所有 Installer、Updater、Backup verification、Restore、Migration 与 Doctor compatibility evaluation 必须按以下顺序：

    1. Bounded and readable format
    → 2. Integrity and authentication
    → 3. Deployment contract
    → 4. Database and configuration schema/contracts
    → 5. Exact release identity
    → 6. Platform and runtime
    → 7. Required supported path

不得为了尽早得到 “compatible” 而跳过前序阶段。

### 5.1 Bounded and readable format

- 在分配大内存、解压、KDF 或数据库解析前执行 size/count/depth/path bounds。
- 验证 format magic/name、schema version、required top-level shape 和 canonical member identity。
- Unknown future format 只有在 stable outer framing 足以证明它是完整 future artifact 时才是 UNSUPPORTED。
- Claimed-v1 malformed/truncated structure 是 CORRUPT。

### 5.2 Integrity and authentication

- 验证 manifest/checksum set、每个 member、gzip/database stream 和 cross-member identity。
- Secret Envelope 必须先完成 AEAD authentication，再解析 plaintext。
- checksum 只证明 bytes 一致性，不能替代 authenticated encryption、Release provenance 或 secret reference authorization。
- 任何 integrity/authentication failure 立即产生 CORRUPT，后续 dimension 不得把它降级。

### 5.3 Deployment contract

- 验证 deployment contract schema、canonical digest、declared files 与 artifact/Release identity。
- instance locator、deployment profile、canonical roots、Compose 与 systemd allowlist 必须一致。
- 有效但 target 不支持的 deployment contract/profile 是 UNSUPPORTED；存在已冻结 explicit cutover 时可为 REQUIRES_UPGRADE。

### 5.4 Database and configuration schema/contracts

- 使用 Release Manifest/Backup/Migration metadata 中的 database 与 configuration contract IDs。
- 目标应用必须显式声明 appAccepts；不得按字符串前缀或版本大小猜测。
- Django migration filename/number 不是 database contract version。
- Django migration snapshot 只可作为 crash reconcile 或 ordered migration path 的执行证据，不能取代 contract ID。
- breaking-blocked 或不存在受支持 forward path 时为 UNSUPPORTED。

### 5.5 Exact release identity

- 绑定 release version、channel、release.commit、provenance.sourceCommit、Manifest identity、deployment contract digest 和 API/Web exact OCI digests。
- Release metadata/cached Manifest/embedded image 不是第二 Release Authority。
- 需要取得 application/deployment bytes 时仍从正式 GitHub Release + GHCR exact digest authority 重新验证。
- Release exact identity 无法验证且没有已冻结 recovery-compatible path 时为 UNSUPPORTED；authority 暂时不可访问则是 evaluation error，不产生 status。

### 5.6 Platform and runtime

- 当前标准 server profile 只支持 Linux/amd64。
- Host 必须满足 Linux、systemd、Docker daemon、Compose v2 与所需 local verifier/capability boundary。
- 非 Linux、非 amd64 或非 standard server profile 为 UNSUPPORTED。
- 精确 distribution、kernel、Docker、Compose、PostgreSQL tool/server version 支持声明必须来自后续 qualification evidence；本文不发明 version floor。

### 5.7 Required supported path

- 评估 minimum Updater、Updater state schema、enabled Plugin SDK APIs/runtime、Backup/Migration/Envelope converter、PostgreSQL logical restore path、required exact release hops 和 secret/media dependency。
- REQUIRES_UPGRADE 只允许已知、有限、ordered、每一步均可验证且没有 breaking-blocked transition 的路径。
- 任一步依赖未知工具、隐式 reverse migration、不可验证 release、unsupported plugin/runtime 或 secret loss 时为 UNSUPPORTED。

## 6. Aggregate precedence

Per-dimension status 的固定聚合优先级为：

    CORRUPT
    >
    UNSUPPORTED
    >
    REQUIRES_UPGRADE
    >
    COMPATIBLE

只要任一 required dimension 为 CORRUPT，overall 必须为 CORRUPT。

不存在 CORRUPT 但任一 required dimension 为 UNSUPPORTED，overall 必须为 UNSUPPORTED。

全部 required dimensions 至少受支持，但任一需要明确 action，overall 为 REQUIRES_UPGRADE。

只有全部 required dimensions 均为 COMPATIBLE 且无 required action 时，overall 才是 COMPATIBLE。

Optional informational probe 失败不得被伪装成 required dimension PASS；它应作为非 decision diagnostic 单独报告。

## 7. Machine-readable decision shape

Canonical logical shape：

    {
      "matrixVersion": "animemo.compatibility/v1",
      "operation": "install|update|backup|restore|migration|doctor",
      "overallStatus": "COMPATIBLE|REQUIRES_UPGRADE|UNSUPPORTED|CORRUPT",
      "artifact": {
        "format": "...",
        "schemaVersion": 1,
        "artifactId": "...",
        "manifestDigest": "sha256:..."
      },
      "dimensions": [
        {
          "name": "...",
          "source": {},
          "target": {},
          "status": "COMPATIBLE|REQUIRES_UPGRADE|UNSUPPORTED|CORRUPT",
          "reasonCode": "STABLE_MACHINE_CODE"
        }
      ],
      "actions": [
        {
          "order": 1,
          "kind": "...",
          "inputIdentity": {},
          "outputIdentity": {},
          "requiredReleaseIdentity": {}
        }
      ],
      "evaluatedArtifactIdentity": {}
    }

Requirements：

- matrixVersion 必须精确等于 animemo.compatibility/v1；
- operation 必须是固定枚举；
- overallStatus 必须按第 6 节计算，不能由 caller 提供；
- dimensions 必须覆盖当前 operation 的所有 required dimensions，顺序稳定；
- source/target 只含 non-secret canonical identities/capabilities；
- reasonCode 稳定、机器可读，不以自由文本作为控制输入；
- REQUIRES_UPGRADE 必须有非空、连续 order 的 exact actions；
- COMPATIBLE、UNSUPPORTED、CORRUPT 不携带可执行 upgrade actions；
- evaluatedArtifactIdentity 必须足以防止 plan 被用于另一 artifact；
- 人类 detail 可以 additive 提供，但不能改变 status。

Evaluation error 使用独立 error envelope，不得生成以上 decision shape 或填入 UNKNOWN。

## 8. Required dimensions

| Dimension | Source identity | Target capability | 关键规则 |
| --- | --- | --- | --- |
| Release | version/channel、release.commit、provenance.sourceCommit、Manifest digest、API/Web digests | 可重新验证 exact Release 并运行该 platform image | SemVer 不是单独 compatibility proof |
| Deployment contract | schema version、contract digest、declared file identities、profile | 支持的 deployment schema/profile/roots | locator/Compose/systemd 不一致 fail closed |
| Database | database contract、logical dump profile、PostgreSQL source server/tool major | appAccepts、supported import/forward migration、target PostgreSQL major | 不使用 Django migration number 作为 version |
| Configuration | configuration contract、protected config schema | appAccepts 与 atomic config migration | 不猜 legacy alias |
| Backup | format animemo-instance-backup、schemaVersion、backupId、checksum-set digest | Backup Format v1 parser/verifier | Data Bundle/Update Safety Backup 不是 instance backup |
| Migration Bundle | bundle format/schema、bundleId、Manifest digest | Migration Bundle v1 parser/verifier | 与 Backup/Export 分离 |
| Secret Envelope | animemo.migration-secret-envelope/v1、suite、authenticated binding | 支持的 envelope version/suite 与 external secret availability | wrong key/tamper 为 CORRUPT |
| Installer / locator | Installer Contract version、instance.json schema、deployment profile、roots | matching Installer/Doctor/Updater reader 与 systemd allowlist | custom/legacy profile 必须显式 |
| Updater | Updater SemVer、release slots/runtime/operation schemas、pending state | 满足 Manifest minimumUpdaterVersion，理解 state schema | 不丢弃 PENDING/history |
| Plugin | Manifest schema 2、enabled SDK APIs、trusted-in-process runtime | target supportedApis/runtime | 已启用集合必须是子集 |
| Platform | linux/amd64、standard server profile | Linux/amd64 与 required host capabilities | 其他 server target UNSUPPORTED |
| Runtime | systemd、Docker、Compose capability、filesystem/permission semantics | qualified capability profile | 不在本文发明 version floor |
| PostgreSQL restore | plain logical dump、source server major、pg_dump major/profile | target import/tool major 与 approved path | live data directory archive 永不 compatible |

Redis 是可重建 operational state，不是正式 database backup member；其运行能力属于 runtime dimension，不得把 Redis 持久目录当 Restore database compatibility input。

Container 内 Python、Node、Nginx 等由 exact API/Web release image identity封装。Host Matrix 不要求管理员单独安装这些 image-internal runtimes。

## 9. Operation-specific requirements

### Install

必须覆盖 Release、deployment、Installer/locator、platform/runtime、Updater 和 listen/filesystem capability。Fresh target 没有 Backup/Migration source dimension。

### Update

复用 Update Agent v1 的 live database/configuration/enabled Plugin SDK/minimum Updater checks。Matrix 可以映射结果，但不能把 unsafe_downgrade 变为 warning。

### Backup

source 必须先结构健康且可解释；无法读取 live contract、PENDING state 或 secret disposition 时不产生 decision并失败。Backup creation 本身不把 target Restore rehearsal假设为 COMPATIBLE。

### Restore

严格执行 VERIFY → COMPATIBILITY PLAN → RESTORE。只有 COMPATIBLE，或操作者显式接受完整 REQUIRES_UPGRADE actions，才可 mutation。

### Migration

除 Backup/Restore dimensions 外，必须覆盖 source/target deployment profile、Migration Bundle、Secret Envelope、media/R2 dependency 与 Public Origin/listen reconfiguration。

### Doctor

Doctor 只读产生 compatibility decision和 diagnostics。它不得因诊断结果自动 repair、upgrade、restore、rotate secret 或修改 filesystem。

## 10. Existing noncanonical formats

AniMemo Data Bundle v1 是用户-owned portable export/import，不是 instance Backup、Restore 或 Migration format。它的 schema_version 不能填入 Backup Format 或 Migration Bundle dimension。

scripts/dr_backup.py 与 isolated DR rehearsal 是 historical test/evidence helper，不是 Backup Format v1、Restore v1 或 Migration Bundle v1。其 manifest schema 不得成为 Matrix 的 canonical Backup version。

Updater pre-migration pg_dump 是 Update Safety Backup，不是完整 instance Backup；其 metadata 不能单独得到 Restore COMPATIBLE。

## 11. CURRENT → TARGET gaps

只使用以下分类：ALREADY SATISFIED、DOCUMENTATION GAP、IMPLEMENTATION DEFERRED、CONTRACT CONFLICT、RELEASE CONTRACT REVIEW NEEDED。

| Area | CURRENT | TARGET | Classification |
| --- | --- | --- | --- |
| Release switch compatibility | database/config appAccepts、enabled Plugin SDK、minimum Updater、migration/rollback policy 已机器判定 | 继续作为 update dimension | ALREADY SATISFIED |
| Exact Release identity | Manifest、deployment digest、commit/provenance 与 OCI digest 已冻结 | 所有 durability plan 绑定同一 identity | ALREADY SATISFIED |
| Four-state vocabulary | Backup/Restore 已引用四态，但没有 canonical cross-tool owner | 本文成为唯一 owner | DOCUMENTATION GAP |
| Shared decision engine | 无统一 evaluator | 按固定 order/shape 实现 | IMPLEMENTATION DEFERRED |
| Runtime state version | release slots 有 schema，runtime.json 无 schemaVersion | 所有被 Matrix 消费的 state versioned | IMPLEMENTATION DEFERRED |
| Backup compatibility metadata | 旧 DR/helper 与 Updater safety backup 不同且不完整 | Backup Format v1 artifact-owned metadata | IMPLEMENTATION DEFERRED |
| Migration Bundle / Envelope | 尚无 runtime/parser | 独立 versioned dimensions | IMPLEMENTATION DEFERRED |
| Instance locator/profile | Phase 1 已冻结 logical interface，reader/writer 未实现 | Matrix 读取 verified locator/profile | IMPLEMENTATION DEFERRED |
| Linux/amd64 | Release schema与 Installer 已限制 | standard server profile | ALREADY SATISFIED |
| Exact distro/runtime versions | 当前只验证 capabilities | qualification-backed support claims | DOCUMENTATION GAP |
| PostgreSQL logical compatibility | 当前 Compose 使用 PostgreSQL 16，Backup Contract要求记录 server/tool major | qualified source-target import/path rules | IMPLEMENTATION DEFERRED |
| Data Bundle naming | 历史文案曾使用“备份/恢复”，但 Phase 1 已冻结 Export 与 Backup 分离 | canonical docs 明确为 Export/Import，永不作为 instance format | DOCUMENTATION GAP |
| Release Manifest boundary | v1 schema拒绝 unknown fields | artifact formats拥有自身版本，不扩展 Manifest v1 | ALREADY SATISFIED |

未来若提议改变 Release Manifest、Release asset allowlist 或 consumer authority，分类才是 RELEASE CONTRACT REVIEW NEEDED；实现本文 artifact-owned Matrix 不需要修改 Release Contract。

## 12. Memory Integrity mapping

- **MI-1:** external metadata/provider/R2 暂时不可用不能被解释为用户 memory 已删除或 artifact CORRUPT；应按 dependency evidence 返回 evaluation error、WARN diagnostics或没有安全路径时的 UNSUPPORTED。
- **MI-2:** provider identity变化必须作为显式 identity-mapping dimension；没有 verified mapping时不能得到 COMPATIBLE，也不能静默 orphan memory。
- **MI-3:** identity merge path只有在完整 actions保留历史 references/provenance时才能是 REQUIRES_UPGRADE；Matrix不批准自动丢弃式 merge。
- **MI-4:** future/unknown memory、resource或plugin payload必须为 REQUIRES_UPGRADE或UNSUPPORTED；不得通过忽略字段、只导入已知部分获得COMPATIBLE。
- **MI-5:** ownership、target、locator、member或identity存在destructive ambiguity时fail closed；evaluator不能用UNKNOWN、默认值或force flag替代明确证据。

## 13. Acceptance criteria

Compatibility Matrix v1 只有在以下条件全部满足时为 PASS：

- machine identity 精确为 animemo.compatibility/v1；
- public status 只有 COMPATIBLE、REQUIRES_UPGRADE、UNSUPPORTED、CORRUPT；
- 没有 UNKNOWN 或 operational-error-as-status；
- future valid format 与 claimed-v1 corruption 明确区分；
- evaluation order 与 aggregate precedence 固定；
- machine decision 覆盖 source、target、reason code、artifact identity 和 exact ordered actions；
- app SemVer 与 database/config/artifact versions 分离；
- Django migration filename 不被当作 schema version；
- Backup、Migration Bundle、Secret Envelope 拥有独立版本；
- Release Manifest v1 未被扩展；
- non-Linux/amd64 standard server targets 为 UNSUPPORTED；
- exact distro/runtime/PostgreSQL claims只来自 qualification evidence；
- Data Bundle、DR helper、Updater safety backup 明确 noncanonical；
- CORRUPT/UNSUPPORTED 均不能以 force flag 绕过；
- REQUIRES_UPGRADE 没有 exact path 时不得产生；
- evaluation failure 无 decision并 fail closed。

任一条件失败，Compatibility Matrix v1 acceptance 为 FAIL。
