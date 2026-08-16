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

## v1.1 Durable Deployment Contracts

v1.1 先冻结 provider-neutral 的部署边界，再实现安装与维护工具。canonical Contract 为：

- [`Deployment Boundary v1`](docs/deployment-boundary-v1.md)
- [`Standard Filesystem Layout v1`](docs/filesystem-layout-v1.md)
- [`Installer Contract v1`](docs/installer-contract-v1.md)
- [`Public Origin / Listen Contract v1`](docs/public-origin-listen-contract-v1.md)
- [`Compatibility Matrix v1`](docs/compatibility-matrix-v1.md)
- [`Backup Contract v1`](docs/backup-contract-v1.md)
- [`Migration Secret Envelope v1`](docs/migration-secret-envelope-v1.md)
- [`Restore Contract v1`](docs/restore-contract-v1.md)
- [`Migration Bundle v1`](docs/migration-bundle-v1.md)
- [`Doctor Basic Contract v1`](docs/doctor-basic-contract-v1.md)

版本路线为 `v1.0.0 Stable → v1.1 development → v1.1 RC → v1.1.0 Stable`。原计划的 v1.0.1 Stability/UI Patch 已取消，当前 main 上的品牌与小型修复累计进入 v1.1；生产继续保持 v1.0.0。本阶段不创建 tag、Release、OCI，不部署生产。

v1.1 新安装只使用 `/opt/animemo`、`/data/animemo`、`/opt/animemo-updater`、`/var/lib/animemo-updater` 与 `/run/animemo-updater`，默认监听 `127.0.0.1:8088`。Phase 3C 已实现 Fresh Install、Restore-to-New、canonical Updater adoption、托管配置与平台资格验证；Migration Runtime 仍独立拥有迁移包消费和激活授权。DNS、TLS、公网反向代理、firewall 与 hosting panel 由管理员负责，不是 AniMemo 安装成功条件。

## 生产部署

v1.1 的部署 authority 是经过验证的 GitHub Release、Manifest/checksums/attestation 与不可变 OCI digest。Installer 只消费 Release 中逐字节绑定的材料；不同版本的现有实例必须交给 Updater，foreign、partial 或无有效 locator 的目标默认拒绝。

实例配置的唯一 authority 是 `/data/animemo/config/animemo.json`。Updater 从它生成可重建的 `/run/animemo-updater/managed.env` 供 exact Compose 消费；该 env 文件不是配置 authority，也不得人工维护。查看或修改非秘密配置使用：

```bash
animemo-updater config show
animemo-updater config validate --public-origin https://anime.example
animemo-updater config dry-run --listen 127.0.0.2:8088
animemo-updater config apply --listen 127.0.0.2:8088 --accept
```

Public Origin 与 Listen 是独立字段。非 loopback 监听和 HTTP Public Origin 都需要独立显式确认；配置 apply 会执行原子更新、只协调 AniMemo API/Web、验证 health、exact Release、locator 与 Doctor，失败时回滚或留下 `RECOVERY_REQUIRED` 证据。

Fresh Installer 生成并保护必需 secret，但不创建管理员。成功后操作者访问同源 `/setup`，使用私有一次性初始化生命周期创建首位管理员。Restore-to-New 必须显式选择无保护、受保护 key 文件、passphrase 文件或 FD/stdin 获取方式，并保留源 `instanceId`、CEK、用户/资源/Memory identity；secret 不进入 argv、日志、plan 或结果。

媒体后端由 Superuser 在“媒体存储”页面配置，可按优先级使用多个 Cloudflare R2 与固定根目录下的 Local Server Storage。未配置时网站仍可启动，但媒体上传返回 `MEDIA_STORAGE_SETUP_REQUIRED`。完整主机运维边界与命令见 [`生产部署`](docs/deployment-vps.md)。

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
