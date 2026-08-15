# VPS Deployment

> 本文件是当前 v1.0 生产实例与 legacy bootstrap/break-glass 的兼容 runbook。v1.1 新安装的 canonical、provider-neutral 规则由 [Deployment Boundary v1](deployment-boundary-v1.md)、[Filesystem Layout v1](filesystem-layout-v1.md)、[Installer Contract v1](installer-contract-v1.md) 和 [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md) 冻结；下文的 1Panel、OpenResty、certbot、当前域名与旧 app root 不属于 v1.1 Installer 依赖。

AniMemo 的正常生产更新路径是可信、不可变 Release 加受限 Host Update Agent：

```text
GitHub Release Producer
→ release-manifest.json + checksums + attestations
→ GHCR API/Web repository@sha256:digest
→ AniMemo Update Agent
→ explicit migration/bootstrap
→ scoped API/Web switch
→ health/smoke
```

`deploy/deploy.sh` 不再是普通发布入口。它只允许显式 `--bootstrap` 或 `--break-glass`，使用 core ZIP 在服务器构建 API/Web，供首次安装、旧架构切换或 Update Agent 无法运行时人工恢复。日常更新不得用 ZIP、`git pull` 或服务器端 build 替代 immutable Release。

本轮只实现和验证部署基础设施；生产安装、更新、迁移和 smoke 均为 **NOT RUN**。

## Current v1.0 production layout (legacy compatibility profile)

固定生产路径：

```text
/opt/1panel/docker/compose/animemo/app
/opt/1panel/docker/compose/animemo/app/deploy/docker-compose.yml
/data/animemo/{postgres,redis,plugins,logs,backups,media,private}
/opt/animemo-updater
/var/lib/animemo-updater
/run/animemo-updater/updater.sock
```

生产 Compose 只接受 Manifest 给出的 digest identity：

```text
ANIMEMO_API_IMAGE=ghcr.io/yanyuhanyue/animemo-api@sha256:<digest>
ANIMEMO_WEB_IMAGE=ghcr.io/yanyuhanyue/animemo-web@sha256:<digest>
```

`deploy/docker-compose.yml` 没有 API/Web `build:`。`deploy/docker-compose.build.yml` 只供 CI、bootstrap 和 break-glass 使用。所有人工 Compose 命令都必须显式指定 `.env.production` 与 Compose 文件；禁止 mutable `latest` 作为部署身份。

`.env.production` 只保存在服务器，不进入 Release 或 ZIP。生产环境必须保持 `DEBUG=false`、PostgreSQL、Redis、HTTPS、精确的 `ALLOWED_HOSTS`/CORS/CSRF 来源、Secure Cookie 与可信代理网段。Turnstile 是安装后的可选 SiteSettings 配置，不写入生产 ENV，也不参与镜像构建；需要时在 Staff「安全验证」中填写当前实例的 Site Key 与 Secret Key。同源部署默认使用 `SameSite=Lax`；跨站部署必须让 Session、CSRF、Refresh 三类 Cookie 一致使用 `SameSite=None; Secure`。

## Normal update

正常更新由 Staff 系统通过本地 Unix Socket 请求 Host Agent。Agent 固定执行：

1. 验证 Release Manifest、checksums、GitHub Release 的 draft/prerelease metadata、OCI digest 与 GitHub attestations。
2. Preflight 检查固定路径、至少 2 GiB 可用磁盘、至少 512 MiB `MemAvailable`、Docker/Compose、PostgreSQL/Redis/API/Web container health、当前 `/health/` 与 `/`，以及 backup gate。
3. 根据 CURRENT、目标 Manifest 和 live contracts 计算 Safe Switch、Application Rollback 或 Unsafe Downgrade。实际启用的 Plugin SDK API 从当前 API image 读取；Staff status/list/plan 最多缓存 30 秒，apply/rollback 执行前再次强制刷新。
4. 无 migration 时，preflight 完整验证 24 小时内的 backup metadata、compressed SHA-256、UTC timestamp 与 gzip 流；有 migration 时先确认固定 backup root 可写，再创建并完整校验新备份。
5. 按 digest pull API/Web；需要时以目标 API image 运行一次性 `migration` job，然后运行 `bootstrap` job。
6. 只替换 AniMemo API/Web。`runtime-images.env` 使用 `0600`、fsync 与 atomic replace，不跟随预置 link 原地写入。
7. 稳定窗口每次检查 API/Web container health、restart count、`/health/`、`/`、`/login`、`/api/schema/`、`/api/docs/`，并扫描观察窗口内 API/Web stdout+stderr 的 HTTP 5xx 与 critical/fatal/panic/Traceback；通过后才更新 CURRENT/PREVIOUS。

执行 apply 或 rollback 时，Agent 不直接信任 plan/PREVIOUS 中已缓存的 Release 结果：它会绕过 Release cache 再验证 exact tag、Release metadata、checksums 与 attestations，并要求 Manifest 与绑定内容完全一致。state、cache、backup 及其受管子目录拒绝 symlink/junction 与 hard link；原子状态和 gzip backup 使用随机私有临时文件。RPC 启动只清理真实 socket，不删除同名普通文件。

API 容器启动命令只运行 Gunicorn，不隐式执行 migration、bootstrap 或 static collection。数据库 migration 永远是显式 one-shot job。普通 Application Rollback 只切换 API/Web；不会 reverse migration，也不会自动 restore 数据库。

PostgreSQL、Redis、Docker daemon、共享 OpenResty、cloudflared、AstrBot、NapCat、Gotify 和其他 Compose 项目不在 Agent 的允许操作集合中。Django 只挂载 `/run/animemo-updater`，永远不挂载 Docker socket。

## Install or upgrade the Host Agent

从经过审计的本地应用树手动安装或升级 Agent：

```bash
sudo sh deploy/install-updater.sh
```

安装器只管理 AniMemo Updater 自身目录、launcher、systemd/sysusers/tmpfiles 资产。每个版本安装到独立目录，再原子切换 `current` symlink；服务启动失败时恢复旧 symlink。它不会部署 AniMemo、导入 CURRENT、重启 Docker 或触碰其他宿主机服务。

首次切换前，在人工证明运行中的 API/Web digest 与已验证 Manifest 完全一致后，执行一次性 CURRENT bootstrap：

```bash
sudo sh deploy/bootstrap-updater.sh /path/to/verified/release-manifest.json
```

`import-current` 会用 Manifest 的 exact API digest 执行固定只读检查，记录真实启用的 Plugin SDK API，而不是把 Core 支持列表误当成启用列表。无论 Manifest 是否相同都拒绝第二次导入。后续 CURRENT/PREVIOUS 只能由成功的 Agent Operation 维护。

## Agent credentials

公开 GitHub Release assets 和 attestations 优先匿名读取。只有仓库或 GHCR 实际要求认证时，才在 Agent Host 配置最小只读凭据：

- GitHub credential 仅允许读取 repository contents/packages；不得使用 repo write、admin 或 workflow token。
- `GH_CONFIG_DIR=/var/lib/animemo-updater/gh`；token 只存在宿主机 secret/GitHub CLI config，不进入 AniMemo 数据库、Staff UI、API 请求、Operation journal 或日志。
- GHCR Docker credential只保存在 Agent 用户的 Host home/config 中，由 Docker 客户端读取；不得复制进应用容器或 `.env.production`。
- 安装、轮换和撤销凭据是人工 Host 运维动作；Agent API 不提供写入或展示 credential 的操作。

Docker socket access is effectively privileged。systemd hardening 只缩小无关主机表面，不能把 Agent 描述为完全 sandboxed。

## Bootstrap and break-glass ZIP path

只有首次安装、旧架构 cutover 或 Agent 已不可用且人工批准恢复时，才允许：

```bash
sudo sh deploy/deploy.sh --bootstrap \
  --archive /tmp/animemo-core-<stamp>.zip \
  --sha256 /tmp/animemo-core-<stamp>.sha256

sudo sh deploy/deploy.sh --break-glass \
  --archive /tmp/animemo-core-<stamp>.zip \
  --sha256 /tmp/animemo-core-<stamp>.sha256
```

脚本校验 core-only ZIP 和 SHA，使用 build override 构建 API/Web，显式运行 migration/bootstrap，再只替换 API/Web。它不会自动 reverse migration 或 restore 数据库。`--reset-data --yes` 仅属于明确的首次 bootstrap destructive reset；不得用于普通更新或 break-glass。

全新数据库的 bootstrap 会把一次性初始化码写入 `/data/animemo/private/setup-code`。该目录由 API UID/GID 拥有并使用 `0700`，文件使用 `0600`；初始化码不写入命令输出、日志、API 响应或 Release artifact。操作者读取文件后访问同源 `/setup` 创建全新的首位管理员，成功提交会删除文件并把数据库安装状态永久锁为 `initialized`。普通注册在此之前保持关闭。升级中的已有数据库会迁移为已初始化，绝不会根据可预测用户名提升已有账号。完整流程见 [`First-run Bootstrap`](first-run-bootstrap.md)。

ZIP/current.json 归档只保留为 legacy evidence，不是 signed OCI release identity，也不得被静默导入为 Agent CURRENT。

## Preflight, backup and acceptance

任何未来生产切换都必须先记录：

```text
RELEASE VERSION
RELEASE CHANNEL
release.commit
provenance.sourceCommit
API repository@digest
WEB repository@digest
CURRENT/PREVIOUS
database/configuration contracts
enabled Plugin SDK APIs
verified backup path/checksum/time
```

`release.commit` 是应用构建 commit；`provenance.sourceCommit` 是实际运行 Release/Promotion 签署 workflow 的 commit。Stable 保留 RC 的应用 commit 和 image digests，但 Stable Manifest 可由较新的 promotion workflow commit 签署；两者不得混为一个字段。

生产验收至少覆盖本机与公网 `/`、`/login`、`/health/`、`/api/schema/`、`/api/docs/`，关键认证回归、Journal/Watch History/Analytics、Integration/Bridge、官方插件状态、API/Web logs、CURRENT/PREVIOUS/PREVIOUS compatibility 和 scoped restart persistence。没有专用 smoke identity 时，有效 refresh 明确记录 `NOT RUN`，不得使用真实用户凭据凑证据。

不得执行全局 Docker prune、volume prune、Docker daemon restart、PostgreSQL/Redis restart、共享 OpenResty restart 或 cloudflared 修改。不得删除备份、自动 restore 数据库、手工修改插件 CAS 或未知远程媒体对象。

最终报告只使用 `PASS`、`FAIL`、`NOT RUN`、`NOT APPLICABLE`。本阶段固定为：

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION UPDATE AGENT INSTALL: NOT RUN
PRODUCTION RC: NOT RUN
PRODUCTION SMOKE: NOT RUN
DATABASE PRODUCTION MIGRATION: NOT RUN
DATABASE RESTORE: NOT RUN
R2 PRODUCTION WRITE: NOT RUN
R2 CLEANUP: NOT RUN
SSH: NOT RUN
```
