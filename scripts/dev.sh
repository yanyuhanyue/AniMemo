#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUP_ONLY=0
if [[ "${1:-}" == "--setup-only" ]]; then SETUP_ONLY=1; fi

command -v python3 >/dev/null || { echo "未找到 Python 3.12+。" >&2; exit 1; }
command -v node >/dev/null || { echo "未找到 Node.js 20+。" >&2; exit 1; }
command -v npm >/dev/null || { echo "未找到 npm。" >&2; exit 1; }

python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(f"Python 版本必须为 3.12+，当前为 {sys.version.split()[0]}。")
PY
node -e 'const major=Number(process.versions.node.split(".")[0]); if (major < 20) { throw new Error(`Node.js 版本必须为 20+，当前为 ${process.versions.node}。`); }'

cd "$ROOT"
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if [[ ! -f .env ]]; then cp .env.development.example .env; echo "已创建 .env（仅供本地开发使用）。"; fi
.venv/bin/python -m pip install -r backend/requirements.txt
if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
mkdir -p runtime/private
chmod 0700 runtime/private
.venv/bin/python backend/manage.py migrate --noinput
.venv/bin/python backend/manage.py bootstrap_animemo
.venv/bin/python backend/manage.py check
.venv/bin/python backend/manage.py shell -c "from django.conf import settings; from django.test import Client; assert settings.DEBUG and settings.TURNSTILE_ENABLED is False; assert Client(HTTP_HOST='localhost').get('/health/').status_code == 200"
if (( SETUP_ONLY )); then exit 0; fi

cleanup() {
    trap - EXIT INT TERM
    [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
    [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

.venv/bin/python backend/manage.py runserver 127.0.0.1:8000 --noreload & BACKEND_PID=$!
npm run dev -- --host 0.0.0.0 & FRONTEND_PID=$!
echo "Django: http://127.0.0.1:8000"
echo "Vite:   http://127.0.0.1:5173"
wait "$BACKEND_PID"
