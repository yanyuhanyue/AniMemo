#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_IMAGE=""
WEB_IMAGE=""
VERSION=""
COMMIT=""
CHANNEL=""
CONFIRM_ISOLATED=false

while (($#)); do
  case "$1" in
    --api-image) API_IMAGE="${2:-}"; shift 2 ;;
    --web-image) WEB_IMAGE="${2:-}"; shift 2 ;;
    --version) VERSION="${2:-}"; shift 2 ;;
    --commit) COMMIT="${2:-}"; shift 2 ;;
    --channel) CHANNEL="${2:-}"; shift 2 ;;
    --confirm-isolated) CONFIRM_ISOLATED=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$CONFIRM_ISOLATED" != true || "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "Exact image rehearsal is restricted to an explicitly confirmed GitHub-hosted isolated runner." >&2
  exit 2
fi
if [[ -z "$API_IMAGE" || -z "$WEB_IMAGE" || -z "$VERSION" || -z "$COMMIT" || -z "$CHANNEL" ]]; then
  echo "API/Web image and release identity arguments are required." >&2
  exit 2
fi
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ || ! "$CHANNEL" =~ ^(beta|rc)$ ]]; then
  echo "Release identity arguments are invalid." >&2
  exit 2
fi
if [[ -z "${RUNNER_TEMP:-}" || "$RUNNER_TEMP" != /* ]]; then
  echo "RUNNER_TEMP must be an absolute isolated runner path." >&2
  exit 2
fi

TEMP_ROOT="$(mktemp -d "$RUNNER_TEMP/animemo-release-images.XXXXXX")"
case "$TEMP_ROOT" in
  "$RUNNER_TEMP"/*) ;;
  *) echo "Temporary rehearsal root escaped RUNNER_TEMP." >&2; exit 1 ;;
esac

PROJECT_NAME="animemo-release-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
PROJECT_NAME="$(printf '%s' "$PROJECT_NAME" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-')"
DATA_ROOT="$TEMP_ROOT/data"
META_ROOT="$TEMP_ROOT/meta"
ENV_FILE="$TEMP_ROOT/rehearsal.env"
COMPOSE=(
  docker compose
  --project-name "$PROJECT_NAME"
  --env-file "$ENV_FILE"
  -f "$ROOT/deploy/docker-compose.yml"
  -f "$ROOT/updater/docker-compose.runtime.yml"
  -f "$ROOT/deploy/docker-compose.upgrade-gate.yml"
)

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if ! rm -rf -- "$TEMP_ROOT" 2>/dev/null; then
    sudo -n rm -rf -- "$TEMP_ROOT"
  fi
}
trap cleanup EXIT

mkdir -p "$DATA_ROOT"/{plugins,logs,backups,media,postgres,redis} "$META_ROOT"
chmod -R a+rwx "$DATA_ROOT"
sudo install -d -m 0700 -o 10001 -g 10001 "$DATA_ROOT/private"

cat > "$ENV_FILE" <<EOF
DEBUG=false
DJANGO_SECRET_KEY=ci-only-release-rehearsal-key-012345678901234567890123456789012345678901
CREDENTIAL_ENCRYPTION_KEY=a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io=
POSTGRES_DB=animemo
POSTGRES_USER=animemo
POSTGRES_PASSWORD=ci-release-rehearsal-password
DATABASE_URL=postgresql://animemo:ci-release-rehearsal-password@postgres:5432/animemo
DATABASE_SSL_REQUIRE=false
REDIS_URL=redis://redis:6379/0
ALLOWED_HOSTS=release-rehearsal.example.test
ANIMEMO_PUBLIC_ORIGIN=https://release-rehearsal.example.test
CORS_ALLOWED_ORIGINS=https://release-rehearsal.example.test
CSRF_TRUSTED_ORIGINS=https://release-rehearsal.example.test
TRUSTED_PROXY_IPS=127.0.0.1/32
SECURE_SSL_REDIRECT=false
SESSION_COOKIE_SECURE=false
CSRF_COOKIE_SECURE=false
REFRESH_COOKIE_SECURE=false
ALLOW_INSECURE_PRODUCTION_COOKIES=true
ANIMEMO_PORT=18088
ANIMEMO_DATA_ROOT=$DATA_ROOT
ANIMEMO_API_IMAGE=$API_IMAGE
ANIMEMO_WEB_IMAGE=$WEB_IMAGE
ANIMEMO_RELEASE_VERSION=$VERSION
ANIMEMO_RELEASE_COMMIT=$COMMIT
ANIMEMO_RELEASE_CHANNEL=$CHANNEL
ANIMEMO_DATABASE_CONTRACT=animemo-db-v1
ANIMEMO_CONFIGURATION_CONTRACT=animemo-config-v1
STATEFUL_UPGRADE_ENV_FILE=$ENV_FILE
STATEFUL_UPGRADE_HELPER_ROOT=$ROOT
STATEFUL_UPGRADE_META_ROOT=$META_ROOT
EOF
chmod 600 "$ENV_FILE"

export ANIMEMO_DATA_ROOT="$DATA_ROOT"
export ANIMEMO_API_IMAGE="$API_IMAGE"
export ANIMEMO_WEB_IMAGE="$WEB_IMAGE"
export ANIMEMO_RELEASE_VERSION="$VERSION"
export ANIMEMO_RELEASE_COMMIT="$COMMIT"
export ANIMEMO_RELEASE_CHANNEL="$CHANNEL"
export ANIMEMO_DATABASE_CONTRACT="animemo-db-v1"
export ANIMEMO_CONFIGURATION_CONTRACT="animemo-config-v1"
export STATEFUL_UPGRADE_ENV_FILE="$ENV_FILE"
export STATEFUL_UPGRADE_HELPER_ROOT="$ROOT"
export STATEFUL_UPGRADE_META_ROOT="$META_ROOT"

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up -d --wait --wait-timeout 120 postgres redis
"${COMPOSE[@]}" run --rm --no-deps migration
"${COMPOSE[@]}" run --rm --no-deps bootstrap
"${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout 120 api web

api_container="$("${COMPOSE[@]}" ps -q api)"
web_container="$("${COMPOSE[@]}" ps -q web)"
test -n "$api_container" && test -n "$web_container"

network_name="${PROJECT_NAME}-network"
web_proxy_ip="$(docker inspect --format "{{(index .NetworkSettings.Networks \"$network_name\").IPAddress}}" "$web_container")"
trusted_proxy_cidr="$(python3 - "$web_proxy_ip" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
if address.version != 4:
    raise SystemExit("Release rehearsal Web proxy must have an IPv4 address.")
print(f"{address}/32")
PY
)"
echo "Release rehearsal trusted proxy source: $trusted_proxy_cidr"

TRUSTED_PROXY_CIDR="$trusted_proxy_cidr" python3 - "$ENV_FILE" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith("TRUSTED_PROXY_IPS=")]
if len(matches) != 1:
    raise SystemExit("Release rehearsal must define exactly one TRUSTED_PROXY_IPS value.")
lines[matches[0]] = f"TRUSTED_PROXY_IPS={os.environ['TRUSTED_PROXY_CIDR']}"
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

"${COMPOSE[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 120 api
api_container="$("${COMPOSE[@]}" ps -q api)"
test -n "$api_container"

test "$(docker inspect --format '{{.Image}}' "$api_container")" = \
  "$(docker image inspect --format '{{.Id}}' "$API_IMAGE")"
test "$(docker inspect --format '{{.Image}}' "$web_container")" = \
  "$(docker image inspect --format '{{.Id}}' "$WEB_IMAGE")"

for pair in "$api_container:$API_IMAGE" "$web_container:$WEB_IMAGE"; do
  container="${pair%%:*}"
  image="${pair#*:}"
  test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$container")" = "$VERSION"
  test "$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container")" = "$COMMIT"
  test "$(docker inspect --format '{{index .Config.Labels "cc.animemo.release.channel"}}' "$container")" = "$CHANNEL"
  test -n "$image"
done

health_payload="$(curl --fail --silent --show-error \
  -H 'Host: release-rehearsal.example.test' http://127.0.0.1:18088/health/)"
HEALTH_PAYLOAD="$health_payload" python3 - "$VERSION" "$COMMIT" "$CHANNEL" <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["HEALTH_PAYLOAD"])
assert payload["status"] == "ok"
assert payload["release"] == {
    "version": sys.argv[1],
    "commit": sys.argv[2],
    "channel": sys.argv[3],
}
assert payload["artifact"] == payload["release"]
assert payload["contracts"] == {
    "database": "animemo-db-v1",
    "configuration": "animemo-config-v1",
}
PY

page="$(curl --fail --silent --show-error \
  -H 'Host: release-rehearsal.example.test' http://127.0.0.1:18088/)"
grep -Fq '<div id="root"></div>' <<<"$page"
grep -Fq "name=\"animemo-artifact-version\" content=\"$VERSION\"" <<<"$page"
grep -Fq "name=\"animemo-artifact-commit\" content=\"$COMMIT\"" <<<"$page"
grep -Fq "name=\"animemo-artifact-channel\" content=\"$CHANNEL\"" <<<"$page"

ANIMEMO_CI_SETUP_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ANIMEMO_CI_SETUP_PASSWORD
sudo cat "$DATA_ROOT/private/setup-code" |
  python3 "$ROOT/scripts/ci_first_run.py" \
    --confirm-isolated --code-stdin \
    --base-url http://127.0.0.1:18088 \
    --host release-rehearsal.example.test \
    --username release-rehearsal-admin \
    --email release-rehearsal-admin@example.test \
    --password-env ANIMEMO_CI_SETUP_PASSWORD

recorded_setup_ip="$("${COMPOSE[@]}" exec -T api python manage.py shell -c \
  "from journal.models import AdminAuditLog; print(AdminAuditLog.objects.get(action='installation.initialized').ip_address)" | tail -n 1)"
python3 - "$web_proxy_ip" "$recorded_setup_ip" <<'PY'
import ipaddress
import sys

proxy_ip = ipaddress.ip_address(sys.argv[1])
recorded_ip = ipaddress.ip_address(sys.argv[2])
if recorded_ip == proxy_ip:
    raise SystemExit("Django did not accept the client address forwarded by the exact Web proxy.")
PY

echo "Exact release image rehearsal: PASS"
