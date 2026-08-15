# AniMemo / My Anime Memory / 我的动漫记忆库

这是一个独立实现的全栈番剧手账，产品定位为 AniMemo（我的动漫记忆库）。它提供高对比配色、重描边、错位阴影、列表/海报墙切换与响应式卡片等产品交互，并加入可独立部署的私人数据系统。

> 发布前请确认品牌视觉、文案与图片素材均为自有、原创或已获得明确授权。第三方内容继续遵循其自身来源与使用条件。

## 许可证与内容边界

AniMemo 自有源代码采用 PolyForm Noncommercial License 1.0.0；这是一份 source-available、非商业源码许可证，不代表 AniMemo 是 OSI Open Source 项目。请先阅读 [PolyForm Noncommercial License 1.0.0](LICENSE) 与字节一致的 [PolyForm-Noncommercial-1.0.0.md](PolyForm-Noncommercial-1.0.0.md)。

品牌名称、品牌视觉和 AniMemo 原创默认资产受 [TRADEMARKS](TRADEMARKS) 及相关品牌条款单独约束。仓库中的 `public/assets/avatar.png`、`public/assets/featured-column.png`、`public/assets/site-icon.png`、`public/assets/posters/poster-01.webp` 与 `public/assets/posters/poster-02.webp` 是 AniMemo 控制的默认品牌视觉素材，其中 `poster-01.webp` 也作为缺失封面 fallback；Bangumi/provider 返回的番剧封面、条目元数据及其他第三方内容不属于 AniMemo 自有资产，也不会因 PolyForm 被重新许可。详细边界见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) 和 [许可证来源审计](docs/license-provenance-audit-20260813.md)。

## 已实现

- React 19、Vite、Tailwind CSS 3、GSAP 前端
- Django 5 + Django REST Framework + session-versioned JWT API
- 注册、邮箱激活、登录、忘记/重置密码
- 私人番剧记录新增、修改、删除、搜索、状态筛选、列表/海报墙
- 自定义标签和标签颜色
- 记录公开/私密切换、个人公开页、单条分享链接
- 精选专栏草稿、投稿审核、申请下架、公开专栏接口
- JSON/CSV 导入与 JSON 导出
- 账号昵称、头像、强调色、默认视图与公开设置
- PostgreSQL、Redis、Cloudflare R2、Nginx/OpenResty 与 Resend 的生产配置

## 目录

```text
animemo/
├─ src/                 React 页面、组件与样式
├─ public/assets/       本地演示图片
├─ backend/             Django 项目与 journal API
├─ deploy/              Docker Compose、Nginx 与宿主机部署脚本
├─ .env.example         全部环境变量模板
└─ public/              静态资源与 Shared React Runtime wrapper
```

## 本地快速启动

要求 Node.js 20+、Python 3.12+。

推荐使用跨平台 bootstrap（会创建或复用 `.venv`、安装锁定依赖、执行迁移，并同时启动 Django 与 Vite）：

```powershell
./scripts/dev.ps1
```

```bash
./scripts/dev.sh
```

只准备依赖和数据库、不启动服务：

```powershell
./scripts/dev.ps1 -SetupOnly
./scripts/dev.sh --setup-only
```

脚本使用 SQLite + LocMemCache，仅限本地开发；生产环境仍需要 PostgreSQL + Redis。脚本不会预设管理员凭据，只会生成私有一次性初始化码，随后由浏览器 `/setup` 创建首位管理员。详细说明见 [`docs/local-development.md`](docs/local-development.md)。

文档入口：[`首次运行引导`](docs/first-run-bootstrap.md)、[`API 错误契约`](docs/api-errors.md)、[`前端状态与路由架构`](docs/frontend-architecture.md)、[`依赖更新工作流`](docs/dependencies.md)、[`维护与调度`](docs/maintenance.md)、[`插件开发`](docs/plugin-development.md)、[`集成协议`](docs/integration-protocol-v1.md) 和 [`生产部署`](docs/deployment-vps.md)。

手动启动仍然支持。复制 `.env.development.example` 为 `.env`，然后运行：

```bash
npm ci
python -m pip install -r backend/requirements.txt
mkdir -p runtime/private && chmod 0700 runtime/private
python backend/manage.py migrate
python backend/manage.py bootstrap_animemo
python backend/manage.py runserver 8000
```

另开终端启动前端：

```bash
npm run dev -- --host 0.0.0.0
```

前端默认访问 `http://localhost:5173`，canonical API 是 `http://localhost:8000/api/v1/`。现有 `/api/` Core 路径仅作为兼容别名保留；开发环境中后端不可用时，登录页会进入只写浏览器 `localStorage` 的演示模式，生产构建不会启用这个回退。

首页数据按以下顺序加载：`VITE_PUBLIC_SHOWCASE_SLUG` 对应的公开 API、当前浏览器里的私人手账数据、仓库演示数据。配置公开手账 UUID 后，页面会在重新聚焦及每 60 秒自动刷新，统计卡始终由当前记录实时聚合。

## API 摘要

| 模块 | 路径 |
|---|---|
| JWT | `GET /api/v1/auth/csrf/`、`POST /api/v1/token/`、`POST /api/v1/token/refresh/`、`POST /api/v1/auth/logout/` |
| 首次运行 | `GET /api/v1/setup/status/`、`POST /api/v1/setup/` |
| 注册与邮件 | `POST /api/v1/auth/register/request/`、`POST /api/v1/auth/register/verify/`、`POST /api/v1/auth/register/complete/`、`/api/v1/auth/password-reset/` |
| 手账 CRUD | `/api/v1/entries/` |
| 快捷筛选 | `/api/v1/filters/` |
| 专栏投稿 | `/api/v1/columns/`、`POST /api/v1/columns/{id}/submit/` |
| 导入导出 | `/api/v1/import/`、`/api/v1/export/` |
| 个人设置 | `/api/v1/settings/me/` |
| 公开展示 | `/api/v1/showcase/{public_slug}/`、`/api/v1/shared/{share_slug}/` |
| 健康检查 | `/health/` |
| OpenAPI Schema | `GET /api/schema/` |
| Swagger UI | `GET /api/docs/` |

## 插件开发

项目采用经过管理员审查的 Plugin SDK v2 运行时。插件包使用 `.ajplugin` 格式，清单、命名空间、权限、安全、测试和发布规范见 [`docs/plugin-development.md`](docs/plugin-development.md)；宿主接口见 [`docs/plugin-sdk-v2.md`](docs/plugin-sdk-v2.md)。

全栈空白模板位于 [`plugins/_template/`](plugins/_template/)，复制并替换模板标识后使用。插件清单可通过以下命令检查：

```bash
npm run test:plugins
```

## 生产部署

正式拓扑为：Cloudflare → Nginx/OpenResty → React 静态站点与 Django API → PostgreSQL/Redis；媒体后端由 Superuser 在“媒体存储”页面配置，可按优先级使用多个 Cloudflare R2 与固定根目录下的 Local Server Storage，插件包写入持久化插件卷。复制 [`.env.production.example`](.env.production.example) 为 `.env.production`，替换所有 placeholder，并确保 `POSTGRES_PASSWORD` 与 `DATABASE_URL` 中的密码完全一致。

生产环境强制要求 `DEBUG=false`、PostgreSQL、共享 Redis、独立 `CREDENTIAL_ENCRYPTION_KEY`、HTTPS `ANIMEMO_PUBLIC_ORIGIN`、精确的 CORS/CSRF 来源、至少 50 个字符的随机 `DJANGO_SECRET_KEY`，以及显式的可信代理网段。Compose 内部 PostgreSQL 使用私有 Docker 网络，因此模板设置 `DATABASE_SSL_REQUIRE=false`；连接要求 TLS 的外部 PostgreSQL 时必须改为 `true`。媒体存储由 Superuser 登录后在“媒体存储”页面创建；尚未配置时网站仍可启动，但媒体上传会返回 `MEDIA_STORAGE_SETUP_REQUIRED`。

生成凭证主密钥：

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

当前 canonical public origin 为 `https://animemo.cc`，media origin 为 `https://media.animemo.cc`；前端通过同源 `/api` 访问 Django。生产配置使用 `ALLOWED_HOSTS=animemo.cc`，并将 `ANIMEMO_PUBLIC_ORIGIN`、CORS 与 CSRF origin 都设为 `https://animemo.cc`。`www.animemo.cc` 不加入 Django，由 OpenResty 明确 301 到主域名。Cookie 使用：

```env
SESSION_COOKIE_SAMESITE=Lax
CSRF_COOKIE_SAMESITE=Lax
REFRESH_COOKIE_SAMESITE=Lax
SESSION_COOKIE_SECURE=true
CSRF_COOKIE_SECURE=true
REFRESH_COOKIE_SECURE=true
```

如果前后端确实跨站，三类 Cookie 都改为 `SameSite=None` 并保持 `Secure=true`，同时将前端 origin 精确加入 CORS/CSRF 配置，禁止使用通配符。

正常生产更新只使用不可变 Release 与受限 Host Update Agent。`deploy/deploy.sh` 只保留给首次安装/旧架构切换的 `--bootstrap`，或 Update Agent 无法运行时人工批准的 `--break-glass`；日常更新不得用 ZIP、`git pull` 或服务器端构建替代不可变 Release。先在服务器准备并填写唯一的 production env 文件：

```bash
cp .env.production.example .env.production
```

首次安装或旧架构切换使用显式 bootstrap 模式：

```bash
sudo sh deploy/deploy.sh \
  --bootstrap \
  --archive /tmp/animemo-core-<stamp>.zip \
  --sha256 /tmp/animemo-core-<stamp>.sha256
```

Update Agent 不可用且完成了人工审批时，才允许 break-glass：

```bash
sudo sh deploy/deploy.sh \
  --break-glass \
  --archive /tmp/animemo-core-<stamp>.zip \
  --sha256 /tmp/animemo-core-<stamp>.sha256
```

只有明确要清空本网站数据时才允许在 bootstrap 模式增加 `--reset-data --yes`；脚本只操作经校验的 AniMemo 数据根目录，绝不执行全局 `docker system prune`、`docker volume prune` 或其他 Compose 项目的 `down`。真实环境的 OpenResty 配置也只写入显式的单站点配置，可用 `--skip-openresty` 做本地或非 1Panel 验证。

迁移与 bootstrap 完成后，一次性初始化码只写入 `${ANIMEMO_DATA_ROOT}/private/setup-code`（目录 `0700`、文件 `0600`），不会出现在日志、API 或构建产物。操作者读取该文件后访问 `/setup` 创建首位管理员；成功后文件立即删除，入口由数据库状态永久锁定。完整生命周期与故障恢复见 [`首次运行引导`](docs/first-run-bootstrap.md)。

构建前端执行 `npm run build`，输出目录为 `dist/client`。生产镜像是与实例配置无关的通用 Web 工件；Turnstile 在 AniMemo Staff「安全验证」设置中按实例配置，不参与前端构建。Smoke Test 会严格检查健康接口为 HTTP 200 且 JSON `status` 为 `ok`，再检查四个容器健康状态、Host 转发、PostgreSQL/Redis 连接，以及 Local 文件 `0644`、目录 `0755`、Nginx `/local-media/` 读取和清理。

## 认证与安全部署

- access token 只保存在浏览器运行内存；refresh token 只存入 `HttpOnly` Cookie，JSON 和 Web Storage 均不返回或保存 refresh。
- 页面刷新时，前端先从 `/api/v1/auth/csrf/` 获取 Django CSRF token，再携带 Cookie 和 `X-CSRFToken` 调用刷新接口。多个并发 401 共用一次 refresh 请求。
- 未配置 `VITE_API_BASE_URL` 时，浏览器默认通过同源 `/api/v1` 访问后端；本地 Vite 开发与预览均由代理转发到 Django，避免 Cookie、CSRF 与 CSP 的跨端口分歧。
- refresh/logout 必须允许 credentials，生产前端来源必须同时精确列入 `CORS_ALLOWED_ORIGINS` 与 `CSRF_TRUSTED_ORIGINS`。不要用通配符 origin 配合 credentials。
- 同站部署使用 `SameSite=Lax`；确需跨站时使用 `SameSite=None` 且必须保持 `Secure=true` 和 HTTPS。
- 工作人员 2FA 保持可选：未启用时密码正确即可进入自定义工作人员后台；启用后，普通 JWT 与工作人员登录都必须完成 TOTP 或一次性恢复码验证后才签发凭据。Django Admin 始终要求已启用并完成 2FA。
- 登录、工作人员登录、注册申请、完成注册和密码重置入口支持可选的 Cloudflare Turnstile；配置来自 Staff「安全验证」中的 SiteSettings 数据库。关闭时不加载 Turnstile，开启后必须配置 Site Key 与加密保存的 Secret Key；后端只在 Cloudflare `siteverify` 返回 `success: true` 时继续执行原有 handler。
- 头像和站点头像默认限制 2MB，海报和专栏封面默认限制 5MB；核心图片会先完整解码、应用 EXIF 方向、去除元数据并重新编码为静态 WebP，动态图片会被拒绝。
- 工作人员自助注销账号必须输入当前密码和 TOTP/一枚恢复码；最后一个有效超级管理员受到服务端事务锁保护，成功和拒绝都会写入不含敏感凭据的审计记录。
- refresh rotation 会锁定对应 `OutstandingToken`，同一 refresh 并发请求只允许一个成功，重放会被拒绝并记录截断 JTI 哈希。
- 注册接口使用 Redis 原子 IP、邮箱和 IP+邮箱三维限流，默认分别为 `10/hour`、`3/hour`、`3/hour`，429 响应带 `Retry-After`；邮箱维度只使用 SHA-256 摘要。
- 用户名与邮箱都在数据库层执行不区分大小写的唯一约束；迁移发现历史冲突会停止并要求人工处理，不会静默合并账号。
- 密码修改、密码重置、管理员强制重置和 2FA 安全变更都会提升 `session_version`，旧 access/refresh 随即失效。
- 2FA 恢复码只在生成时显示一次，数据库只保存哈希；管理员应离线保管，不要写入浏览器存储或工单。
- Django Admin 不再提供独立密码登录入口；工作人员必须先从 `/admin-login` 完成项目登录和两步验证，Admin Session 同时绑定用户 ID、`session_version` 和验证时间。验证默认 8 小时失效，可通过 `ADMIN_2FA_SESSION_MAX_AGE` 调整。
- `DEBUG` 未配置时默认关闭；本地开发必须显式设置 `DEBUG=true`，生产环境缺少安全密钥、共享 Redis、可信代理或 `ALLOWED_HOSTS` 时会拒绝启动。
- 每次启用、重新绑定或主动重生成 2FA 时固定生成 6 枚恢复码；使用后立即原子失效。
- 普通退出会撤销当前 access token 并 blacklist 当前 refresh，不影响其他设备；“退出全部设备”才会提升 session version。
- 静态前端、Nginx 和 OpenResty 均返回 `frame-ancestors 'none'`、`X-Frame-Options: DENY`、`nosniff` 与严格 Referrer-Policy；公网单层 Nginx 会覆盖客户端传入的 XFF/XFP。
- 可定期执行 `python backend/manage.py purge_expired_revoked_tokens` 清理已过期的 access 撤销记录。
- `python backend/manage.py audit_orphan_media` 只列出本地孤立媒体文件；只有显式增加 `--delete` 才会删除。

## Python 依赖更新

生产安装使用精确锁定的 `backend/requirements.txt`。升级时先修改 `backend/requirements.in`，运行 `python scripts/update_dependencies.py` 重新生成锁定文件，并完整运行 Django 与前端测试；CI 会运行 `python scripts/update_dependencies.py --check` 防止锁文件漂移。Django 最低安全基线为 5.2.16，当前不升级到 Django 6.x。

## API 文档与维护

`/api/schema/` 提供 OpenAPI 3 schema，`/api/docs/` 提供同源 Swagger UI。文档只描述接口契约，不包含任何生产密钥、OAuth secret、凭证密文或插件 HMAC secret；refresh token 仅通过 HttpOnly Cookie 传递。错误响应契约和前端解析规则见 [`docs/api-errors.md`](docs/api-errors.md)。

已有维护命令可通过统一入口执行：

```bash
python backend/manage.py run_maintenance
python backend/manage.py run_maintenance --task purge_expired_revoked_tokens
```

每个任务都会输出 `PASS` 或 `FAIL`；任一任务失败时总命令返回非零状态，但会继续执行其他任务。该入口只包含非破坏性任务，不会启用 Celery、后台 worker 或生产部署操作。

查询计划和 cron/systemd 调度说明见 [`docs/maintenance.md`](docs/maintenance.md)。

## 验证命令

```bash
npm run test:plugins
npm run build
npm test
python backend/manage.py check
python backend/manage.py test journal
```

生产前另需：换掉示例媒体、通过 `/setup` 完成首位管理员初始化、在 Admin 中设定专栏审核状态，并将域名 DNS/HTTPS 配置完成。

## GitHub Actions CI

针对 `main` 的 Pull Request 会先按变更风险选择受影响的前端、后端、PostgreSQL、插件、Bridge 与发布门禁；数据库、认证、发布、Updater、部署和 CI 权威逻辑等高风险变更会自动扩大验证范围。合并前必须对当前 PR HEAD 和最新 `main` 显式运行一次权威 Pre-Merge Full Gate，随后 squash merge；`main` push 只做轻量合并后校验。门禁分级、触发规则与本地命令见 [`docs/release-gates.md`](docs/release-gates.md)。
