# 本地开发

AniMemo 的本地开发使用 SQLite 与 LocMemCache，仅用于开发和测试；生产环境仍必须使用 PostgreSQL、Redis 和正式的安全配置。

## 快速启动

Windows PowerShell：

```powershell
./scripts/dev.ps1
```

macOS/Linux：

```bash
./scripts/dev.sh
```

只安装依赖并执行迁移、不启动服务：

```powershell
./scripts/dev.ps1 -SetupOnly
```

```bash
./scripts/dev.sh --setup-only
```

脚本会检查 Python 3.12+、Node.js 20+ 和 npm，复用或创建 `.venv`，安装精确锁定依赖、从 `.env.development.example` 创建开发 `.env`（若不存在）、执行迁移和幂等 bootstrap，并在完整模式下同时启动 Django 与 Vite。脚本可重复执行，不会删除 SQLite 数据库、重建虚拟环境、清空 npm 缓存或预设管理员凭据。

全新数据库会在 `runtime/private/setup-code` 生成一次性初始化码。读取该文件后打开 `http://localhost:5173/setup`，填写初始化码、管理员用户名、邮箱和新密码。完成后初始化码会被删除且 `/setup` 永久锁定；详细状态机和恢复说明见 [`首次运行引导`](first-run-bootstrap.md)。

开发模板使用 SQLite + LocMemCache，Turnstile 默认在 SiteSettings 中关闭；生产部署也不通过 ENV 配置 Turnstile，而是在 Staff「安全验证」中按实例启用并保存密钥。
