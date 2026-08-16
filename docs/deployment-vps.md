# AniMemo v1.1 主机部署与运维

本文只描述 v1.1 canonical、provider-neutral 部署。历史 panel 路径、app-tree env、ZIP bootstrap 与 break-glass 脚本不是受支持入口，也不会被 Installer、Updater、Restore、Migration 或 Doctor 自动发现。

本阶段只实现和验证部署基础设施；Production、DNS、TLS、反向代理与 Release 发布均为 **NOT RUN**。

## Authority 与固定布局

安装和更新只消费经过完整验证的 GitHub Release、Manifest、checksums、provenance、attestation 与不可变 OCI `repository@sha256:digest`。目录名、mutable tag、工作树和本地缓存都不是 Release Identity。

唯一受支持的主机根为：

```text
/opt/animemo
/data/animemo
/opt/animemo-updater
/var/lib/animemo-updater
/run/animemo-updater
```

程序和 exact Compose 材料位于 `/opt/animemo`；数据库、Redis、媒体、插件、备份、日志、private state 与托管配置位于 `/data/animemo`。Updater 的 durable state 与 canonical locator 位于 `/var/lib/animemo-updater`，运行时 socket 和派生 env 位于 `/run/animemo-updater`。

## 托管配置

唯一配置 authority 是受保护的 `/data/animemo/config/animemo.json`，schema 为 `animemo.managed-config/v1`。Updater 从它和 exact Release 派生 `/run/animemo-updater/managed.env`；派生文件可重建、使用 `0600`，不是第二 authority，也不得人工编辑。

Public Origin 与 Listen 独立。默认 Listen 为 `127.0.0.1:8088`。非 loopback 监听或 HTTP Public Origin 都必须分别显式确认，Installer 和配置管理不会修改 DNS、TLS、reverse proxy 或 firewall。

```bash
animemo-updater config show
animemo-updater config validate --public-origin https://anime.example
animemo-updater config dry-run --listen 127.0.0.2:8088
animemo-updater config set-origin https://anime.example
animemo-updater config set-listen 127.0.0.2:8088
animemo-updater config apply --public-origin https://anime.example --accept
```

`show`、plan、Doctor 与错误结果只报告 secret 为 `configured`、`missing` 或 `invalid`，不会显示值、长度、摘要或片段。`apply` 绑定 instanceId、config revision 与 locator digest，原子替换配置，只协调 AniMemo API/Web，然后验证 local health、exact Release、locator CAS 与 Doctor。失败会回滚；回滚失败进入 `RECOVERY_REQUIRED`。

## Fresh Install 与 Restore-to-New

Installer 只支持 Fresh Install 和 Restore-to-New。它先完成只读 target classification、平台资格、端口、exact Release、完整 Installer Material Profile 与 canonical Compatibility Engine 评估，再生成绑定所有关键输入的 plan。dry-run 不创建目录、secret、config、locator 或 Updater state。

执行顺序由 Installer Runtime 固定：准备 canonical roots、写入受保护配置、安装 exact materials、启动 PostgreSQL/Redis、显式运行 migration 与 bootstrap、启动 API/Web、验证 health 与 exact release、调用 canonical Updater adoption、最后发布 locator并运行完整 Doctor。健康且完全一致的同版本实例可以 verified no-op；不同版本返回 Updater handoff；foreign、partial、corrupt 或 data-bearing but undiscoverable 目标 fail closed。

Fresh 安装只生成一次 stable `instanceId` 和所需 secret，不自动创建管理员。安装成功后通过浏览器 `/setup` 完成私有首次管理员生命周期。

Restore-to-New 直接编排 canonical Restore Runtime。它重新验证 Backup v1、Secret Envelope、目标 Release 和材料，保留源 instanceId、CEK、用户、Resource Identity、Memory Identity、merge history 与 opaque future payload。保护输入只能来自受保护 key/passphrase 文件或 FD/stdin；secret 不进入 argv、环境、plan、journal 或结果。失败保留独立 durable recovery evidence，不会自动删除数据库或 restored data。

## Updater

Updater 只从 `/var/lib/animemo-updater/instance.json` 与 canonical managed config 发现实例。初始 CURRENT 必须通过固定 adoption request 调用 `animemo-updater adopt-current`；Installer、Restore 与 Migration target 不得手写 CURRENT/PREVIOUS、伪造 Manifest 或提前发布 locator。

正常更新由 Staff 通过本地 Unix Socket 请求 Host Agent。Agent 会重新验证 exact Release、deployment bytes、当前 locator/config、backup gate、Compatibility 与运行中 API/Web digest，然后只切换 AniMemo API/Web。数据库 migration 和 bootstrap 是显式 one-shot job；普通 rollback 不 reverse migrate，也不自动 restore 数据库。

Host Updater 自身只通过 exact verified material 安装：

```bash
sudo sh deploy/install-updater.sh
```

该脚本只管理 `/opt/animemo-updater`、自身 launcher 与 systemd/sysusers/tmpfiles 资产，不部署 AniMemo、不导入 CURRENT、不重启 Docker 或其他服务。

## 受控 Compose 运维

人工诊断必须使用固定项目、固定 Compose 与派生 env：

```bash
cd /opt/animemo
/usr/bin/docker compose --project-name animemo \
  --env-file /run/animemo-updater/managed.env \
  -f /opt/animemo/deploy/docker-compose.yml \
  -f /opt/animemo/updater/docker-compose.runtime.yml ps
```

不要把该命令当作安装、更新或配置 authority。禁止 `docker system prune`、`docker volume prune`、`compose down -v`、Docker daemon restart、全局网络清理或操作其他 Compose project。

## 调度、凭据与外部基础设施

维护任务通过运行中的 API 容器执行，见 [Maintenance](maintenance.md)。公开 GitHub Release asset 和 attestation 优先匿名读取；需要凭据时只在 Updater Host 上配置最小只读权限。凭据不得进入 AniMemo 数据库、API、配置展示、operation journal、应用容器或 Release artifact。

管理员自行维护 DNS、TLS certificate、public reverse proxy、可信代理网段、firewall 与 provider callback allowlist。AniMemo 不安装或重载 OpenResty/Caddy/Traefik/cloudflared，不申请证书，不修改 80/443、防火墙或 DNS。

## 验收与安全边界

Installer success 依赖完整 Doctor Basic required checks，关键检查不能以 `SKIPPED` 计为 PASS。至少验证 PostgreSQL、Redis、API/Web health、exact running release、Compose material、dependency image digest、managed config、locator、Updater state、setup lifecycle 与 durable write probe。

所有操作只允许 AniMemo-owned、operation-bound cleanup。不可逆 mutation 后不得删除数据库、restored user data、unknown media/plugin object 或 reverse migration；必须保留可诊断 recovery evidence。

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION UPDATE: NOT RUN
PRODUCTION RESTORE: NOT RUN
PRODUCTION MIGRATION: NOT RUN
RELEASE/RC/STABLE: NOT RUN
DNS/TLS/CLOUDFLARE: NOT RUN
SSH: NOT RUN
```
