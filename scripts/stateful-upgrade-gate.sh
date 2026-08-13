#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_SHA=""
HEAD_SHA=""
CURRENT_ROOT="$ROOT"
KEEP_TEMP=false

usage() {
  cat <<'EOF'
Usage: scripts/stateful-upgrade-gate.sh --base SHA --head SHA [--current-root PATH] [--keep-temp]

Runs BASE -> CURRENT against one isolated Compose project while preserving the
PostgreSQL, Redis and plugin CAS/runtime bind-mounted data roots.
EOF
}

while (($#)); do
  case "$1" in
    --base)
      BASE_SHA="${2:-}"
      shift 2
      ;;
    --head)
      HEAD_SHA="${2:-}"
      shift 2
      ;;
    --current-root)
      CURRENT_ROOT="${2:-}"
      shift 2
      ;;
    --keep-temp)
      KEEP_TEMP=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BASE_SHA" || -z "$HEAD_SHA" ]]; then
  echo "Both --base and --head are required." >&2
  exit 2
fi

for command in git docker python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Stateful upgrade gate prerequisite is missing: $command" >&2
    exit 1
  fi
done

BASE_SHA="$(git -C "$ROOT" rev-parse --verify "$BASE_SHA^{commit}")"
HEAD_SHA="$(git -C "$ROOT" rev-parse --verify "$HEAD_SHA^{commit}")"
if [[ "$BASE_SHA" == "$HEAD_SHA" ]]; then
  echo "Stateful upgrade gate requires distinct BASE and HEAD commits." >&2
  exit 1
fi
if ! git -C "$ROOT" merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA"; then
  echo "Stateful upgrade BASE must be an ancestor of HEAD." >&2
  exit 1
fi
CURRENT_HEAD="$(git -C "$CURRENT_ROOT" rev-parse --verify HEAD^{commit})"
if [[ "$CURRENT_HEAD" != "$HEAD_SHA" ]]; then
  echo "Current worktree HEAD ($CURRENT_HEAD) does not match resolved head ($HEAD_SHA)." >&2
  exit 1
fi

RUN_KEY="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${GITHUB_JOB:-stateful-upgrade}"
RUN_KEY="$(printf '%s' "$RUN_KEY" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-')"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-animemo-upgrade-${RUN_KEY}}"
TEMP_PARENT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
if [[ -n "${STATEFUL_UPGRADE_TEMP_ROOT:-}" ]]; then
  TEMP_ROOT="$STATEFUL_UPGRADE_TEMP_ROOT"
  case "$TEMP_ROOT" in
    "$TEMP_PARENT"/*) ;;
    *)
      echo "STATEFUL_UPGRADE_TEMP_ROOT must stay under $TEMP_PARENT." >&2
      exit 1
      ;;
  esac
  if [[ -e "$TEMP_ROOT" ]]; then
    echo "Stateful upgrade temp root already exists: $TEMP_ROOT" >&2
    exit 1
  fi
  mkdir -p "$TEMP_ROOT"
else
  TEMP_ROOT="$(mktemp -d "$TEMP_PARENT/animemo-upgrade.XXXXXX")"
fi
BASE_ROOT="$TEMP_ROOT/base"
DATA_ROOT="$TEMP_ROOT/data"
META_ROOT="$TEMP_ROOT/meta"
ENV_FILE="$TEMP_ROOT/upgrade.env"
OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.upgrade-gate.yml"
BUILD_OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.build.yml"
BASE_ADDED=false

compose() {
  local source_root="$1"
  shift
  local compose_files=(-f "$source_root/deploy/docker-compose.yml" -f "$BUILD_OVERRIDE_FILE")
  compose_files+=(-f "$OVERRIDE_FILE")
  UPGRADE_SOURCE_ROOT="$source_root" \
  STATEFUL_UPGRADE_HELPER_ROOT="$CURRENT_ROOT" \
  STATEFUL_UPGRADE_META_ROOT="$META_ROOT" \
  STATEFUL_UPGRADE_ENV_FILE="$ENV_FILE" \
  ANIMEMO_DATA_ROOT="$DATA_ROOT" \
  COMPOSE_PROJECT_NAME="$PROJECT_NAME" \
    docker compose \
      --project-name "$PROJECT_NAME" \
      --env-file "$ENV_FILE" \
      "${compose_files[@]}" \
      "$@"
}

print_logs() {
  local source_root="${1:-$CURRENT_ROOT}"
  echo "--- Compose status ($PROJECT_NAME) ---" >&2
  compose "$source_root" ps >&2 || true
  echo "--- Compose logs ($PROJECT_NAME) ---" >&2
  compose "$source_root" logs --no-color >&2 || true
}

remove_temp_root() {
  [[ -e "$TEMP_ROOT" ]] || return 0
  if rm -rf -- "$TEMP_ROOT" 2>/dev/null; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n rm -rf -- "$TEMP_ROOT"
    return
  fi
  echo "Unable to remove container-owned temp data: $TEMP_ROOT" >&2
  return 1
}

cleanup() {
  local exit_code=$?
  set +e
  compose "$CURRENT_ROOT" down --remove-orphans
  if [[ "$BASE_ADDED" == true ]]; then
    git -C "$ROOT" worktree remove --force "$BASE_ROOT"
    git -C "$ROOT" worktree prune
  fi
  if [[ "$KEEP_TEMP" == true ]]; then
    echo "Stateful upgrade temp root retained: $TEMP_ROOT"
  else
    remove_temp_root
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'print_logs "$CURRENT_ROOT"' ERR

health_check() {
  local source_root="$1"
  compose "$source_root" exec -T api python - <<'PY'
import http.client
import json

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
connection.request("GET", "/health/", headers={"Host": "ci.example.test", "X-Forwarded-Proto": "https"})
response = connection.getresponse()
payload = json.loads(response.read())
assert response.status == 200 and payload.get("status") == "ok", (response.status, payload)
print("HTTP /health/: PASS")
PY
}

wait_for_api() {
  local source_root="$1"
  local phase="$2"
  for attempt in $(seq 1 36); do
    if health_check "$source_root" >/dev/null 2>&1; then
      health_check "$source_root"
      return 0
    fi
    if ! compose "$source_root" ps --status running api | grep -q "${PROJECT_NAME}-api"; then
      echo "$phase release API is not running." >&2
      print_logs "$source_root"
      return 1
    fi
    sleep 5
  done
  echo "$phase release API did not become healthy within 180 seconds." >&2
  print_logs "$source_root"
  return 1
}

mkdir -p "$DATA_ROOT"/{plugins,logs,backups,media,postgres,redis} "$META_ROOT"
chmod -R a+rwx "$DATA_ROOT" "$META_ROOT"
sudo install -d -m 0700 -o 10001 -g 10001 "$DATA_ROOT/private"
cat >"$ENV_FILE" <<EOF
DEBUG=false
DJANGO_SECRET_KEY=ci-only-secret-key-012345678901234567890123456789012345678901234567890
CREDENTIAL_ENCRYPTION_KEY=a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io=
POSTGRES_DB=animemo
POSTGRES_USER=animemo
POSTGRES_PASSWORD=ci-password
DATABASE_URL=postgresql://animemo:ci-password@postgres:5432/animemo
DATABASE_SSL_REQUIRE=false
REDIS_URL=redis://redis:6379/0
ALLOWED_HOSTS=ci.example.test
ANIMEMO_PUBLIC_ORIGIN=https://ci.example.test
CORS_ALLOWED_ORIGINS=https://ci.example.test
CSRF_TRUSTED_ORIGINS=https://ci.example.test
TRUSTED_PROXY_IPS=127.0.0.1/32
TURNSTILE_ENABLED=false
VITE_TURNSTILE_SITE_KEY=ci-site-key
SECURE_SSL_REDIRECT=false
ALLOW_INSECURE_PRODUCTION_COOKIES=true
PLUGIN_MIN_FREE_DISK_MB=0
ANIMEMO_DATA_ROOT=$DATA_ROOT
STATEFUL_UPGRADE_ENV_FILE=$ENV_FILE
STATEFUL_UPGRADE_META_ROOT=$META_ROOT
STATEFUL_UPGRADE_HELPER_ROOT=$CURRENT_ROOT
UPGRADE_SOURCE_ROOT=$CURRENT_ROOT
COMPOSE_PROJECT_NAME=$PROJECT_NAME
ANIMEMO_API_IMAGE=$PROJECT_NAME-api:current
ANIMEMO_WEB_IMAGE=$PROJECT_NAME-web:current
EOF

echo "Upgrade Base SHA: $BASE_SHA"
echo "Upgrade Head SHA: $HEAD_SHA"
echo "Compose project: $PROJECT_NAME"
echo "Ephemeral data root: $DATA_ROOT"

git -C "$ROOT" worktree add --detach "$BASE_ROOT" "$BASE_SHA"
BASE_ADDED=true

echo "== Validate isolated Base Compose configuration =="
compose "$BASE_ROOT" config --quiet

echo "== Build Base API and boot persistent services =="
compose "$BASE_ROOT" build api
compose "$BASE_ROOT" up -d --wait --wait-timeout 120 postgres redis
compose "$BASE_ROOT" run --rm --no-deps migration
compose "$BASE_ROOT" run --rm --no-deps bootstrap
compose "$BASE_ROOT" up -d --no-deps api
if ! wait_for_api "$BASE_ROOT" "BASELINE"; then
  echo "BASELINE RELEASE CANNOT BOOT" >&2
  exit 1
fi

echo "== Seed representative persistent Base state =="
compose "$BASE_ROOT" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py seed --output /app/ci-meta/base-state.json
compose "$BASE_ROOT" exec -T api python manage.py migrate --check
compose "$BASE_ROOT" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json
BASE_POSTGRES_ID="$(compose "$BASE_ROOT" ps -q postgres)"
BASE_REDIS_ID="$(compose "$BASE_ROOT" ps -q redis)"
[[ -n "$BASE_POSTGRES_ID" && -n "$BASE_REDIS_ID" ]] || { echo "Persistent service identity is unavailable." >&2; exit 1; }

echo "== Build Current API without touching persistent services =="
compose "$CURRENT_ROOT" config --quiet
compose "$CURRENT_ROOT" build api

echo "== Run explicit Current migration and bootstrap jobs =="
compose "$CURRENT_ROOT" run --rm --no-deps migration
compose "$CURRENT_ROOT" run --rm --no-deps bootstrap

echo "== Replace only the API container with Current =="
compose "$CURRENT_ROOT" up -d --no-deps --force-recreate api
wait_for_api "$CURRENT_ROOT" "CURRENT"
[[ "$(compose "$CURRENT_ROOT" ps -q postgres)" == "$BASE_POSTGRES_ID" ]] || { echo "PostgreSQL container was unexpectedly replaced." >&2; exit 1; }
[[ "$(compose "$CURRENT_ROOT" ps -q redis)" == "$BASE_REDIS_ID" ]] || { echo "Redis container was unexpectedly replaced." >&2; exit 1; }
echo "PostgreSQL and Redis containers were retained"
compose "$CURRENT_ROOT" exec -T api python manage.py migrate --check
compose "$CURRENT_ROOT" exec -T api python manage.py showmigrations --plan
compose "$CURRENT_ROOT" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json

echo "== Restart Current API once and verify recovery =="
compose "$CURRENT_ROOT" restart api
wait_for_api "$CURRENT_ROOT" "CURRENT RESTART"
compose "$CURRENT_ROOT" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json

echo "Stateful production upgrade gate: PASS"
