# AniMemo Update Agent v1 Contract

AniMemo Update Agent 是独立的 Host Service，也是不可变 Core Release 的 Release Consumer。Django 只通过本地 Unix Socket 请求白名单操作；Docker、Compose、Release cache、备份与持久 Operation journal 的权限都留在 Agent 进程内。

## Fixed authority

生产资源固定为：

```text
application: /opt/1panel/docker/compose/anime-journal/app
data:        /data/anime-journal
state:       /var/lib/animemo-updater
socket:      /run/animemo-updater/updater.sock
repository:  yanyuhanyue/AniMemo
API image:   ghcr.io/yanyuhanyue/animemo-api
Web image:   ghcr.io/yanyuhanyue/animemo-web
project:     anime-journal
services:    migration, bootstrap, api, web
```

生产 CLI 不接受路径、repository、URL、image、Compose project、service 或 command 参数。`HostPaths.production()` 也拒绝替换固定路径。测试可使用显式 testing factory，但它不进入生产 CLI。

Agent 需要读取固定应用树，写入自己的 state/cache、AniMemo data backup 目录和 runtime socket，并访问 Docker daemon。Docker access is effectively privileged；systemd hardening 只缩小无关主机表面，不能把该进程描述为完全 sandboxed。

## Local RPC

Socket mode 是 `0660`，目录 mode 是 `0750`，属主/组为 `animemo-updater:animemo-api`。API 容器只挂载 `/run/animemo-updater`，从不挂载 `/var/run/docker.sock`。Agent 不监听 TCP。

请求是单行 JSON：

```json
{"operation":"get_status","params":{}}
```

最大请求为 64 KiB，最大响应为 1 MiB。允许操作只有：

- `get_status`
- `list_releases`
- `check_update`
- `plan_update`
- `apply_update`
- `rollback_previous`
- `get_operation`
- `get_logs`

禁止 `run_command`、`shell`、`exec`、`docker`、`compose`、`file_write`、`download_url`，也禁止在合法操作里夹带 service/path/image/repository/URL。子进程始终使用固定 executable 与 argv list，`shell=False`；备份所需的容器内 `sh -c` 是固定字符串，不含请求输入。

## Release verification

Release discovery 只访问 `yanyuhanyue/AniMemo`，并把 tag SemVer 通道与 GitHub Release 的 `draft/prerelease` metadata 绑定；错误标记的 Stable/RC/Beta 不进入可用列表。下载固定资产 `release-manifest.json` 与 `checksums.txt` 后，Agent 验证：

1. tag、channel、SemVer、40 位 commit 与 Manifest schema；
2. checksum 和固定 repository/platform/digest；
3. minimum updater version 与 database/configuration/Plugin SDK contract；
4. API/Web OCI attestation 的 repository、release workflow 与应用 commit；
5. Manifest attestation 的 repository、签署 workflow 与 `provenance.sourceCommit`。

Stable Manifest 保留 RC 的应用 commit 和 API/Web digest，但由 promotion workflow 的 commit 签署，因此 `release.commit` 与 `provenance.sourceCommit` 是两个明确身份。

## Compatibility and rollback

计划输入是 CURRENT、live database/configuration contract、实际 enabled Plugin SDK API 与目标 Manifest。一次性 bootstrap 通过当前 exact API image 的固定管理命令初始化真实启用集合；Staff status/list/plan 最多缓存该只读检查 30 秒，apply/rollback 在执行边界强制刷新。结果只有：

- **Safe Switch**：目标应用接受所有 live contracts；
- **Application Rollback**：旧应用接受当前 contracts，可只切回 API/Web；
- **Unsafe Downgrade**：任一 live contract 被拒绝，必须阻断。

Agent 不把 Django migration 文件号当 database schema version，不执行 reverse migration，不自动 restore database。迁移成功后若新应用 health 失败，仅在 PREVIOUS 接受全部 live database/configuration/Plugin SDK contracts 时回退应用；应用回退只替换 API/Web，live 数据与配置契约保持不变。

## Operation lifecycle

每次 apply/rollback 都先创建持久 Operation，HTTP/RPC 调用立即返回；后台线程更新 journal。全局跨进程 lock 防止 update/rollback 并发执行。

```text
idle
→ preflight
→ fetching
→ verifying
→ backup (when migration is required)
→ pulling
→ migrating (when required)
→ switching
→ verifying_health
→ succeeded
```

失败终态为 `failed_pre_switch`、`rolled_back` 或 `manual_recovery_required`。Agent 重启会把 switch 前未完成 Operation 标为 `failed_pre_switch`；migration/switch 已开始的 Operation 标为 `manual_recovery_required`，绝不自动重放 migration。每个会改变 database/configuration contract 的命令在执行前把 exact target Manifest 与 pending transition 写入 Operation journal；正常完成后才解析为 live contract。主机显式 `reconcile` 使用 current/target exact image 的只读 migration snapshot 判定 migration 实际停在 current、target 或 indeterminate：只接受前两者，部分 migration 始终保持阻断。Bootstrap 是唯一允许在该显式恢复边界幂等重试的 mutation。所有 event detail 在 `OperationStore` 的唯一持久化边界统一 redaction；调用方遗漏脱敏也不能把常见 password/token/Authorization/URL credential 明文写入 journal。

## Backup and switch

Preflight 固定检查至少 2 GiB 可用磁盘、至少 512 MiB `MemAvailable`、Docker/Compose、PostgreSQL/Redis/API/Web container health、当前 `/health/` 与 `/`。无 migration 时必须已有不超过 24 小时的 verified backup：metadata 路径位于固定 backup root、compressed SHA-256 匹配、UTC timestamp 合法、gzip 流可完整解压。有 migration 时 preflight 先确认固定 backup root 可写，随后 Agent 创建新的 `pg_dump` gzip 与 metadata；失败则不 pull/migrate/switch。

切换只执行：

```text
pull API@sha256
pull Web@sha256
optional migration job
bootstrap job
up --no-deps --force-recreate api web
stable health observations
```

`runtime-images.env` 以 `0600` 写入临时文件、fsync、atomic replace 并同步目录，避免半写状态或通过预置 link 改写 state root 外文件。每次 stable observation 要求 API/Web healthy、restart count 为零、`/health/`、`/`、`/login`、`/api/schema/`、`/api/docs/` 均为 HTTP 200，并扫描观察窗口内 API/Web stdout 与 stderr；HTTP 5xx 或 critical/fatal/panic/Traceback 会使 health gate 失败。原始日志不写入 Operation detail。

固定 state/data/cache root 及其受管子目录不能是 symlink 或 junction；Operation、plan、runtime、CURRENT/PREVIOUS/history、lock、Release asset 与 backup/metadata 必须是私有单链接普通文件。所有原子写和 gzip backup 使用系统独占创建的随机临时文件。Unix RPC 只会清理真实的陈旧 socket 节点；同名普通文件、目录或 link 会阻止启动，而不会被删除。

Plan 保存的是已经验证过的 exact Manifest 和哈希绑定，但它不替代执行期验证。apply/rollback 在 `FETCHING` 阶段强制绕过 Release cache，重新验证 exact tag、GitHub Release metadata、checksums 和 attestations；`VERIFYING` 阶段要求结果与 plan 或 PREVIOUS 槽位完全一致，任何差异都进入 `failed_pre_switch`，不执行 pull、migration 或 switch。

`reconcile` 只存在于 Host CLI，RPC 明确不暴露。它要求 exact Operation confirmation；若存在 pending contract transition，会重新验证并拉取 journal 中绑定的 exact target，且不会把 running app 自报的 Manifest contract 当作物理数据库事实。无法明确判定的状态继续保持全局 `manual_recovery_required` barrier。

PostgreSQL、Redis、Docker daemon、OpenResty、cloudflared 与其他 VPS 服务都不在 Agent 的操作集合中。

## Installation and bootstrap

手动安装或升级 Agent：

```bash
sudo sh deploy/install-updater.sh
```

安装器只写 `/opt/animemo-updater`、`/var/lib/animemo-updater`、`/run/animemo-updater`、自身 systemd/sysusers/tmpfiles 资产和 `/usr/local/bin/animemo-updater`。每个 Updater 版本安装到独立目录，再原子切换 `current` symlink；启动失败时恢复旧 symlink。它不部署 AniMemo、不导入 CURRENT、不重启 Docker 或其他服务。

公开 GitHub Release assets 与 attestations 优先匿名读取。确实需要认证时，只在 Agent Host 配置 read-only contents/packages credential：GitHub CLI 使用固定 `GH_CONFIG_DIR=/var/lib/animemo-updater/gh`，GHCR Docker credential 保存在 Agent 用户的 Host home/config。token 不进入数据库、Staff UI、API/RPC、Manifest、Operation journal 或日志；不得使用 repo write、admin 或 workflow token。凭据安装、轮换和撤销是人工 Host 运维动作，不属于 Agent allowlist。

一次性 CURRENT bootstrap：

```bash
sudo sh deploy/bootstrap-updater.sh /path/to/verified/release-manifest.json
```

脚本把 operator 已验证的 Manifest 复制到固定 bootstrap 路径，再以 Agent 用户执行 `import-current`。导入会重新验证 Manifest，并且无论内容是否相同都拒绝第二次执行。生产 cutover 前必须另外证明运行 API/Web 的 exact digest 与该 Manifest 一致。

## Staff interface and errors

Staff Update API 属于 `manage_system`，Stable 默认可见；只有 superuser 可见 RC/Beta。所有 mutation 需要 CSRF，使用 scoped throttling，并写 staff audit。apply 必须输入 `APPLY <version>`，rollback 必须输入 `ROLLBACK PREVIOUS`。

Agent unavailable 返回 503 `updater_unavailable`；兼容性、并发或 operation state 冲突返回 409；其他拒绝返回稳定 `{code, detail}`。PREVIOUS 的实时 compatibility 随 status DTO 返回，Staff UI 会展示判定并禁用 Unsafe Downgrade；rollback RPC 仍在执行前再次裁决。日志在写入 journal 和返回 RPC 前执行 secret redaction。

Update Agent endpoints 不属于普通 Public SDK，也不向 Plugin SDK 或 Integration Protocol v1 暴露。
