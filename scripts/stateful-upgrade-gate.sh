#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_SHA=""
HEAD_SHA=""
CURRENT_ROOT="$ROOT"
KEEP_TEMP=false

COMMAND_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_COMMAND_TIMEOUT_SECONDS:-180}"
BUILD_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_BUILD_TIMEOUT_SECONDS:-900}"
JOB_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_JOB_TIMEOUT_SECONDS:-300}"
EXEC_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_EXEC_TIMEOUT_SECONDS:-120}"
HEALTH_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_HEALTH_TIMEOUT_SECONDS:-15}"
INSPECT_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_INSPECT_TIMEOUT_SECONDS:-15}"
DIAGNOSTIC_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_DIAGNOSTIC_TIMEOUT_SECONDS:-20}"
CLEANUP_TIMEOUT_SECONDS="${STATEFUL_UPGRADE_CLEANUP_TIMEOUT_SECONDS:-60}"
API_WAIT_SECONDS="${STATEFUL_UPGRADE_API_WAIT_SECONDS:-240}"
POLL_SECONDS="${STATEFUL_UPGRADE_POLL_SECONDS:-5}"
TIMEOUT_KILL_AFTER_SECONDS="${STATEFUL_UPGRADE_TIMEOUT_KILL_AFTER_SECONDS:-10}"
DIAGNOSTIC_LOG_LINES="${STATEFUL_UPGRADE_DIAGNOSTIC_LOG_LINES:-200}"

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

for command in git docker python3 timeout; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Stateful upgrade gate prerequisite is missing: $command" >&2
    exit 1
  fi
done

for value_name in \
  COMMAND_TIMEOUT_SECONDS BUILD_TIMEOUT_SECONDS JOB_TIMEOUT_SECONDS EXEC_TIMEOUT_SECONDS \
  HEALTH_TIMEOUT_SECONDS INSPECT_TIMEOUT_SECONDS DIAGNOSTIC_TIMEOUT_SECONDS CLEANUP_TIMEOUT_SECONDS \
  API_WAIT_SECONDS TIMEOUT_KILL_AFTER_SECONDS DIAGNOSTIC_LOG_LINES; do
  value="${!value_name}"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "Stateful upgrade gate timeout setting must be a positive integer: $value_name=$value" >&2
    exit 2
  fi
done
if [[ ! "$POLL_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Stateful upgrade gate poll setting must be a non-negative integer: POLL_SECONDS=$POLL_SECONDS" >&2
  exit 2
fi

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
DIAGNOSTICS_RUNNING=false

phase_marker() {
  local phase="$1"
  local status="$2"
  local exit_code="${3:-}"
  if [[ -n "$exit_code" ]]; then
    printf 'STATEFUL_UPGRADE_PHASE phase=%s status=%s exit_code=%s\n' "$phase" "$status" "$exit_code" >&2
  else
    printf 'STATEFUL_UPGRADE_PHASE phase=%s status=%s\n' "$phase" "$status" >&2
  fi
}

command_marker() {
  local command_name="$1"
  local status="$2"
  local exit_code="${3:-}"
  if [[ -n "$exit_code" ]]; then
    printf 'STATEFUL_UPGRADE_COMMAND command=%s status=%s exit_code=%s\n' "$command_name" "$status" "$exit_code" >&2
  else
    printf 'STATEFUL_UPGRADE_COMMAND command=%s status=%s\n' "$command_name" "$status" >&2
  fi
}

timeout_command() {
  local timeout_seconds="$1"
  shift
  timeout \
    --foreground \
    --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s" \
    "${timeout_seconds}s" \
    "$@"
}

is_timeout_exit() {
  local exit_code="$1"
  [[ "$exit_code" -eq 124 || "$exit_code" -eq 137 ]]
}

timed_compose_raw() {
  local source_root="$1"
  local timeout_seconds="$2"
  shift 2
  local compose_files=(-f "$source_root/deploy/docker-compose.yml" -f "$BUILD_OVERRIDE_FILE")
  compose_files+=(-f "$OVERRIDE_FILE")
  timeout_command "$timeout_seconds" \
    env \
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

diagnostic_compose() {
  local command_name="$1"
  local source_root="$2"
  shift 2
  local exit_code=0
  if timed_compose_raw "$source_root" "$DIAGNOSTIC_TIMEOUT_SECONDS" "$@"; then
    command_marker "$command_name" pass
    return 0
  else
    exit_code=$?
  fi
  if is_timeout_exit "$exit_code"; then
    command_marker "$command_name" timeout "$exit_code"
  else
    command_marker "$command_name" failed "$exit_code"
  fi
  return "$exit_code"
}

diagnostic_docker() {
  local command_name="$1"
  shift
  local exit_code=0
  if timeout_command "$DIAGNOSTIC_TIMEOUT_SECONDS" docker "$@"; then
    command_marker "$command_name" pass
    return 0
  else
    exit_code=$?
  fi
  if is_timeout_exit "$exit_code"; then
    command_marker "$command_name" timeout "$exit_code"
  else
    command_marker "$command_name" failed "$exit_code"
  fi
  return "$exit_code"
}

print_logs() {
  local source_root="${1:-$CURRENT_ROOT}"
  local reason="${2:-unknown}"
  local original_exit_code="${3:-1}"
  if [[ "$DIAGNOSTICS_RUNNING" == true ]]; then
    return 0
  fi
  DIAGNOSTICS_RUNNING=true
  printf 'STATEFUL_UPGRADE_DIAGNOSTICS status=start reason=%s exit_code=%s\n' "$reason" "$original_exit_code" >&2
  echo "--- Compose status ($PROJECT_NAME) ---" >&2
  diagnostic_compose diagnostic_ps "$source_root" ps >&2 || true
  echo "--- API container state ($PROJECT_NAME-api) ---" >&2
  diagnostic_docker diagnostic_api_inspect inspect \
    --format '{{json .State}}' "$PROJECT_NAME-api" >&2 || true
  echo "--- Compose logs ($PROJECT_NAME) ---" >&2
  diagnostic_compose diagnostic_logs "$source_root" logs --no-color --tail "$DIAGNOSTIC_LOG_LINES" >&2 || true
  printf 'STATEFUL_UPGRADE_DIAGNOSTICS status=complete reason=%s exit_code=%s\n' "$reason" "$original_exit_code" >&2
  DIAGNOSTICS_RUNNING=false
}

run_compose() {
  local phase="$1"
  local source_root="$2"
  local timeout_seconds="$3"
  shift 3
  local exit_code=0
  phase_marker "$phase" start
  if timed_compose_raw "$source_root" "$timeout_seconds" "$@"; then
    command_marker "$phase" pass
    phase_marker "$phase" pass
    return 0
  else
    exit_code=$?
  fi
  if is_timeout_exit "$exit_code"; then
    command_marker "$phase" timeout "$exit_code"
    phase_marker "$phase" timeout "$exit_code"
  else
    command_marker "$phase" failed "$exit_code"
    phase_marker "$phase" failed "$exit_code"
  fi
  print_logs "$source_root" "$phase" "$exit_code"
  return "$exit_code"
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
  timed_compose_raw "$CURRENT_ROOT" "$CLEANUP_TIMEOUT_SECONDS" down --remove-orphans
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

health_check() {
  local phase="$1"
  local exit_code=0
  if timeout_command "$HEALTH_TIMEOUT_SECONDS" docker exec -i "$PROJECT_NAME-api" python - <<'PY'
import http.client
import json

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
connection.request("GET", "/health/", headers={"Host": "ci.example.test", "X-Forwarded-Proto": "https"})
response = connection.getresponse()
payload = json.loads(response.read())
assert response.status == 200 and payload.get("status") == "ok", (response.status, payload)
print("HTTP /health/: PASS")
PY
  then
    command_marker "${phase}_probe" pass
    return 0
  else
    exit_code=$?
  fi
  if is_timeout_exit "$exit_code"; then
    command_marker "${phase}_probe" timeout "$exit_code"
  else
    command_marker "${phase}_probe" failed "$exit_code"
  fi
  return "$exit_code"
}

inspect_api_running() {
  local phase="$1"
  local inspection=""
  local exit_code=0
  local state_status=""
  local state_running=""
  local state_restarting=""
  if inspection="$(timeout_command "$INSPECT_TIMEOUT_SECONDS" docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.Restarting}}' "$PROJECT_NAME-api" 2>/dev/null)"; then
    read -r state_status state_running state_restarting <<<"$inspection"
    if [[ "$state_restarting" == "true" || "$state_status" == "restarting" ]]; then
      command_marker "${phase}_inspect" restarting
      return 0
    fi
    if [[ "$state_running" == "true" ]]; then
      command_marker "${phase}_inspect" pass
      return 0
    fi
    command_marker "${phase}_inspect" failed 1
    return 1
  else
    exit_code=$?
  fi
  if is_timeout_exit "$exit_code"; then
    command_marker "${phase}_inspect" timeout "$exit_code"
  else
    command_marker "${phase}_inspect" failed "$exit_code"
  fi
  return "$exit_code"
}

wait_for_api() {
  local source_root="$1"
  local phase="$2"
  local label="$3"
  local deadline=$((SECONDS + API_WAIT_SECONDS))
  local health_output=""
  local health_exit_code=0
  local inspect_exit_code=0
  phase_marker "$phase" start
  while ((SECONDS < deadline)); do
    if health_check "$phase" >/dev/null; then
      if health_output="$(health_check "$phase")"; then
        printf '%s\n' "$health_output"
        phase_marker "$phase" pass
        return 0
      else
        health_exit_code=$?
      fi
    else
      health_exit_code=$?
    fi
    if inspect_api_running "$phase"; then
      sleep "$POLL_SECONDS"
      continue
    else
      inspect_exit_code=$?
    fi
    if is_timeout_exit "$inspect_exit_code"; then
      phase_marker "$phase" timeout "$inspect_exit_code"
    else
      phase_marker "$phase" failed "$inspect_exit_code"
    fi
    echo "$label release API is not running." >&2
    print_logs "$source_root" "${phase}_inspect" "$inspect_exit_code"
    return "$inspect_exit_code"
  done
  phase_marker "$phase" timeout 124
  echo "$label release API did not become healthy within $API_WAIT_SECONDS seconds (last probe exit $health_exit_code)." >&2
  print_logs "$source_root" "$phase" 124
  return 124
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
FRONTEND_URL=https://ci.example.test
CORS_ALLOWED_ORIGINS=https://ci.example.test
CSRF_TRUSTED_ORIGINS=https://ci.example.test
TRUSTED_PROXY_IPS=127.0.0.1/32
# Audited historical BASE revisions required these legacy inputs to validate
# and build their Web image. They are test-only compatibility fixtures; current
# production remains SiteSettings database-only. The Site Key is a public test
# fixture, not a secret.
TURNSTILE_ENABLED=false
VITE_TURNSTILE_SITE_KEY=1x00000000000000000000AA
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
run_compose base_config "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" config --quiet

echo "== Build Base API and boot persistent services =="
run_compose base_build "$BASE_ROOT" "$BUILD_TIMEOUT_SECONDS" build api
run_compose base_services_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" up -d --wait --wait-timeout 120 postgres redis
run_compose base_migration "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS" run --rm --no-deps migration
run_compose base_bootstrap "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS" \
  run --rm --no-deps bootstrap \
  sh -eu -c 'python manage.py sync_official_plugins && exec python manage.py collectstatic --noinput'
run_compose base_api_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" up -d --no-deps api
wait_for_api "$BASE_ROOT" base_api_health BASELINE

echo "== Seed representative persistent Base state =="
run_compose base_state_seed "$BASE_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py seed --output /app/ci-meta/base-state.json
run_compose base_migration_check "$BASE_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python manage.py migrate --check
run_compose base_state_verify "$BASE_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json
BASE_POSTGRES_ID="$(run_compose base_postgres_identity "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" ps -q postgres)"
BASE_REDIS_ID="$(run_compose base_redis_identity "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" ps -q redis)"
[[ -n "$BASE_POSTGRES_ID" && -n "$BASE_REDIS_ID" ]] || { echo "Persistent service identity is unavailable." >&2; exit 1; }

echo "== Build Current API without touching persistent services =="
run_compose current_config "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" config --quiet
run_compose current_build "$CURRENT_ROOT" "$BUILD_TIMEOUT_SECONDS" build api

echo "== Run explicit Current migration and bootstrap jobs =="
run_compose current_migration "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" run --rm --no-deps migration
run_compose current_bootstrap "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" run --rm --no-deps bootstrap

echo "== Replace only the API container with Current =="
run_compose current_api_replace "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" up -d --no-deps --force-recreate api
wait_for_api "$CURRENT_ROOT" current_api_health CURRENT
CURRENT_POSTGRES_ID="$(run_compose current_postgres_identity "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" ps -q postgres)"
CURRENT_REDIS_ID="$(run_compose current_redis_identity "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" ps -q redis)"
[[ "$CURRENT_POSTGRES_ID" == "$BASE_POSTGRES_ID" ]] || { echo "PostgreSQL container was unexpectedly replaced." >&2; exit 1; }
[[ "$CURRENT_REDIS_ID" == "$BASE_REDIS_ID" ]] || { echo "Redis container was unexpectedly replaced." >&2; exit 1; }
echo "PostgreSQL and Redis containers were retained"
run_compose current_migration_check "$CURRENT_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python manage.py migrate --check
run_compose current_migration_plan "$CURRENT_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python manage.py showmigrations --plan
run_compose current_state_verify "$CURRENT_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json

echo "== Restart Current API once and verify recovery =="
run_compose current_restart "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" restart api
wait_for_api "$CURRENT_ROOT" current_restart_health "CURRENT RESTART"
run_compose current_restart_state_verify "$CURRENT_ROOT" "$EXEC_TIMEOUT_SECONDS" exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/base-state.json

phase_marker gate pass
echo "Stateful production upgrade gate: PASS"
