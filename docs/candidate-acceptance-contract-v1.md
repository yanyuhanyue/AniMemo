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
- `animemo.prepublication-candidate-acceptance-receipt/v3`
- `animemo.r2-origin-observation-receipt/v3`

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
instance mutation 必须为零。Production composition 必须输出独立绑定的 Doctor、canonical
CRUD/health、completed steps、平台与运行时命令边界、network policy、external-pull inventory
以及 OCI acquisition/runtime readback receipts；Profile Runner 不得从 Installer outcome 或计划
action 数推导这些事实。离线 Profile 不得出现任何网络命令，在线 Profile 只接受计划绑定的
Ubuntu APT argv。`expected_network_command_digests` 必须保留计划顺序且无重复；只有
`retryable_network_command_digests` 显式列出的 install 才允许恰好一次 `124 -> 0`，更新、
模拟、重复成功、重复超时、乱序、缺失或额外命令全部失败关闭。所有 Candidate 可达的
Docker run/Compose run/up 都必须显式 `--pull never`，
镜像只从 verifier 已闭合的本地 OCI bytes 导入。Candidate composition 还必须加载固定字节的
Compose override，把 Profile 实例的 `animemo` network 设置为 `internal: true`；Candidate
Updater service 必须通过独立 systemd drop-in 固定为
`RestrictAddressFamilies=AF_UNIX AF_NETLINK`。Profile Receipt 只能在 Docker network 与
systemd property 的真实 readback 均精确匹配后记录 OS egress isolation receipt；这不会修改
公共 DNS、Cloudflare、主机防火墙或共享生产服务。

## 4. VM Harness 与原始 VM 保护

唯一入口为：

```text
python scripts/candidate_vm_harness.py
```

Harness 默认 `PLAN_ONLY`。只允许固定 `FRESH_BASE`、`DOCKER_BASE`、
`RUNTIME_BASE_OFFLINE` 及其固定 VMware snapshot 名；不接受 VM path、snapshot path、
shell、package list 或安全策略覆盖。`--execute` 还必须给出完全相同的 aggregate plan
digest，并在任何 clone 前通过关闭 Account、Bucket、Candidate Prefix 和 expected keys 的
S3 只读 empty PRESTATE 证明。Account、Bucket、Prefix 与 expected keys 必须由本次
Candidate version 的关闭计划派生，不能由旧 RC 编号恢复。

其中 R2 Origin 的 canonical acceptance 入口只允许显式的 S3 Object Read only 模式：

Harness 对同一关闭计划调用 `verify_candidate_r2_origin_from_environment`：clone 前使用
`observation_role=PRESTATE`，三个 Profile 均结束后必须再次发起新的 List/Head 调用并使用
`observation_role=POSTSTATE`。两个 Observation 使用不同的 UUIDv4 `observation_id`，各自绑定
完整 object inventory 及其摘要；POSTSTATE 不得复用 PRESTATE receipt、对象列表或读取结果。

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
与 Installer plan/receipt、四个 OCI digest、实际 Doctor、三项 canonical tests、completed
steps、实际命令边界/network policy、external-pull inventory 和原始 VM 前后 hashes。Guest
只能输出不含任何宿主 VM hash 字段的 Profile Receipt Draft；Host Harness 拒绝 Guest 注入的
前后 hash 字段，并用计划冻结的 pre hashes 与独立重读的 post hashes 生成和校验最终 Receipt。
Aggregate Receipt
分别绑定已验证的 `candidate_prestate` 与 `candidate_poststate` Observation Receipt 摘要，并
要求二者 `observation_id` 不同、执行前后 Origin 均为空、repository/publication/shared-host
mutation 为零。v3 的 `profile_results` 对三个固定 Profile 分别表达 `PASS`、`FAIL`、`ERROR`
或 `NOT_RUN_SHARED_BLOCKER`；只有三项全为 `PASS` 时 overall 才为 `PASS`，Freshness 与
Publication 必须拒绝语法有效的 FAIL Aggregate，并始终固定
`release_authority_granted=false`、`publish_authorized=false`。

一次性 VM 必须在原始 VM 停机时进行全字节复制；每次启动前解析并闭合 VMX/VMDK 的
disk、extent 与 parent backing 引用，拒绝 linked clone、clone 根外路径、raw/physical disk、
multi-writer 和 shared bus。Plan 必须冻结当前 active graph 与三个固定 Snapshot descriptor
祖先链的完整 descriptor/extent union：每个文件逐项 SHA-256，并同时绑定 source graph 与
各 Profile Snapshot graph 聚合摘要。Clone copy 后必须按完整文件集合和字节摘要精确对账；
revert 后 active VMDK 只能来自该冻结集合，所选 Snapshot graph 还必须再次与 Profile plan
精确匹配，同尺寸 extent 漂移同样失败关闭。动态 VMX/redo 只作为运行观察，不得成为计划
Authority。宿主命令使用固定 argv 与最小环境，guest sudo secret 只经 stdin
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
别名由 Candidate version、Candidate digest、随机 session ID、Profile 与 clone identity 派生；不得从用户或系统 ssh_config 恢复别名、用户、身份、代理、
跳板、LocalCommand 或转发语义。

Provider session root 必须由 Candidate version、Candidate digest 和随机 session ID 动态
派生，每个 Profile 使用独立 clone root、session key 与 known_hosts。身份文件与 known_hosts
均须为各自 authority root 内的普通文件、路径链无 reparse point、由当前 Harness 用户所有，且不得向
Everyone、Authenticated Users 或 Builtin Users 提供有效 NTFS 权限。身份文件必须显式
绑定，默认 `~/.ssh/id_rsa`、`~/.ssh/id_ed25519` 和 ssh-agent 都不是 Authority。Harness
不得读取或记录身份文件正文、用户配置正文、agent endpoint、完整环境或受控文件路径。

所有 Win32 能力只允许通过单一、延迟加载的 `ctypes.WinDLL(..., use_last_error=True)`
适配器访问；`advapi32`、`kernel32`、`ole32` 与 `shell32` 所有已使用函数都必须显式声明
`argtypes` 和 `restype`，其中 HANDLE、指针及输出参数保持指针宽度安全。Known Folder
缓冲区、进程 Token 与 Security Descriptor 必须分别成对调用 `CoTaskMemFree`、
`CloseHandle` 与 `LocalFree`。Token 与 Security Descriptor 仅在原生调用明确成功取得后
释放；Known Folder 返回的非空 `PWSTR` 则按其所有权合同无论 HRESULT 成败都必须释放。
`EqualSid` 返回 false 时必须立即读取 last-error，将真正的不相等、SID 无效与查询失败
分别分类；适配器不得在非 Windows 导入路径初始化。

Canonical 顺序为：Candidate Authority -> Windows Provider readiness -> VM base identity ->
Harness plan -> Clone create -> boot 前向 exact clone VMX 注入随机 challenge -> VM boot ->
只读 bootstrap identity 两次核验 -> 独立 session key 与 Guest host key 轮换 -> session identity
再核验 -> Candidate staging。固定 IP 只用于连通性，不能成为 Guest 身份权威；任一错误 VMX、
disk graph、snapshot、UUID、MAC、IP、machine-id、boot-id、challenge 或 host key 都必须在 sudo、
SCP 和 remote rm 前失败。readiness 在本地验证固定 ssh/scp 绝对路径、
预期 SHA256、AMD64 PE 架构、Provider session 文件、OpenSSH scope、闭合 argv 合同，并仅
执行不建立网络连接的 `ssh.exe -V`。成功后签发缓存的、无秘密、无发布权威 Provider
Readiness Receipt；其摘要进入三个 Profile 的 clone identity 输入，因此三套 Profile 绑定
同一 receipt。任一步失败时 Clone create、VM boot 和真实 SSH/SCP 计数都必须为零；稳定
错误类别包括 `WINDOWS_OPENSSH_PROGRAMDATA_UNAVAILABLE`、
`WINDOWS_OPENSSH_PROGRAMDATA_INVALID`、`WINDOWS_OPENSSH_ENVIRONMENT_CONFLICT`、
`WINDOWS_OPENSSH_BINARY_UNAVAILABLE`、`WINDOWS_OPENSSH_IDENTITY_MISMATCH`、
`WINDOWS_OPENSSH_CONFIG_AUTHORITY_UNSAFE`、`WINDOWS_OPENSSH_ACL_QUERY_FAILED`、
`WINDOWS_OPENSSH_ACL_UNSAFE`、`WINDOWS_OPENSSH_OWNER_MISMATCH`、
`WINDOWS_WIN32_ABI_UNSUPPORTED`、`WINDOWS_WIN32_SECURITY_DESCRIPTOR_INVALID` 与
`WINDOWS_OPENSSH_READINESS_FAILED`。API 查询失败、策略判定不安全、所有者不匹配、
ABI 不受支持和 Security Descriptor 无效必须保持彼此可区分，且都在任何 Clone 创建前
fail closed。

Source VM Authority 还必须枚举当前 VMX 与三个受控 Snapshot descriptor 可达的闭合
VMDK parent/extent 图，并把每个 descriptor 与 extent 的实际字节 SHA256 纳入
`originalVmHashes`（当前 56 个图文件、与静态 VMX/VMSD/VMSN 合并后 61 个键）。完整图摘要
进入 Harness plan，每个 Snapshot 的祖先图摘要同时进入对应 Profile plan。全量复制后必须对
所有 source-bound 文件逐字节重验；revert 后 active VMDK 节点只能来自这份 source-bound
inventory，并且 selected Snapshot descriptor 的闭合祖先链必须与 Profile plan 摘要完全一致。
VMware 在 revert 中产生的动态 VMX/redo 不是计划 Authority：VMX 仍由独立 runtime identity
与 challenge 绑定，任何新增未计划 VMDK 节点、同尺寸 extent 篡改或 Source extent 漂移均
fail closed。

该合同修复不在 Repair 阶段执行真实 VM，也不改变 Candidate Identity v2、Qualification 或
R2 Authority。修复合并改变 exact main 后，旧
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
