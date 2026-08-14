[CmdletBinding()]
param(
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

function Assert-Command($name, $label) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "未找到 $label（$name）。请先安装后再运行此脚本。"
    }
}

function Assert-LastExitCode($label) {
    if ($LASTEXITCODE -ne 0) {
        throw "$label 失败，退出码为 $LASTEXITCODE。"
    }
}

function Stop-ProcessTree($ProcessId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree $child.ProcessId
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

Assert-Command "python" "Python 3.12+"
Assert-Command "node" "Node.js 20+"
Assert-Command "npm.cmd" "npm"

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Assert-LastExitCode "读取 Python 版本"
if ([version]$pythonVersion -lt [version]"3.12") { throw "Python 版本必须为 3.12+，当前为 $pythonVersion。" }
$nodeVersion = (& node --version).TrimStart("v")
Assert-LastExitCode "读取 Node.js 版本"
if ([version]$nodeVersion -lt [version]"20.0") { throw "Node.js 版本必须为 20+，当前为 $nodeVersion。" }

if (-not (Test-Path $venvPython)) {
    Write-Host "创建本地开发虚拟环境 .venv..."
    & python -m venv $venv
    Assert-LastExitCode "创建虚拟环境"
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Copy-Item (Join-Path $root ".env.development.example") (Join-Path $root ".env")
    Write-Host "已创建 .env（仅供本地开发使用）。"
}

Write-Host "安装后端依赖..."
& $venvPython -m pip install -r (Join-Path $root "backend\requirements.txt")
Assert-LastExitCode "安装后端依赖"
Write-Host "安装前端依赖..."
Push-Location $root
try {
    if (Test-Path "package-lock.json") { & npm ci } else { & npm install }
    Assert-LastExitCode "安装前端依赖"
    Write-Host "执行数据库迁移..."
    $privateState = Join-Path $root "runtime\private"
    New-Item -ItemType Directory -Force -Path $privateState | Out-Null
    & $venvPython backend/manage.py migrate --noinput
    Assert-LastExitCode "执行数据库迁移"
    & $venvPython backend/manage.py bootstrap_animemo
    Assert-LastExitCode "初始化应用默认数据"
    Write-Host "验证本地 Django 配置与健康检查..."
    & $venvPython backend/manage.py check
    Assert-LastExitCode "Django 配置检查"
    & $venvPython backend/manage.py shell -c "from site_config.models import SiteSettings; from django.conf import settings; from django.test import Client; assert settings.DEBUG and SiteSettings.load().turnstile_enabled is False; assert Client(HTTP_HOST='localhost').get('/health/').status_code == 200"
    Assert-LastExitCode "Django 健康检查"
    if ($SetupOnly) { return }

    $backend = Start-Process -FilePath $venvPython -ArgumentList @("backend/manage.py", "runserver", "127.0.0.1:8000", "--noreload") -WorkingDirectory $root -PassThru
    $frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "0.0.0.0") -WorkingDirectory $root -PassThru
    Write-Host "Django: http://127.0.0.1:8000"
    Write-Host "Vite:   http://127.0.0.1:5173"
    try {
        Wait-Process -Id $backend.Id
    } finally {
        foreach ($process in @($backend, $frontend)) {
            if ($process -and -not $process.HasExited) {
                Stop-ProcessTree $process.Id
            }
        }
    }
} finally {
    Pop-Location
}
