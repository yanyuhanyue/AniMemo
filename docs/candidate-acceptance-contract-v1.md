# AniMemo 发布前 Candidate Acceptance 合同 v1

状态：v1.1 RC 发布前强制合同。

本合同把“Qualification 产物可验证”与“GitHub Release 已发布”严格分开。Candidate
Acceptance 只证明同一次 Qualification 的完整字节在三种一次性 VM Profile 中通过；它
不创建 Release Authority、生产授权、发布授权或 Stable 晋升资格。

## 1. 固定合同链

```text
exact-main Qualification
-> API/Web 一次构建并导出完整 OCI layout
-> 从 release/dependency-images.json 唯一权威取得 PostgreSQL/Redis OCI layout
-> Candidate Input
-> canonical verifier
-> Verified Candidate Identity v2
-> Candidate-only Installer
-> FRESH_BASE / DOCKER_BASE / RUNTIME_BASE_OFFLINE
-> 三份 Profile Receipt
-> 一份 Aggregate Receipt
-> Metadata Freshness 摘要绑定
-> Publish 再次验证同一摘要
```

每次 canonical verifier 执行还会生成独立的 Verification Execution Receipt；它只单向
引用 Identity v2，作为操作与诊断记录，不进入上述 Authority 链，也不能被 Profile、
Aggregate、Freshness 或 Publish 当作 Candidate 身份。

`.dockerbuild` 是 BuildKit history/debug 产物，不是运行时镜像字节，也不能授予 VM
Acceptance。每个可接受 OCI layout 必须包含 index、authoritative manifest、config 和
全部 layer blobs，并逐摘要闭合；mutable tag、`latest`、远端 registry pointer 和重建都
不被接受。

## 2. Candidate Input 与 canonical verifier

六个 closed JSON Schema 为：

- `animemo.prepublication-candidate-input/v1`
- `animemo.verified-prepublication-candidate/v2`
- `animemo.prepublication-candidate-verification-execution-receipt/v1`
- `animemo.prepublication-candidate-profile-receipt/v1`
- `animemo.prepublication-candidate-acceptance-receipt/v1`
- `animemo.r2-origin-prestate-receipt/v2`

唯一验证入口为：

```text
python -m release.cli verify-prepublication-candidate
```

Verifier 必须同时绑定 repository、workflow name/path/ref、Run ID、attempt 1、head SHA、
head tree、required jobs、精确 Artifact ID/API digest、Candidate Input、Release Notes、
Manifest、Deployment Contract、Installer Materials、checksums 与四套 OCI DAG。产物只能
落到 `/var/lib/animemo/prepublication-candidates/v2/<candidate-input-digest-hex>/`；
`--verified-candidate-digest` 只能在这些 verifier-owned roots 中唯一定位同摘要 Evidence，CLI 不接受任意
本地目录。

Identity v2 只包含 Candidate Input 的不可变字段及确定性派生结果，采用 UTF-8、稳定键序、
无 BOM、末尾恰好一个 LF 的 canonical JSON。它绑定完整 runtime file inventory、其总摘要、
每个 OCI role 的 inventory 摘要、manifest/config/layer digest，并固定所有 release、production
和 publish authority 为 false。`generated_at` 只由 Candidate Input 自身摘要间接绑定；
`verified_at`、当前时钟、绝对/临时路径、主机、用户、PID、UUID、mtime、locale 和 timezone
不得进入 Identity。

`--verified-at` 仅供 Execution Receipt 使用，并规范化为 UTC RFC3339、固定六位微秒、`Z`
结尾。Receipt 绑定 Candidate Input 摘要和 Identity v2 摘要，记录非敏感检查计数，固定
`identity_authority_granted=false`、`release_authority_granted=false`、
`production_authorized=false`、`publish_authorized=false`，并带 canonical body 自摘要。
完整 Receipt 文件按其 SHA256 追加到
`<identity-root>/verification-receipts/<receipt-digest-hex>/verification-execution-receipt.json`；
Receipt 摘要永远不能替代 `--verified-candidate-digest`。

ZIP 解包拒绝绝对路径、父目录逃逸、Windows drive path、重复路径、大小写碰撞、链接、
特殊文件、未知成员和尺寸超限。写入采用 exclusive/atomic 语义；同一摘要不同字节失败。
Identity 目标已存在且字节相同才幂等返回 `existing=true`；不同字节继续以
`VERIFIED_CANDIDATE_OUTPUT_CONFLICT` 失败关闭。Receipt 目标同样只允许原子新建或
same-byte 幂等，绝不覆盖。旧 v1 Identity 仅保留为历史取证格式，新 main 的正常 loader、
Installer 与 Harness Acceptance 路径只接受 v2 Identity。

## 3. Candidate-only Installer

唯一入口为：

```text
python -m installer candidate \
  --verified-candidate-digest sha256:<64 hex> \
  --profile ONLINE_FRESH|ONLINE_EXISTING_DOCKER|OFFLINE_VALIDATE_ONLY \
  --public-origin https://<exact-origin>
```

默认只输出平台计划；真实执行还需要 `--execute --accept`。该入口使用
`VerifiedPrepublicationCandidateCapability` 和 `CandidateBootstrapPrivilegeGate`，与
`ProductionBootstrapPrivilegeGate` 分离，不能发现 GitHub Release、访问 R2/GHCR
fallback、重新构建、重新生成 Manifest/Deployment Contract/Installer Materials，或
把 Candidate 变成 Release Authority。

固定顺序为：验证 Candidate -> Platform Bootstrap plan/execute -> strict post-provision
qualification -> `Installer.plan` -> `Installer.execute`。平台 qualification 前的 AniMemo
instance mutation 必须为零。离线 Profile 的 network、APT 和 external pull 计数必须全为
零，镜像只从 verifier 已闭合的本地 OCI bytes 导入。

## 4. VM Harness 与原始 VM 保护

唯一入口为：

```text
python scripts/candidate_vm_harness.py
```

Harness 默认 `PLAN_ONLY`。只允许固定 `FRESH_BASE`、`DOCKER_BASE`、
`RUNTIME_BASE_OFFLINE` 及其固定 VMware snapshot 名；不接受 VM path、snapshot path、
shell、package list 或安全策略覆盖。`--execute` 还必须给出完全相同的 aggregate plan
digest，并在任何 clone 前通过固定 Account、Bucket、RC.14 Prefix 和六个 expected keys 的
S3 只读 empty 证明。

其中 R2 Origin 的 canonical acceptance 入口只允许显式的 S3 Object Read only 模式：

```text
python -m release.cli verify-rc14-r2-origin-empty \
  --auth-method s3-object-read-only \
  --expected-source-sha <exact-sha> \
  --expected-source-tree <exact-tree> \
  --output <new-receipt-path>
```

它使用 `ListObjectsV2` 枚举固定 Prefix，并对六个 expected keys 执行 `HeadObject`；客户端
接口只暴露 `ListObjectsV2`、`HeadObject`、`GetObject` 三个读取方法。Account、Bucket、
jurisdiction、endpoint host、Prefix 和 source SHA/tree 全部进入 closed Receipt。REST Bearer
Token、公共 CDN、GitHub Release 和其他 transport 均不是 fallback，也不能产生 Candidate
Acceptance 所需的 R2 Receipt。

公共 CDN 404 不是 R2 Origin 权威。缺少专用 S3 只读凭据、Receipt schema/auth method 不符、
Receipt 被篡改或 source SHA/tree 不匹配时，必须在读取 Candidate、公共回查和 clone 前失败。
专用凭据只从当前进程的 `ANIMEMO_R2_S3_ACCESS_KEY_ID`、
`ANIMEMO_R2_S3_SECRET_ACCESS_KEY` 与可选的 `ANIMEMO_R2_S3_SESSION_TOKEN` 读取；不使用
AWS profile、metadata 或通用 `AWS_*` ambient chain。Harness 实现不暴露 R2 写、删、复制、
multipart、Bucket、DNS、Cache、Worker 或 Route 方法，也不记录 Access Key、Secret、Session
Token、Authorization header、签名或 signed URL。操作员边界见
[`R2 S3 只读凭据处理合同`](r2-s3-readonly-credential-handling.md)。

每个 Profile Receipt 必须回绑 Candidate/Run/SHA/tree/version、base/snapshot/clone、平台
与 Installer plan/receipt、四个 OCI digest、Doctor、canonical tests、网络计数和原始 VM
前后 hashes。Aggregate Receipt 绑定已验证 R2 prestate Receipt 的摘要，并要求三份不同
Profile digest、全部 PASS、RC.14 前后仍为空、
repository/publication/shared-host mutation 为零，并固定
`release_authority_granted=false`、`publish_authorized=false`。

一次性 VM 必须在原始 VM 停机时进行全字节复制；每次启动前解析并闭合 VMX/VMDK 的
disk、extent 与 parent backing 引用，拒绝 linked clone、clone 根外路径、raw/physical disk、
multi-writer 和 shared bus。宿主命令使用固定 argv 与最小环境，guest sudo secret 只经 stdin
传递。成功路径只允许软关机后删除；失败路径必须先软关机，软关机失败时仅允许 soft
suspend、继而 hard suspend 作为紧急 containment（禁止 hard power-off）；只有确认副本
不再运行后才隔离，仍然返回失败且不生成 Acceptance PASS。

### 4.1 Windows Provider 与 OpenSSH readiness

Windows Provider 把 Generic Provider 与 OpenSSH subprocess 环境分开。Generic scope 继续
使用既有最小白名单，且不获得 `PROGRAMDATA`；只有固定绝对路径的 `ssh.exe`、`scp.exe`
获得 OpenSSH scope。OpenSSH scope 的 `PROGRAMDATA` 只能来自 Windows Known Folder API
的 `FOLDERID_ProgramData`，不得来自 CLI、配置或 ambient override。结果必须是存在的本地
固定盘绝对目录，路径链不得含 reparse point；空值、NUL、相对路径、UNC、device path、
不存在目录和 ambient 不一致都失败关闭。Windows 环境名按大小写不敏感语义归一化，冲突
值以 `WINDOWS_OPENSSH_ENVIRONMENT_CONFLICT` 拒绝。OpenSSH scope 不继承 `HOME`、
`USERPROFILE`、`SSH_AUTH_SOCK`、`AWS_*`、R2 凭据、代理变量或完整进程环境。

所有 SSH 与 SCP argv 都使用同一套闭合权威：`-F none`、`BatchMode=yes`、
`IdentitiesOnly=yes`、`IdentityAgent=none`、`ProxyCommand=none`、`ProxyJump=none`、
`PermitLocalCommand=no`、`ClearAllForwardings=yes`、`ForwardAgent=no`、
`PasswordAuthentication=no`、`KbdInteractiveAuthentication=no`、
`PreferredAuthentications=publickey`、`RequestTTY=no`。Host Key 校验固定为
`StrictHostKeyChecking=yes`，只读取 Provider session 的固定 `known_hosts`，并禁用 global
known-hosts authority（`GlobalKnownHostsFile=none`）。连接目标固定为 `192.168.64.10`、用户固定为 `animemo`、Host Key
别名固定为 `animemo-test`；不得从用户或系统 ssh_config 恢复别名、用户、身份、代理、
跳板、LocalCommand 或转发语义。

Provider session 固定在 VM work root 的 `provider-session` 子目录，身份文件与 known_hosts
均须为该目录内的普通文件、路径链无 reparse point、由当前 Harness 用户所有，且不得向
Everyone、Authenticated Users 或 Builtin Users 提供有效 NTFS 权限。身份文件必须显式
绑定，默认 `~/.ssh/id_rsa`、`~/.ssh/id_ed25519` 和 ssh-agent 都不是 Authority。Harness
不得读取或记录身份文件正文、用户配置正文、agent endpoint、完整环境或受控文件路径。

Canonical 顺序为：Candidate Authority -> Windows Provider readiness -> VM base identity ->
Harness plan -> Clone create -> VM boot -> SSH。readiness 在本地验证固定 ssh/scp 绝对路径、
预期 SHA256、AMD64 PE 架构、Provider session 文件、OpenSSH scope、闭合 argv 合同，并仅
执行不建立网络连接的 `ssh.exe -V`。成功后签发缓存的、无秘密、无发布权威 Provider
Readiness Receipt；其摘要进入三个 Profile 的 clone identity 输入，因此三套 Profile 绑定
同一 receipt。任一步失败时 Clone create、VM boot 和真实 SSH/SCP 计数都必须为零；稳定
错误类别包括 `WINDOWS_OPENSSH_PROGRAMDATA_UNAVAILABLE`、
`WINDOWS_OPENSSH_PROGRAMDATA_INVALID`、`WINDOWS_OPENSSH_ENVIRONMENT_CONFLICT`、
`WINDOWS_OPENSSH_BINARY_UNAVAILABLE`、`WINDOWS_OPENSSH_IDENTITY_MISMATCH`、
`WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE` 与 `WINDOWS_OPENSSH_READINESS_FAILED`。

该 readiness 只修复 Host Provider 安全与顺序合同，不执行真实 VM，也不改变 Candidate
Identity v2、Qualification、Installer、R2 或发布 Schema。修复合并改变 exact main 后，旧
Qualification、Identity、Execution Receipt、live R2 prestate 与隔离 Clone 全部仅可取证；
下一轮必须从新 exact-main Qualification 开始，使用未暴露的最小只读 R2 凭据并创建三套
全新 Profile Clone。

## 5. Freshness 与 Publish

Freshness workflow 必须接收非可选 `candidate_acceptance_receipt_b64url`：它是 bounded、
canonical UTF-8 Aggregate Receipt 的 unpadded base64url。Freshness Artifact 为十文件闭合
集合，包含原始 `candidate-acceptance-receipt.json`，并绑定其 SHA256、Qualification Run、
intended main SHA/tree 与 candidate version，同时保留双快照、至少 60 秒间隔和 15 分钟
TTL。

Publish 的两个有效 mutation 前门禁都必须证明：传入 Aggregate Receipt 实际摘要、
Freshness 中绑定的摘要、Publish 期望摘要三者相同。缺失、空值、错误 Schema、任一 Profile
FAIL、Run/main/tree/version 不同、Candidate Smoke 或 postpublication receipt 均失败关闭。

合并本合同实现会改变 main，因此旧 Qualification 只能用于历史分析。必须在新的 exact
main 重新 Qualification，生成新的 Identity v2 和 Execution Receipt，并重新完成实时 R2
Origin prestate 后再进行 VM Acceptance；不得复用旧 Run、旧 v1 Candidate 或旧 R2 Receipt。
