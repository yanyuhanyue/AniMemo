# VPS Deployment

AniMemo 的生产部署分为两种流程：

- **完整发布**：使用 `deploy/deploy.sh` 替换应用树，执行 Compose 构建、完整 smoke test，并按需要安装/重载 AniMemo 专用 OpenResty 配置。
- **Scoped hotfix**：只替换 AniMemo 的 API/Web 请求服务，保留 PostgreSQL、Redis、共享 OpenResty、Cloudflare 和其他 Compose 项目。生产 hotfix 不使用临时 merge ref，必须使用已经合并到 `main` 的真实 merge SHA。

## Production Layout

默认生产路径与 Compose 文件如下：

```text
/opt/1panel/docker/compose/anime-journal/app
/opt/1panel/docker/compose/anime-journal/app/deploy/docker-compose.yml
/data/anime-journal/{postgres,redis,plugins,logs,backups,media}
```

所有 Compose 命令都要显式指定生产环境文件和 Compose 文件：

```bash
cd /opt/1panel/docker/compose/anime-journal/app
docker compose --env-file .env.production -f deploy/docker-compose.yml ps
```

`.env.production` 只保存在服务器，不进入发布 ZIP。生产环境必须保持 `DEBUG=false`、PostgreSQL、Redis、HTTPS、精确的 `ALLOWED_HOSTS`/CORS/CSRF 来源、Secure Cookie、可信代理网段和 `TURNSTILE_ENABLED=true`。同源部署默认使用 `SameSite=Lax`；跨站部署必须让 Session、CSRF、Refresh 三类 Cookie 一致使用 `SameSite=None; Secure`。

## Full Release

完整发布使用已经生成并校验过的 core-only ZIP 和 SHA 文件：

```bash
sudo sh deploy/deploy.sh \
  --archive /tmp/anime-journal-core-<stamp>.zip \
  --sha256 /tmp/anime-journal-core-<stamp>.sha256
```

首次从旧架构迁移时才使用 `--fresh`；清空本站数据必须额外使用 `--reset-data --yes`。这些选项只允许作用于 AniMemo 明确的数据目录，禁止使用全局 Docker prune、`down -v` 或其他 Compose 项目的 `down`。

完整发布脚本会：

1. 校验发布 ZIP 的 SHA、路径、符号链接和 core-only 内容。
2. 复制服务器上的 `.env.production`，校验数据根目录与媒体根目录。
3. 校验 Compose 配置，构建 API/Web 镜像并保留旧镜像标签用于失败恢复。
4. 启动 AniMemo Compose 项目并执行 `deploy/smoke-test.sh`。
5. 默认只安装并重载 AniMemo 专用的 OpenResty 配置，不重启 OpenResty 容器。
6. 将发布归档与 `current.json` 保存到 `/opt/1panel/docker/compose/anime-journal/releases`。

## Scoped Hotfix

适用于只修复 API/Web 代码、没有数据库 schema 变化的生产 hotfix。执行前先在 GitHub 审计 PR：

```bash
git fetch origin
git rev-parse origin/main
git log --oneline --decorate -10
git merge-base --is-ancestor <HOTFIX_HEAD_SHA> <HOTFIX_MERGE_SHA>
```

必须记录以下四个身份，并确认生产最终 checkout 与 merge SHA 完全一致：

```text
PREVIOUS_PRODUCTION_SHA
HOTFIX_HEAD_SHA
HOTFIX_MERGE_SHA
FINAL_PRODUCTION_SHA == HOTFIX_MERGE_SHA
```

不要把 GitHub PR 的 temporary/test merge ref 当作生产 release identity；如果 PR head 发生变化，先重新审计新增 commit 和 required checks。

### Preflight and migration boundary

在生产服务器执行只读预检：

```bash
cd /opt/1panel/docker/compose/anime-journal/app
git rev-parse HEAD
docker compose --env-file .env.production -f deploy/docker-compose.yml ps
df -hT /
free -h
gzip -t /data/anime-journal/backups/<verified-backup>.sql.gz
```

没有 schema 变化的 hotfix 必须明确记录：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.yml \
  exec -T api python manage.py check
docker compose --env-file .env.production -f deploy/docker-compose.yml \
  exec -T api python manage.py makemigrations --check --dry-run
git diff --name-status <PREVIOUS_PRODUCTION_SHA> <HOTFIX_MERGE_SHA> -- 'backend/**/migrations/'
```

若无 migration 变化，`NEW MIGRATION` 和 `MIGRATION RUN` 均为 `NOT APPLICABLE`。不要 reverse migration、不要回滚到旧 schema，也不要为了 hotfix 重做数据转换。API 容器启动时即使执行了正常的 migrate 命令，也必须确认输出为 `No migrations to apply`。

### Build and replacement scope

只构建 AniMemo 的 API/Web：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.yml build api web
docker compose --env-file .env.production -f deploy/docker-compose.yml \
  up -d --no-deps --force-recreate api web
```

不要构建或重启 PostgreSQL、Redis，也不要触碰 AstrBot、NapCat、Gotify、dailyhub、PHP、Cloudflare 或全局 OpenResty。确认 API/Web 容器 image digest 与本次构建记录一致，Compose 端口仍为 `127.0.0.1:8088->80/tcp`。

### Acceptance checks

至少完成以下检查并保留输出：

- 本机与公网的 `/`、`/login`、`/health/`、`/api/schema/`、`/api/docs/` 均为 HTTP 200。
- Swagger 使用同源 sidecar，未引用 jsDelivr 等外部脚本。
- `DEBUG=false`、Turnstile 已配置且 fail-closed、Session/CSRF/Refresh Cookie 的 Secure/SameSite 配置未降低。
- 缺少 CSRF 的 refresh 返回 `403 csrf_failed`。
- 过期或 legacy refresh 返回 `401 session_expired`，不得返回 PostgreSQL outer-join lock 500，并确认 refresh cookie 被清除。
- 若没有专用 smoke identity，将有效 refresh 明确标记 `NOT RUN`，不要使用真实用户凭据。
- 读取 Journal、Watch History、Analytics、外部集成和官方插件状态；插件必须记录版本、enabled/healthy、rollback floor 和 package SHA。
- 扫描 API/Web scoped logs，不得出现 Traceback、HTTP 500、`FOR UPDATE cannot be applied to the nullable side of an outer join`、DB/Redis/plugin runtime error。

### Scoped restart persistence

初次替换成功后只重启请求服务：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.yml restart api web
```

等待 API/Web healthy 后，重复 health、legacy refresh 回归、插件状态、关键数据计数和日志扫描。禁止重启 PostgreSQL、Redis、Docker daemon、OpenResty 或 cloudflared。

## Backup and final report

Hotfix 不需要因为代码-only 变更重复制作数据库备份，但必须确认既有 full backup 仍存在、SHA-256 一致且 `gzip -t` 通过。不得删除备份、restore 数据库或手工修改插件 CAS。

最终报告只使用以下状态：`PASS`、`FAIL`、`NOT RUN`、`NOT APPLICABLE`。至少包含：

```text
PR MERGE:
CI:
RELEASE GATE:
NEW MIGRATION:
MIGRATION RUN:
DATABASE RESTORE:
LEGACY/EXPIRED REFRESH 401:
VALID REFRESH:
NO OUTER-JOIN LOCK 500:
API:
WEB:
HEALTH:
SWAGGER:
WATCH HISTORY:
OFFICIAL PLUGIN:
SCOPED RESTART:
POST-RESTART REFRESH:
POST-RESTART HEALTH:
PRODUCTION HOTFIX:
```

只有当真实 merge SHA 已部署、refresh PostgreSQL 回归不再 500、API/Web 在 scoped restart 后仍 healthy、没有 migration/data rollback，且共享 VPS 其他服务未受影响时，才能报告 `PRODUCTION HOTFIX: PASS`。
