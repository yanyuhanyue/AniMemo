# AniMemo Installer Security Successor Contract v2

Status: FROZEN FOR v1.1 P1 SECURITY REPAIR

Predecessor: `docs/installer-contract-v1.md` at Git blob
`a6307b42928423cea3a2cf04db7836887fc818a0`.

This is a narrow security successor. It inherits Installer Contract v1 in full
and replaces only the bootstrap privilege and trust-lifecycle boundaries below.
It does not modify the v1 Release Authority, compatibility, deployment, backup,
rollback, or updater ownership contracts.

## 1. Authority and input classification

GitHub Immutable Release remains the single Release Authority. The install
portal, Official Mirror, Portable archive, checksums, bundle-contained verifier,
and bundle-contained roots are `UNTRUSTED_TRANSPORT_INPUT`. TUF authorizes only
GitHub/Sigstore trust-metadata succession and signer deauthorization; it cannot
mint an AniMemo Release.

Online Stage-0 uses independently installed `/usr/bin/gh` exactly `2.97.0` to
verify the exact tag and the exact protected `installer-materials.tar`. Offline
Stage-0 requires an operator or trusted image to provision the verifier and roots
independently of the Portable payload. Portable-only first trust is forbidden.

The online carrier may explicitly acquire only
`installer-materials.tar` from the fixed Official Mirror origin
`https://download.animemo.cc/yanyuhanyue/AniMemo/releases/download/<EXACT_TAG>/`.
It must verify the GitHub Immutable Release before that download, reject every
redirect and query, verify the mirror bytes with `gh release verify-asset`, copy
them into a root-owned candidate, and reverify both that candidate and its fixed
final path before extraction. Any verification failure removes paths created by
that invocation and leaves zero persistent AniMemo mutation. The receipt is a
transport completeness marker only and cannot satisfy either GitHub gate. This
flow is selected only by `--source official-mirror`; no automatic or error-driven
cross-source fallback exists. The exact command contract is frozen in
`docs/distribution-transports-v1.1.md`.

## 2. Verified bootstrap state machine

`UNTRUSTED_INPUT -> ACQUIRED -> AUTHORITY_VERIFIED -> PROTECTED_COPY_VERIFIED ->
PRIVILEGE_ALLOWED -> TRUST_PROVISIONING -> TRUST_PROVISIONED ->
INSTALLATION_AUTHORIZED -> INSTALLED`.

`PRIVILEGE_ALLOWED` requires a single-use `BOOTSTRAP_PRIVILEGE_GATE` capability
bound to repository, exact tag, protected archive identity, fixed authority root,
and the exact loaded core module bytes. The root launch uses a fixed protected
working directory, closed environment, `PYTHONSAFEPATH=1`, and Python `-P`; cwd
module shadowing is forbidden. No production skip, insecure, debug, environment,
or arbitrary-path bypass exists.

## 3. Trusted filesystem and TOCTOU boundary

Bootstrap authority root is `/var/lib/animemo/bootstrap-authority/v1`; trust
state root is `/var/lib/animemo/offline-trust/v2`. Both are fixed, root-owned,
not group/world writable, and reject symlinks, reparse substitutes, multi-link
files, path traversal, and identity drift. The copied archive is reverified after
the ownership transition and before extraction. Loaded Python source is rebound
byte-for-byte to the same protected archive before privilege is consumed.

## 3A. Verified host-platform bootstrap

缺少 Docker CLI/daemon、Compose v2、`pg_dump` 或 `psql` 的 Ubuntu 24.04
amd64 Fresh Base 是可准备主机状态，不是最终 runtime incompatibility。正式入口在
GitHub Immutable Release 与受保护 `installer-materials.tar` 均已验证、且加载的
`installer.platform_bootstrap` 已逐字节回绑到该 tar 后，按以下固定顺序运行：

```text
verified Release/materials
-> Platform Bootstrap read-only plan
-> accept exact plan digest
-> Platform Bootstrap execute
-> strict ProductionPlatformPort qualification
-> canonical Installer.plan
-> canonical Installer.execute
```

`Installer.plan` 仍完全只读，`Installer.execute` 仍是 AniMemo 实例 mutation 的唯一
owner；包安装不得进入 `Installer.plan`、`ProductionPlatformPort.assess`、
`collect_host_capabilities`、compatibility evaluator 或 instance root 准备。平台准备
失败或 post-provision qualification 失败时，instance root、Compose project、
AniMemo container/network/volume、instance locator、updater unit 与用户数据 mutation
均为零。

唯一生产 package policy 位于 `installer/platform_bootstrap.py`，身份为
`animemo.platform-package-policy/v1`，固定 Ubuntu `24.04`、`amd64`、
`docker.io`、`docker-compose-v2`、`postgresql-client-16` 与 PostgreSQL major 16。
环境变量、CLI、plan、Release、Mirror 和测试均不能提供 mode、package、repository、
command、service 或 lock path。在线执行只接受由固定 `sources.list` / `sources.list.d`
路径和 root-owned Ubuntu archive keyring 约束、且每项显式 `Signed-By` 的 Ubuntu
archive/security APT metadata 候选包；`trusted=yes`、未知安全字段及非 Ubuntu 候选
均失败关闭。执行使用闭合 argv、`DEBIAN_FRONTEND=noninteractive`、有界 APT lock、重试
与完整进程组 timeout 回收；不执行 upgrade、autoremove、第三方脚本、Snap、pip Compose、
daemon config、DNS/firewall/proxy 修改或 Docker prune。

Platform facts、镜像 pull/import/readback、Target/Doctor/Fresh Compose 调用以及数据库流式
备份统一使用 `/usr/bin/docker --host unix:///var/run/docker.sock`。Docker 子进程移除全部
`DOCKER_*` 覆盖，固定 `HOME=/nonexistent` 与系统 `PATH`；平台事实只接受并冻结发行版
Compose plugin 路径，发现 `/usr/local` shadow plugin 即失败关闭。因此用户 Docker
context、`DOCKER_CONFIG`、HOME plugin 和远端 daemon 均不能替换实际执行边界。

主机事实只派生三种模式：

- `ONLINE_FRESH`：Docker CLI/daemon/Compose 均不存在；安装三个固定包，并只对首次
  安装的 Docker 执行 `systemctl enable --now docker`；
- `ONLINE_EXISTING_DOCKER`：本机固定 socket 的 Docker daemon 已健康；冻结并前后比较
  Docker CLI、daemon、socket、Compose plugin 与 daemon config 身份；只补 Compose/PG
  client，不重装 Docker、不 restart daemon；模拟 APT 事务若触及 Docker runtime 即拒绝；
- `OFFLINE_VALIDATE_ONLY`：仅由 `local-bundle` 派生；禁止 APT 与所有网络，要求全部
  能力已存在，否则以 `PLATFORM_BOOTSTRAP_OFFLINE_CAPABILITY_MISSING` 失败。

计划与 Receipt 分别使用 `animemo.platform-bootstrap-plan/v1` 和
`animemo.platform-bootstrap-receipt/v1`。两者拒绝重复 key、未知字段、非规范 JSON、
摘要或主机事实替换；plan digest 不含 wall clock。执行使用固定
`/run/lock/animemo-platform-bootstrap.lock`，要求 root-owned regular single-link
non-symlink 文件并以 nonblocking process lock 拒绝并发。Receipt 只记录闭合包、
初末能力、daemon 状态和零 restart 计数；它既不是 Release Authority，也不是
Platform Qualification，不能授权实例 mutation、VM Acceptance、Publish 或 Stable。

现有 `durability.platform.REQUIRED_CAPABILITIES` 完整保留。Bootstrap 后和
`Installer.execute` 前仍由观测事实重新运行严格 qualification；只有此时仍不兼容
才属于最终 platform runtime unsupported。本变更没有持久 schema migration；未发布
的 plan/Receipt v1 是明确 clean boundary，不提供错误旧 schema 的静默兼容。

直接执行受保护材料中的 `python -m installer` 与正式 Stage0 不是两条生产捷径：
在线入口必须先由 Stage0 建立 exact Release/材料 capability，离线入口必须消费既有的
同 Release 受保护材料 capability；production composition 在两种来源下都会先回绑
Platform Bootstrap 字节。测试可注入 `runtime` 仅是无特权 seam，不能绕过 production
composition，也不能授权真实实例 mutation。

Candidate Fresh Base Smoke 只把一个未发布 commit/tree、候选 materials 摘要和 policy
identity 绑定到一次性 clone 上，用来提前发现确定性安装器缺陷；它没有 GitHub Release、
签名发布材料、Qualification 或 Publish authority，因此不授予 Release Authority、VM
Acceptance 或 Stable。RC.11、RC.12、RC.13 已分别作为不可变 Release 留下真实拒绝历史，
不得删除、编辑或重新标记；本修复合并后由 canonical resolver 消费已占用的 11–13
序号并进入新的 RC.14，后者必须在后续独立任务重新经历 Qualification、Freshness、
Publish、Mirror 与正式 VM Acceptance。

## 4. Initial trust provisioning

The closed seven-file pretrust kit is an input only after the bootstrap
capability is valid. Its manifest binds the Linux verifier, GitHub/Sigstore
trusted roots, both TUF roots, and the closed TrustProfile. Provisioning stages a
new immutable generation, fsyncs files and directories, validates readback, and
atomically replaces the active record. Same-byte repeat is idempotent; different
bytes for an existing identity fail closed. Test fixtures carry explicit
`TEST-ONLY` identities and have no production authorization path.

## 5. Trust update, rollback, revocation, and crash policy

Only the current active TrustProfile may authorize exact successor `N+1`.
GitHub and Sigstore TUF root chains are verified sequentially with old-and-new
root threshold semantics; timestamp, snapshot, and targets versions must all
advance. Version skip beyond the fixed bound, stale parent, replay, downgrade,
wrong project, invalid threshold, expired metadata, or target mismatch fails
closed. A rotated or replaced material is `SUPERSEDED`, not automatically
`REVOKED`. Revoked signer identities must come from an explicit deauthorization
in a cryptographically verified TUF root successor.

Update commit order is: verify untrusted package; construct successor in a new
generation; fsync; readback; atomically replace the active record; fsync parent.
A crash before active replacement leaves the predecessor authoritative; a crash
after replacement must read back the complete successor. Normal rollback of
trust metadata is forbidden. Disaster recovery requires separate governance and
is not provided by this contract.

Offline status is limited to `AUTHENTIC_AS_OF_SIGNED_EVIDENCE`; future revocation
knowledge is `UNKNOWN_OFFLINE`. This contract never claims
`CURRENTLY_NOT_REVOKED` without sufficiently current verified metadata.

## 6. Closed failure taxonomy

Bootstrap failures use `BOOTSTRAP_*` and stop before AniMemo mutation. Trust
failures use `TRUST_*`, including unavailable/invalid authority, archive or
module drift, invalid state root, invalid bootstrap authorization, invalid
profile lineage, rollback/replay, cryptographic verification failure,
revocation inconsistency, staging failure, and readback failure. Failure codes
are stable and contain no credential, environment, raw attestation, or secret
payload values.

平台准备失败使用稳定 `PLATFORM_BOOTSTRAP_*` 分类，覆盖 unsupported OS/arch、root、
package manager/policy、APT lock/update/candidate、三个 package component、Docker
daemon、host inconsistency、offline capability、plan acceptance/change、Receipt、
post-qualification 与 concurrent lock。错误输出只包含 code，不包含 argv、环境、APT
credential、proxy credential 或命令原始输出。

## 7. Production and qualification separation

Production requires official GitHub/Sigstore TUF roots, a Linux/amd64 verifier
built from the frozen module graph, and GitHub Release authority. Synthetic keys,
official-format fixtures, and namespace-isolated filesystem roots are allowed
only in qualification and cannot satisfy the production privilege gate. Live
Immutable Release acceptance remains deferred until the first separately
authorized RC.
