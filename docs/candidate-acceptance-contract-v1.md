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
-> verifier-owned digest root
-> Candidate-only Installer
-> FRESH_BASE / DOCKER_BASE / RUNTIME_BASE_OFFLINE
-> 三份 Profile Receipt
-> 一份 Aggregate Receipt
-> Metadata Freshness 摘要绑定
-> Publish 再次验证同一摘要
```

`.dockerbuild` 是 BuildKit history/debug 产物，不是运行时镜像字节，也不能授予 VM
Acceptance。每个可接受 OCI layout 必须包含 index、authoritative manifest、config 和
全部 layer blobs，并逐摘要闭合；mutable tag、`latest`、远端 registry pointer 和重建都
不被接受。

## 2. Candidate Input 与 canonical verifier

四个 closed JSON Schema 为：

- `animemo.prepublication-candidate-input/v1`
- `animemo.verified-prepublication-candidate/v1`
- `animemo.prepublication-candidate-profile-receipt/v1`
- `animemo.prepublication-candidate-acceptance-receipt/v1`

唯一验证入口为：

```text
python -m release.cli verify-prepublication-candidate
```

Verifier 必须同时绑定 repository、workflow name/path/ref、Run ID、attempt 1、head SHA、
head tree、required jobs、精确 Artifact ID/API digest、Candidate Input、Release Notes、
Manifest、Deployment Contract、Installer Materials、checksums 与四套 OCI DAG。产物只能
落到 `/var/lib/animemo/prepublication-candidates/v1/<candidate-input-digest-hex>/`；
`--verified-candidate-digest` 只能在这些 verifier-owned roots 中唯一定位同摘要 Evidence，CLI 不接受任意
本地目录。

ZIP 解包拒绝绝对路径、父目录逃逸、Windows drive path、重复路径、大小写碰撞、链接、
特殊文件、未知成员和尺寸超限。写入采用 exclusive/atomic 语义；同一摘要不同字节失败。

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
digest，并在任何 clone 前通过 Cloudflare R2 Objects REST API 对固定 account hash、
bucket、RC.14 prefix 和六个 expected keys 的只读 empty 证明。

公共 CDN 404 不是 R2 Origin 权威。缺少短期只读凭据时必须在 clone 前失败。Harness
实现不暴露 R2 写、删、复制、Bucket、DNS、Cache、Worker 或 Route 方法，也不记录 token
或 Authorization header。

每个 Profile Receipt 必须回绑 Candidate/Run/SHA/tree/version、base/snapshot/clone、平台
与 Installer plan/receipt、四个 OCI digest、Doctor、canonical tests、网络计数和原始 VM
前后 hashes。Aggregate Receipt 要求三份不同 Profile digest、全部 PASS、RC.14 前后仍为空、
repository/publication/shared-host mutation 为零，并固定
`release_authority_granted=false`、`publish_authorized=false`。

一次性 VM 必须在原始 VM 停机时进行全字节复制；每次启动前解析并闭合 VMX/VMDK 的
disk、extent 与 parent backing 引用，拒绝 linked clone、clone 根外路径、raw/physical disk、
multi-writer 和 shared bus。宿主命令使用固定 argv 与最小环境，guest sudo secret 只经 stdin
传递。成功路径只允许软关机后删除；失败路径必须先软关机，软关机失败时仅允许 soft
suspend、继而 hard suspend 作为紧急 containment（禁止 hard power-off）；只有确认副本
不再运行后才隔离，仍然返回失败且不生成 Acceptance PASS。

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
main 重新 Qualification，再进行 VM Acceptance；不得复用旧 Run。
