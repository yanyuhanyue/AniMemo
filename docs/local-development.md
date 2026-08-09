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

脚本会检查 Python 3.12+、Node.js 20+ 和 npm，复用或创建 `.venv`，安装精确锁定依赖、创建开发 `.env`（若不存在）、执行迁移，并在完整模式下同时启动 Django 与 Vite。脚本可重复执行，不会删除 SQLite 数据库、重建虚拟环境或清空 npm 缓存，也不会自动创建管理员账号。

首次需要管理员时，请在另一个终端运行 `./.venv/bin/python backend/manage.py createsuperuser`（Windows 使用 `./.venv/Scripts/python.exe`）。
