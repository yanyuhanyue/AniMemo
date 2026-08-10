# Production Hotfix Record: 2026-08-10

本记录对应 2026 年 8 月 10 日（Asia/Shanghai）AniMemo 生产 hotfix。它记录实际执行结果，不是可复用的凭据或操作脚本；下一次发布应重新生成 SHA、备份和镜像身份。

## Release identity

```text
PR: #24
Hotfix head: 303a432e589fb20b40c6d8dbe82c0cb42f8ed162
Hotfix merge: abdf76753f24dfed721517427507d9b9ee355525
Previous production: 58275a3c9f7fe1cd46c35aacf2e006f3011b71a7
Final production: abdf76753f24dfed721517427507d9b9ee355525
```

PR #24 已真实合并到 `main`，required CI、Release Gate 和 hotfix head ancestor 校验均通过。生产 checkout 与 `origin/main` 均为 `abdf767…`，worktree clean。

## Scope and backup

- 只构建并替换 AniMemo `api` 与 `web`。
- PostgreSQL、Redis、Docker daemon、OpenResty、Cloudflare、AstrBot、NapCat、Gotify、dailyhub 和 PHP 未重启或修改。
- 未执行数据库 restore、reverse migration、全局 prune、`down -v` 或插件 CAS 手工操作。
- 既有备份保持不变：

```text
/data/anime-journal/backups/anime-journal-pre-58275a3-20260810-090720.sql.gz
size: 16117 bytes
sha256: 0d91779a13940b39c6f13d6d7aa2f1b40b541356e42035c463c2d3b2cf8a58e6
gzip -t: PASS
```

## Migration boundary

本 hotfix 只修改 refresh token 行锁处理，没有 migration 文件变化：

```text
NEW MIGRATION: NOT APPLICABLE
MIGRATION RUN: NOT APPLICABLE
makemigrations --check --dry-run: No changes detected
django check: 0 issues
```

API 启动时执行了正常的 migrate 检查，输出为 `No migrations to apply`；没有执行 reverse migration，也没有回滚到旧 schema。

## Build and runtime

实际构建的 AniMemo 镜像：

```text
api: sha256:158a2b2acea823876522a3810bdbb459b0a765f92da15a78d965ab27d639da80
web: sha256:14034a1a20b7cb2eb72fde2a385e9b48f082ad2b4762f271d6d06b4c35408a00
```

最终容器状态：API、Web、PostgreSQL、Redis 均 healthy；公网 Web 绑定仍为 `127.0.0.1:8088`。根分区约 48G 可用，内存 available 约 5.6GiB。

## Auth regression

服务器端 PostgreSQL regression probe 在事务中创建临时 legacy refresh token，完成后回滚事务，因此没有污染生产数据：

```json
{
  "csrf_missing_status": 403,
  "csrf_missing_code": "csrf_failed",
  "legacy_status": 401,
  "legacy_code": "session_expired",
  "legacy_cookie_cleared": true,
  "rows_unchanged": true
}
```

回归前后 `OutstandingToken=1`、`BlacklistedToken=0`、`UserSecurityProfile=1`。未使用真实用户凭据；专用有效 smoke identity 候选数为 0，因此：

```text
VALID REFRESH: NOT RUN
NO OUTER-JOIN LOCK 500: PASS
```

生产安全配置保持不变：`DEBUG=false`、Turnstile enabled/secret configured，Session/CSRF/Refresh Cookie 均为 `Secure=true; SameSite=Lax`。

## HTTP, plugin and restart checks

本机和公网的 `/`、`/login`、`/health/`、`/api/schema/`、`/api/docs/` 均返回 HTTP 200；Swagger 使用同源 sidecar，未发现 jsDelivr。Analytics 计算成功，Journal 和 WatchHistory read path 正常。

官方插件状态：

```text
watch-history-importer: 0.4.0
enabled: true
healthy: true
rollback_floor: 0.4.0
package sha256: 9ec0b4b58a8917af1f47a7edba81e78e7d734e712466e23f3a1869a1ce1f23d3
```

部署后仅执行一次：

```bash
docker compose --env-file .env.production -f deploy/docker-compose.yml restart api web
```

重启后 API/Web 仍 healthy，refresh regression、插件状态、数据读取和 HTTP smoke 全部通过。重启后的 API/Web scoped logs 未发现 Traceback、HTTP 500、PostgreSQL outer-join lock、DB/Redis 或 plugin runtime error。

## Final status

```text
PR MERGE: PASS
CI: PASS
RELEASE GATE: PASS
NEW MIGRATION: NOT APPLICABLE
MIGRATION RUN: NOT APPLICABLE
DATABASE RESTORE: NOT RUN
LEGACY/EXPIRED REFRESH 401: PASS
VALID REFRESH: NOT RUN
NO OUTER-JOIN LOCK 500: PASS
API: PASS
WEB: PASS
HEALTH: PASS
SWAGGER: PASS
WATCH HISTORY: PASS
OFFICIAL PLUGIN: PASS
SCOPED RESTART: PASS
POST-RESTART REFRESH: PASS
POST-RESTART HEALTH: PASS
PRODUCTION HOTFIX: PASS
```
