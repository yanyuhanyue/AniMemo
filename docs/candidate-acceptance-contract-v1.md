# AniMemo 发布前 Candidate Acceptance 合同 v1

状态：Candidate RC 发布前强制合同；当前完整插件验收使用 Profile v2 / Aggregate v4。

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

当前身份与收据协议为：

- `animemo.prepublication-candidate-input/v1`
- `animemo.verified-prepublication-candidate/v2`
- `animemo.prepublication-candidate-verification-execution-receipt/v1`
- `animemo.prepublication-candidate-profile-receipt/v2`
- `animemo.prepublication-candidate-acceptance-receipt/v4`
- `animemo.cloudflare-plugin-origin-receipt/v2`

历史 Profile v1、Aggregate v3 和 S3 Origin v3 的协议身份与原始证据保持不变；
Guest 仍只生成 v1 Draft，由 Host 添加独立观察和本轮 plan/session 绑定。

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
Updater service 必须通过独立 systemd drop-in 先以空 `RestrictAddressFamilies=` 重置
基础服务的列表，再固定为 `RestrictAddressFamilies=AF_UNIX AF_NETLINK`。读取实际属性时
要求准确的两个地址族且无重复，不依赖 systemd 输出顺序；Receipt 使用固定规范顺序。
Profile Receipt 只能在 Docker network 与
systemd property 的真实 readback 均精确匹配后记录 OS egress isolation receipt；这不会修改
公共 DNS、Cloudflare、主机防火墙或共享生产服务。

内部 bridge 没有容器默认路由。Candidate Installer 在 datastore 建网后，从已核验
实例归属的容器查得唯一 network ID，再确认该 bridge 的 Compose labels、internal 属性、
唯一 IPv4 IPAM subnet 和成员 endpoint，取得准确 gateway。它把单个规范 IPv4 原子写入
`/run/animemo-candidate/<instance>/edge-proxy-ipv4`：父目录由 root 控制，文件
`root:root / 0444`，独立于 API 可写的数据目录。Web 只读绑定此文件且禁止自动创建缺失源；
启动及 API 重建后重新核验网络 ID。Nginx 仅信任 gateway `/32`，Django 的
`TRUSTED_PROXY_IPS` 仍由实际 Web 容器地址 `/32` 独立绑定。文件存在但无效即退出；
未挂载该文件的普通部署继续使用默认路由解析。Candidate 保持 `internal: true`。

Docker 不为仅连接内部网络的容器实现宿主端口映射。Candidate Installer 在验证准确
Web/network/gateway 绑定后，于配置的 loopback listen endpoint 持有临时 TCP ingress，
目的地址固定为该 Web IPv4 的 80 端口。它不改 Host、forwarded scheme 或响应字节，
不连接外部网络，不改变 Docker/宿主防火墙，也不让调用者选择目的地址。端口被占用
即失败；禁止替换 listen endpoint 或用直接容器探测冒充 Doctor 的 loopback 检查。
连接、缓冲和存活时间有界，不记录流量；在运行校验及 Doctor 完成后重验准确绑定。
Candidate CLI 的成功、失败、取消和 plan-only 出口均关闭进程内 ingress，确认线程及
listener 释放后结束。不安装长期代理服务，不将此开发/验收设施用于普通部署。

## 4. VM Harness 与原始 VM 保护

唯一入口为：

```text
python scripts/candidate_vm_harness.py
```

Harness 默认 `PLAN_ONLY`。只允许固定 `FRESH_BASE`、`DOCKER_BASE`、
`RUNTIME_BASE_OFFLINE` 及其固定 VMware snapshot 名；不接受 VM path、snapshot path、
shell、package list 或安全策略覆盖。`--execute` 必须接受本进程的完整 plan digest；
已明确批准当前封闭单次捕获范围时，使用对应的 `--authorization-id` 和 exclusive
`--result`，接受本次生成并保存的完整计划；范围与独立固定账本见
[Guest sudo 会话控制器](guest-sudo-session.md)。
在任何 Clone 前，必须通过固定 Account、Bucket、Candidate Prefix 和 expected keys 的
Origin empty PRESTATE 证明；前缀和键只从本次 Candidate version 派生。

完整入口默认 `--r2-origin-transport cloudflare-plugin`，同一个受持有的
`CloudflarePluginOrigin` 在 Clone 前与三个 Profile 结束后分别请求 PRESTATE、POSTSTATE。
插件实际 GET 固定账户的 `animemo-release-mirror` Bucket、完整 Prefix 分页和准确 expected
keys。请求绑定 source/tree/Q/Candidate/plan/session/role、新 UUIDv4 nonce、collector 摘要
和五分钟有效期；本地私有通道只运输和校验公开观察，不取得 API Token 或 Guest secret。

插件 Receipt v2 保留完整 request/response、自摘要与派生计数，保留真实 `10007`、未知
HTTP status 为 null 和缺失分页字段的语义；不把 GET 数固定为某个成功常数，不转换成
S3 Receipt。POSTSTATE 必须使用新的 request/nonce/response，并在 PRESTATE 完成后发起。
独立消费者在原观察完成时刻重验请求有效期、scope、分页和对象结果；三 Profile 的长运行
不会刷新或破坏已完成观察的证据。摘要是完整性绑定，不是 Cloudflare 签名；来源权威是受信
插件观察通道和 canonical Host 生产器。

公共 CDN 404 不构成 Origin 权威。缺连接、scope/role/nonce 不符、过期响应或未知 API
错误均失败关闭，无其他 transport 自动 fallback。显式 S3 模式保留既有独立协议及
[`S3 只读凭据处理合同`](r2-s3-readonly-credential-handling.md)，不用于本次插件验收。
所有 Origin 路径仅有只读操作，不记录 Token、Authorization header 或签名。

每个 Profile Receipt 必须回绑 Candidate/Run/SHA/tree/version、base/snapshot/clone、平台
与 Installer plan/receipt、四个 OCI digest、实际 Doctor、三项 canonical tests、completed
steps、实际命令边界/network policy、external-pull inventory 和原始 VM 前后 hashes。Guest
只能输出不含任何宿主 VM hash 字段的 Profile Receipt Draft；Host Harness 拒绝 Guest 注入的
前后 hash 字段，并用计划冻结的 pre hashes 与独立重读的 post hashes 生成和校验最终 Receipt。
插件路径的 Profile v2 另由 Host 绑定 plan digest 与 session ID。
Aggregate Receipt
分别绑定已验证的 `candidate_prestate` 与 `candidate_poststate` Observation Receipt 摘要，并
要求二者 `observation_id` 不同、执行前后 Origin 均为空、repository/publication/shared-host
mutation 为零。v4 内嵌两份可独立验证的 Origin Receipt 与实际生成的完整 Profile Receipts，
重验 scope、时间顺序、Profile 摘要和 source/Q/Candidate/plan/session 绑定。
`profile_results` 对三个固定 Profile 分别表达 `PASS`、`FAIL`、`ERROR`
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
Authority。完整 Candidate 使用一次原生 Console 捕获，内存 owner 绑定整场冻结 plan；
三个 Profile 各自取得三个固定角色的单次 grant，只向产生最终身份观察的同一 SSH 进程交付。
root 首先运行 Host 内嵌的固定程序，安全复制并验证库存后才执行材料中的 Runner；
一次特权进程包含材料完结、执行与回执输出，交付临时副本在发送后立即清理；owner 在
最后一次交付、撤销或结束时清理。阶段诊断与 Draft 独立 framing，诊断不授予 Receipt authority。
额度、交付与清理边界见 [Guest sudo 会话控制器](guest-sudo-session.md)。
成功路径只允许软关机后删除；失败路径必须先软关机，软关机失败时仅允许 soft
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

Provider 的 plan/lease 绑定 Candidate version、Candidate digest 与随机 session ID；Windows
磁盘路径使用受持有私有 work root 下的紧凑 session/profile/vm 布局，每个 Profile 使用
独立 clone root、session key 与 known_hosts。身份文件与 known_hosts
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
`originalVmHashes`，文件数由实际闭合图及 VMX/VMSD/VMSN 清单取得。完整图摘要
进入 Harness plan，每个 Snapshot 的祖先图摘要同时进入对应 Profile plan。全量复制后必须对
所有 source-bound 文件逐字节重验；revert 后 active VMDK 节点只能来自这份 source-bound
inventory，并且 selected Snapshot descriptor 的闭合祖先链必须与 Profile plan 摘要完全一致。
VMware 在 revert 中产生的动态 VMX/redo 不是计划 Authority：VMX 仍由独立 runtime identity
与 challenge 绑定，任何新增未计划 VMDK 节点、同尺寸 extent 篡改或 Source extent 漂移均
fail closed。

该合同修复不在 Repair 阶段执行真实 VM，也不改变 Candidate Identity v2、Qualification 或
R2 Authority。修复合并改变 exact main 后，旧
Qualification、Identity、Execution Receipt、live R2 prestate 与隔离 Clone 全部仅可取证；
本轮从新 exact-main Qualification 开始，使用已连接插件的真实只读 Origin 观察并创建三套
全新 Profile Clone。

## 5. Freshness 与 Publish

Freshness workflow 必须接收非可选 `candidate_acceptance_receipt_b64url`。完整 v4 收据由
`encode_aggregate_receipt_b64url` 生成 `animemo.candidate-acceptance-wire/v1`：canonical JSON
envelope 包含 zlib payload、原始回执字节数与 SHA-256，外层使用 unpadded base64url，
最多 48 KiB，为 [GitHub 全部 workflow_dispatch inputs 的 65,535 字符上限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatchinputs)
留出其他绑定字段的空间。解码最多输出 384 KiB，拒绝截断、拼接或尾随流、超限解压、
错误长度/摘要、额外字段与非 canonical JSON；历史直接 base64url 格式保留原有验证。
Harness 的 `candidateAcceptanceReceiptB64url` 即为下一阶段输入，生成它不派发工作流。
解码后的原始完整 Aggregate 字节保持不变；Freshness Artifact 为十文件闭合集合，
包含原始 `candidate-acceptance-receipt.json`，并绑定其 SHA256、Qualification Run、
intended main SHA/tree 与 candidate version，同时保留双快照、至少 60 秒间隔和 15 分钟
TTL。

Publish 的两个有效 mutation 前门禁都必须证明：传入 Aggregate Receipt 实际摘要、
Freshness 中绑定的摘要、Publish 期望摘要三者相同。缺失、空值、错误 Schema、任一 Profile
FAIL、Run/main/tree/version 不同、Candidate Smoke 或 postpublication receipt 均失败关闭。

合并本合同实现会改变 main，因此旧 Qualification 只能用于历史分析。必须在新的 exact
main 重新 Qualification，生成新的 Identity v2 和 Execution Receipt，并重新完成实时 R2
Origin prestate 后再进行 VM Acceptance；不得复用旧 Run、旧 v1 Candidate 或旧 R2 Receipt。
