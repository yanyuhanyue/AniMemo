#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_ROOT="$ROOT"
CANDIDATE_SHA=""
KEEP_TEMP=false

usage() {
  cat <<'EOF'
Usage: scripts/dr-rehearsal.sh --candidate-sha SHA [--current-root PATH] [--keep-temp]

Runs a destructive-to-the-fixture-only A-to-B disaster-recovery rehearsal.
It creates a real PostgreSQL gzip dump, backs up all authoritative filesystem
members, destroys isolated instance A, and restores into fresh instance B.
EOF
}

while (($#)); do
  case "$1" in
    --candidate-sha)
      CANDIDATE_SHA="${2:-}"
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

if [[ -z "$CANDIDATE_SHA" ]]; then
  echo "--candidate-sha is required." >&2
  exit 2
fi

for command in docker git gzip python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "DR rehearsal prerequisite is missing: $command" >&2
    exit 1
  fi
done
if [[ "$(id -u)" != 0 ]] && ! sudo -n true 2>/dev/null; then
  echo "DR rehearsal requires passwordless sudo for container-owned private data." >&2
  exit 1
fi

as_root() {
  if [[ "$(id -u)" == 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

CANDIDATE_SHA="$(git -C "$ROOT" rev-parse --verify "$CANDIDATE_SHA^{commit}")"
CURRENT_HEAD="$(git -C "$CURRENT_ROOT" rev-parse --verify HEAD^{commit})"
if [[ "$CURRENT_HEAD" != "$CANDIDATE_SHA" ]]; then
  echo "Current worktree HEAD ($CURRENT_HEAD) does not match candidate ($CANDIDATE_SHA)." >&2
  exit 1
fi

RUN_KEY="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${GITHUB_JOB:-dr}"
RUN_KEY="$(printf '%s' "$RUN_KEY" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-' | cut -c1-36)"
PROJECT_PREFIX="${COMPOSE_PROJECT_NAME:-animemo-dr-${RUN_KEY}}"
PROJECT_A="${PROJECT_PREFIX}-a"
PROJECT_B="${PROJECT_PREFIX}-b"
IMAGE_NAME="${PROJECT_PREFIX}-api:${CANDIDATE_SHA:0:12}"
UPDATER_FIXTURE_UID=20001
UPDATER_GID="$(awk '$1 == "g" && $2 == "animemo-api" { print $3 }' \
  "$CURRENT_ROOT/deploy/updater/animemo-updater.sysusers.conf")"
if [[ ! "$UPDATER_GID" =~ ^[1-9][0-9]*$ ]]; then
  echo "Unable to resolve the animemo-api service group from sysusers configuration." >&2
  exit 1
fi
grep -Fxq 'User=animemo-updater' "$CURRENT_ROOT/deploy/updater/animemo-updater.service"
grep -Fxq 'Group=animemo-api' "$CURRENT_ROOT/deploy/updater/animemo-updater.service"
grep -Fxq 'd /var/lib/animemo-updater 0700 animemo-updater animemo-api -' \
  "$CURRENT_ROOT/deploy/updater/animemo-updater.tmpfiles.conf"
TEMP_PARENT="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" canonical-directory \
  --path "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")"
if [[ -n "${DR_REHEARSAL_TEMP_ROOT:-}" ]]; then
  TEMP_ROOT="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" prepare-temp-root \
    --parent "$TEMP_PARENT" \
    --requested "$DR_REHEARSAL_TEMP_ROOT")"
else
  TEMP_ROOT="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" prepare-temp-root \
    --parent "$TEMP_PARENT")"
fi

DATA_A="$TEMP_ROOT/instance-a"
DATA_B="$TEMP_ROOT/instance-b"
META_ROOT="$TEMP_ROOT/meta"
BACKUP_SET="$TEMP_ROOT/backup-set"
DUMP_PATH="$TEMP_ROOT/database.sql.gz"
ENV_A="$TEMP_ROOT/instance-a.env"
ENV_B="$TEMP_ROOT/instance-b.env"
OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.upgrade-gate.yml"
BUILD_OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.build.yml"

phase_project() {
  case "$1" in
    a) printf '%s\n' "$PROJECT_A" ;;
    b) printf '%s\n' "$PROJECT_B" ;;
    *) echo "Unknown DR phase: $1" >&2; return 2 ;;
  esac
}

phase_data_root() {
  case "$1" in
    a) printf '%s\n' "$DATA_A" ;;
    b) printf '%s\n' "$DATA_B" ;;
    *) echo "Unknown DR phase: $1" >&2; return 2 ;;
  esac
}

phase_env_file() {
  case "$1" in
    a) printf '%s\n' "$ENV_A" ;;
    b) printf '%s\n' "$ENV_B" ;;
    *) echo "Unknown DR phase: $1" >&2; return 2 ;;
  esac
}

compose() {
  local phase="$1"
  shift
  local project data_root env_file
  project="$(phase_project "$phase")"
  data_root="$(phase_data_root "$phase")"
  env_file="$(phase_env_file "$phase")"
  ANIMEMO_DATA_ROOT="$data_root" \
  COMPOSE_PROJECT_NAME="$project" \
  STATEFUL_UPGRADE_ENV_FILE="$env_file" \
  STATEFUL_UPGRADE_HELPER_ROOT="$CURRENT_ROOT" \
  STATEFUL_UPGRADE_META_ROOT="$META_ROOT" \
    docker compose \
      --project-name "$project" \
      --env-file "$env_file" \
      -f "$CURRENT_ROOT/deploy/docker-compose.yml" \
      -f "$BUILD_OVERRIDE_FILE" \
      -f "$OVERRIDE_FILE" \
      "$@"
}

write_env() {
  local phase="$1"
  local project data_root env_file
  project="$(phase_project "$phase")"
  data_root="$(phase_data_root "$phase")"
  env_file="$(phase_env_file "$phase")"
  cat >"$env_file" <<EOF
DEBUG=false
DJANGO_SECRET_KEY=dr-only-secret-key-012345678901234567890123456789012345678901234567890
CREDENTIAL_ENCRYPTION_KEY=a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io=
POSTGRES_DB=animemo
POSTGRES_USER=animemo
POSTGRES_PASSWORD=dr-only-password
DATABASE_URL=postgresql://animemo:dr-only-password@postgres:5432/animemo
DATABASE_SSL_REQUIRE=false
REDIS_URL=redis://redis:6379/0
ALLOWED_HOSTS=dr.example.test
ANIMEMO_PUBLIC_ORIGIN=https://dr.example.test
ANIMEMO_MEDIA_PUBLIC_ORIGIN=https://media.dr.example.test
FRONTEND_URL=https://dr.example.test
CORS_ALLOWED_ORIGINS=https://dr.example.test
CSRF_TRUSTED_ORIGINS=https://dr.example.test
TRUSTED_PROXY_IPS=127.0.0.1/32
TURNSTILE_ENABLED=false
VITE_TURNSTILE_SITE_KEY=dr-site-key
SECURE_SSL_REDIRECT=false
ALLOW_INSECURE_PRODUCTION_COOKIES=true
PLUGIN_MIN_FREE_DISK_MB=0
MEDIA_LOCAL_STORAGE_ROOT=/data/animemo/media
ANIMEMO_DATA_ROOT=$data_root
STATEFUL_UPGRADE_ENV_FILE=$env_file
STATEFUL_UPGRADE_META_ROOT=$META_ROOT
STATEFUL_UPGRADE_HELPER_ROOT=$CURRENT_ROOT
COMPOSE_PROJECT_NAME=$project
ANIMEMO_API_IMAGE=$IMAGE_NAME
ANIMEMO_WEB_IMAGE=${PROJECT_PREFIX}-web:${CANDIDATE_SHA:0:12}
EOF
  chmod 600 "$env_file"
}

print_logs() {
  local phase
  for phase in a b; do
    echo "--- DR instance ${phase^^} status ($(phase_project "$phase")) ---" >&2
    compose "$phase" ps >&2 || true
    echo "--- DR instance ${phase^^} logs ($(phase_project "$phase")) ---" >&2
    compose "$phase" logs --no-color >&2 || true
  done
}

remove_temp_root() {
  [[ -e "$TEMP_ROOT" ]] || return 0
  local safe_root
  safe_root="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" validate-delete \
    --parent "$TEMP_PARENT" \
    --target "$TEMP_ROOT")" || return 1
  if rm -rf -- "$safe_root" 2>/dev/null; then
    return 0
  fi
  safe_root="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" validate-delete \
    --parent "$TEMP_PARENT" \
    --target "$TEMP_ROOT")" || return 1
  as_root rm -rf -- "$safe_root"
}

remove_instance_root() {
  local target="$1"
  local safe_target
  safe_target="$(python3 "$CURRENT_ROOT/scripts/dr_recovery_paths.py" validate-delete \
    --parent "$TEMP_ROOT" \
    --target "$target")"
  as_root rm -rf -- "$safe_target"
}

cleanup() {
  local exit_code=$?
  set +e
  compose b down --volumes --remove-orphans
  compose a down --volumes --remove-orphans
  if [[ "$KEEP_TEMP" == true ]]; then
    echo "DR rehearsal temp root retained: $TEMP_ROOT"
  else
    remove_temp_root
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap print_logs ERR

health_check() {
  local phase="$1"
  compose "$phase" exec -T api python - <<'PY'
import http.client
import json

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
connection.request("GET", "/health/", headers={"Host": "dr.example.test", "X-Forwarded-Proto": "https"})
response = connection.getresponse()
payload = json.loads(response.read())
assert response.status == 200 and payload.get("status") == "ok", (response.status, payload)
print("HTTP /health/: PASS")
PY
}

wait_for_api() {
  local phase="$1"
  for attempt in $(seq 1 36); do
    if health_check "$phase" >/dev/null 2>&1; then
      health_check "$phase"
      return 0
    fi
    sleep 5
  done
  echo "DR instance ${phase^^} API did not become healthy within 180 seconds." >&2
  return 1
}

mkdir -p "$META_ROOT"
chmod a+rwx "$META_ROOT"
write_env a
write_env b

echo "Candidate SHA: $CANDIDATE_SHA"
echo "DR instance A project: $PROJECT_A"
echo "DR instance B project: $PROJECT_B"
echo "Ephemeral DR root: $TEMP_ROOT"

echo "== Prepare isolated instance A data roots =="
mkdir -p "$DATA_A"/{plugins,logs,backups,media,postgres,redis}
chmod -R a+rwx "$DATA_A"
as_root install -d -m 0700 -o 10001 -g 10001 "$DATA_A/private"
# systemd-sysusers assigns animemo-updater a host-local dynamic UID. The
# isolated fixture uses a dedicated non-root UID with the production group GID.
as_root install -d -m 0700 -o "$UPDATER_FIXTURE_UID" -g "$UPDATER_GID" "$DATA_A/updater-state"
printf '%s\n' 'excluded-operational-log' >"$DATA_A/logs/dr-rehearsal.log"

echo "== Build candidate API and boot isolated instance A =="
compose a config --quiet
compose a build api
docker run --rm -i \
  --user "$UPDATER_FIXTURE_UID:$UPDATER_GID" \
  --volume "$CURRENT_ROOT:/workspace:ro" \
  --volume "$DATA_A/updater-state:/state" \
  --workdir /workspace \
  "$IMAGE_NAME" python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore, UpdateLock


def manifest(version, digit, created_at):
    return build_manifest(
        version=version,
        channel="stable",
        commit=digit * 40,
        created_at=created_at,
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[1],
        promoted_from=f"{version}-rc.1",
    )


root = Path("/state")
first = manifest("v1.0.0", "1", datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc))
second = manifest("v1.0.1", "2", datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc))
slots = ReleaseSlots(root / "releases")
runtime = RuntimeState(root)
operations = OperationStore(root)
slots.import_current(first)
runtime.initialize_from_manifest(first, enabled_plugin_apis={1})
operation = operations.create("apply_update", {"version": "v1.0.1", "fixture": "dr-rehearsal"})
for status in (
    "preflight", "fetching", "verifying", "backup", "pulling", "migrating",
    "bootstrapping", "switching", "verifying_health", "succeeded",
):
    operations.transition(operation["id"], status, detail="isolated DR fixture")
slots.promote(second, operation_id=operation["id"])
with UpdateLock(root / "update.lock"):
    pass
print("Representative updater CURRENT/PREVIOUS/history/operation fixture: PASS")
PY
compose a up -d --wait --wait-timeout 120 postgres redis
compose a run --rm --no-deps migration
compose a run --rm --no-deps bootstrap
compose a up -d --no-deps api
wait_for_api a

echo "== Seed representative database, plugin, media, private, and auth state on A =="
compose a exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py seed --output /app/ci-meta/stateful.json
compose a exec -T api sh -c "printf '%s\\n' 'dr-private-state-v1' > /app/runtime/private/dr-private.txt"
compose a exec -T api python manage.py shell <<'PY'
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from journal.external_accounts.provider_configuration import update_provider_configuration
from journal.models import ExternalProviderConfiguration
from site_config.media_storage import StoragePoolService
from site_config.models import (
    InstallationState,
    MediaStorageBackend,
    MediaStoragePoolSettings,
    SiteSettings,
)

settings = SiteSettings.load()
settings.site_name = "AniMemo DR Rehearsal"
settings.homepage_description = "dr-site-settings-v1"
settings.save(update_fields=["site_name", "homepage_description", "updated_at"])

media_backend, _ = MediaStorageBackend.objects.update_or_create(
    slug="dr-portable-local",
    defaults={
        "name": "DR portable local media",
        "backend_type": MediaStorageBackend.BackendType.LOCAL,
        "enabled": True,
        "accept_new_writes": True,
        "priority": 1,
        "warning_bytes": 1_000_000,
        "write_limit_bytes": 2_000_000,
        "local_root": "dr-portable",
        "local_public_base_url": "https://media.dr.example.test",
        "min_free_warning_bytes": 2,
        "min_free_block_bytes": 1,
    },
)
pool = MediaStoragePoolSettings.load()
pool.preferred_write_backend = media_backend
pool.save(update_fields=["preferred_write_backend", "updated_at"])
media = StoragePoolService.create_media(
    "dr/application-reference.txt",
    b"dr-portable-local-media-v1\n",
    content_type="text/plain",
)
settings.site_avatar.name = media.reference_name
settings.save(update_fields=["site_avatar", "updated_at"])

effective = update_provider_configuration(
    "bangumi",
    enabled=True,
    client_id="dr-bangumi-client-id",
    client_secret="dr-bangumi-client-secret",
    fields=("enabled", "client_id", "client_secret"),
)
stored = ExternalProviderConfiguration.objects.get(provider="bangumi")
assert effective.oauth_available
assert effective.client_secret_source == "database"
assert "dr-bangumi-client-secret" not in stored.encrypted_client_secret

user = get_user_model().objects.get(username="upgrade-gate-user-a")
user.set_password("DrRestorePass123!")
user.save(update_fields=["password"])
# Keep the real HTTP-issued access token fresh through the intentionally slow
# backup/restore flow so its later rejection proves epoch rotation, not expiry.
AccessToken.lifetime = timedelta(hours=2)
login_client = APIClient(enforce_csrf_checks=True)
csrf_response = login_client.get(
    "/api/v1/auth/csrf/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert csrf_response.status_code == 200, csrf_response.data
login_response = login_client.post(
    "/api/v1/token/",
    {"username": user.username, "password": "DrRestorePass123!"},
    format="json",
    secure=True,
    HTTP_HOST="dr.example.test",
    HTTP_X_CSRFTOKEN=csrf_response.data["csrf_token"],
)
assert login_response.status_code == 200, login_response.data
assert "refresh" not in login_response.data
access = login_response.data["access"]
refresh = login_response.cookies[django_settings.REFRESH_COOKIE_NAME].value
access_token = AccessToken(access)
assert int(access_token["exp"]) - int(access_token["iat"]) >= 2 * 60 * 60
Session.objects.update_or_create(
    session_key="drrehearsalsession",
    defaults={"session_data": "e30=", "expire_date": timezone.now() + timedelta(days=1)},
)
installation = InstallationState.load()
payload = {
    "site_settings_id": settings.pk,
    "site_name": settings.site_name,
    "homepage_description": settings.homepage_description,
    "provider_configuration_id": stored.pk,
    "provider_ciphertext": stored.encrypted_client_secret,
    "media_backend_id": media_backend.pk,
    "media_object_id": str(media.pk),
    "media_reference": media.reference_name,
    "media_object_key": media.object_key,
    "media_sha256": media.sha256,
    "installation_initialized_at": installation.initialized_at.isoformat(),
    "installation_initialized_by_id": installation.initialized_by_id,
    "authentication_epoch": installation.authentication_epoch,
}
Path("/app/ci-meta/dr-state.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
Path("/app/ci-meta/old-access-token").write_text(str(access), encoding="utf-8")
Path("/app/ci-meta/old-refresh-token").write_text(str(refresh), encoding="utf-8")
print("DR application state seed: PASS")
PY
compose a exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/stateful.json

echo "== Quiesce A and create a real PostgreSQL gzip dump =="
compose a stop api redis
compose a exec -T postgres pg_dump --format=plain --no-owner --no-privileges -U animemo -d animemo | gzip -c >"$DUMP_PATH"
test -s "$DUMP_PATH"
gzip -t "$DUMP_PATH"

echo "== Create and verify the self-describing DR backup set =="
as_root python3 "$CURRENT_ROOT/scripts/dr_backup.py" create \
  --output "$BACKUP_SET" \
  --database-dump "$DUMP_PATH" \
  --plugins "$DATA_A/plugins" \
  --media "$DATA_A/media" \
  --private "$DATA_A/private" \
  --updater-state "$DATA_A/updater-state"
as_root python3 "$CURRENT_ROOT/scripts/dr_backup.py" verify "$BACKUP_SET"

echo "== Destroy isolated instance A only =="
compose a down --volumes --remove-orphans
remove_instance_root "$DATA_A"
test ! -e "$DATA_A"
test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_A")"

echo "== Restore the backup set into empty instance B roots =="
test ! -e "$DATA_B"
as_root python3 "$CURRENT_ROOT/scripts/dr_backup.py" restore "$BACKUP_SET" --target-root "$DATA_B"
test ! -e "$DATA_B/logs"
test ! -e "$DATA_B/redis"
test "$(cat "$DATA_B/private/dr-private.txt")" = "dr-private-state-v1"
as_root mkdir -p "$DATA_B"/{logs,backups,postgres,redis}
as_root chmod -R a+rwx "$DATA_B/plugins" "$DATA_B/media" "$DATA_B/logs" "$DATA_B/backups" "$DATA_B/postgres" "$DATA_B/redis"
as_root chown -R 10001:10001 "$DATA_B/private"
as_root chmod 0700 "$DATA_B/private"
as_root chown -R "$UPDATER_FIXTURE_UID:$UPDATER_GID" "$DATA_B/updater-state"
as_root find "$DATA_B/updater-state" -type d -exec chmod 0700 {} +
as_root find "$DATA_B/updater-state" -type f -exec chmod 0600 {} +
docker run --rm -i \
  --user "$UPDATER_FIXTURE_UID:$UPDATER_GID" \
  --volume "$CURRENT_ROOT:/workspace:ro" \
  --volume "$DATA_B/updater-state:/state" \
  --workdir /workspace \
  "$IMAGE_NAME" python - <<'PY'
from pathlib import Path

from updater.runtime_state import RuntimeState
from updater.slots import ReleaseSlots
from updater.state import OperationStore, UpdateLock

root = Path("/state")
slots = ReleaseSlots(root / "releases").read()
assert slots["generation"] == 2
assert slots["current"]["release"]["version"] == "v1.0.1"
assert slots["previous"]["release"]["version"] == "v1.0.0"
assert [record["manifest"]["release"]["version"] for record in slots["history"]] == [
    "v1.0.0",
    "v1.0.1",
]
runtime = RuntimeState(root)
assert runtime.read() == {
    "databaseContract": "animemo-db-v1",
    "configurationContract": "animemo-config-v1",
    "enabledPluginApis": [1],
}
operations = OperationStore(root)
existing = operations.list()
assert len(existing) == 1 and existing[0]["status"] == "succeeded"
probe = operations.create("plan_update", {"fixture": "post-restore-write-probe"})
operations.transition(probe["id"], "preflight", detail="post-restore durable write probe")
runtime.update(enabledPluginApis=[1])
with UpdateLock(root / "update.lock"):
    pass
assert operations.get(probe["id"])["status"] == "preflight"
print("Restored updater service-identity read/write and lock verification: PASS")
PY
echo "Portable local media set restored; external R2 inventory is not exercised by this isolated rehearsal."

echo "== Prove B starts with a fresh database and rebuildable Redis =="
compose b config --quiet
compose b up -d --wait --wait-timeout 120 postgres redis
test "$(compose b exec -T postgres psql -At -U animemo -d animemo -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")" = "0"
test "$(compose b exec -T redis redis-cli --raw DBSIZE | tr -d '\r')" = "0"

echo "== Restore PostgreSQL, migrate/bootstrap, and rotate authentication epoch on B =="
gzip -dc "$DATA_B/database.sql.gz" | compose b exec -T postgres psql --set ON_ERROR_STOP=1 -U animemo -d animemo
compose b run --rm --no-deps migration
compose b run --rm --no-deps bootstrap
compose b run --rm --no-deps api python manage.py rotate_authentication_epoch --confirm-restore
compose b up -d --no-deps api
wait_for_api b

echo "== Verify restored application graph, setup lock, and authentication behavior =="
compose b exec -T api python manage.py migrate --check
compose b exec -T api python /app/ci-scripts/stateful_upgrade_fixture.py verify --input /app/ci-meta/stateful.json
compose b exec -T api python manage.py shell <<'PY'
import json
import re
from pathlib import Path
from time import time

from django.conf import settings as django_settings
from django.contrib.sessions.models import Session
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from journal.external_accounts.provider_configuration import get_effective_provider_configuration
from journal.models import ExternalProviderConfiguration
from site_config.media_storage import StoragePoolService
from site_config.models import InstallationState, MediaObject, MediaStorageBackend, SiteSettings

fixture = json.loads(Path("/app/ci-meta/dr-state.json").read_text(encoding="utf-8"))
site_settings = SiteSettings.load()
assert site_settings.pk == fixture["site_settings_id"]
assert site_settings.site_name == fixture["site_name"]
assert site_settings.homepage_description == fixture["homepage_description"]
assert site_settings.site_avatar.name == fixture["media_reference"]

media_backend = MediaStorageBackend.objects.get(
    pk=fixture["media_backend_id"],
    slug="dr-portable-local",
    backend_type=MediaStorageBackend.BackendType.LOCAL,
)
media = MediaObject.objects.get(
    pk=fixture["media_object_id"],
    storage_backend=media_backend,
    object_key=fixture["media_object_key"],
    sha256=fixture["media_sha256"],
)
assert media.reference_name == fixture["media_reference"]
assert StoragePoolService.open_reference(media.reference_name).read() == b"dr-portable-local-media-v1\n"
assert site_settings.site_avatar.open("rb").read() == b"dr-portable-local-media-v1\n"

stored = ExternalProviderConfiguration.objects.get(
    pk=fixture["provider_configuration_id"],
    provider="bangumi",
)
assert stored.encrypted_client_secret == fixture["provider_ciphertext"]
assert "dr-bangumi-client-secret" not in stored.encrypted_client_secret
effective = get_effective_provider_configuration("bangumi")
assert effective.enabled and effective.oauth_available
assert effective.client_id == "dr-bangumi-client-id"
assert effective.client_id_source == "database"
assert effective.client_secret == "dr-bangumi-client-secret"
assert effective.client_secret_source == "database"

installation = InstallationState.load()
assert installation.status == InstallationState.Status.INITIALIZED
assert installation.initialized_at.isoformat() == fixture["installation_initialized_at"]
assert installation.initialized_by_id == fixture["installation_initialized_by_id"]
assert re.fullmatch(r"[0-9a-f]{64}", installation.authentication_epoch)
assert installation.authentication_epoch != fixture["authentication_epoch"]
assert not installation.setup_code_hash
assert not Path(django_settings.FIRST_RUN_SETUP_CODE_PATH).exists()
assert not Session.objects.filter(session_key="drrehearsalsession").exists()

old_access = Path("/app/ci-meta/old-access-token").read_text(encoding="utf-8")
old_refresh = Path("/app/ci-meta/old-refresh-token").read_text(encoding="utf-8")
old_access_token = AccessToken(old_access)
assert int(old_access_token["exp"]) > int(time()) + 300, (
    "old access token expired before epoch rejection proof"
)

old_access_client = APIClient()
old_access_client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_access}")
old_access_response = old_access_client.get(
    "/api/v1/auth/me/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert old_access_response.status_code == 401, old_access_response.data

old_refresh_client = APIClient(enforce_csrf_checks=True)
old_refresh_csrf = old_refresh_client.get(
    "/api/v1/auth/csrf/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert old_refresh_csrf.status_code == 200, old_refresh_csrf.data
old_refresh_client.cookies[django_settings.REFRESH_COOKIE_NAME] = old_refresh
old_refresh_response = old_refresh_client.post(
    "/api/v1/token/refresh/",
    {},
    format="json",
    secure=True,
    HTTP_HOST="dr.example.test",
    HTTP_X_CSRFTOKEN=old_refresh_csrf.data["csrf_token"],
)
assert old_refresh_response.status_code == 401, old_refresh_response.data

login_client = APIClient(enforce_csrf_checks=True)
login_csrf = login_client.get(
    "/api/v1/auth/csrf/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert login_csrf.status_code == 200, login_csrf.data
login_response = login_client.post(
    "/api/v1/token/",
    {"username": "upgrade-gate-user-a", "password": "DrRestorePass123!"},
    format="json",
    secure=True,
    HTTP_HOST="dr.example.test",
    HTTP_X_CSRFTOKEN=login_csrf.data["csrf_token"],
)
assert login_response.status_code == 200, login_response.data
assert "access" in login_response.data and "refresh" not in login_response.data
assert login_client.cookies[django_settings.REFRESH_COOKIE_NAME].value

refresh_csrf = login_client.get(
    "/api/v1/auth/csrf/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert refresh_csrf.status_code == 200, refresh_csrf.data
refresh_response = login_client.post(
    "/api/v1/token/refresh/",
    {},
    format="json",
    secure=True,
    HTTP_HOST="dr.example.test",
    HTTP_X_CSRFTOKEN=refresh_csrf.data["csrf_token"],
)
assert refresh_response.status_code == 200, refresh_response.data
assert "access" in refresh_response.data and "refresh" not in refresh_response.data

new_access_client = APIClient()
new_access_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh_response.data['access']}")
me_response = new_access_client.get(
    "/api/v1/auth/me/",
    secure=True,
    HTTP_HOST="dr.example.test",
)
assert me_response.status_code == 200, me_response.data
assert me_response.data["username"] == "upgrade-gate-user-a"
print("DR media graph and restored authentication/refresh verification: PASS")
PY
compose b exec -T api python - <<'PY'
import http.client
import json

connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
connection.request(
    "GET",
    "/api/v1/setup/status/",
    headers={"Host": "dr.example.test", "X-Forwarded-Proto": "https"},
)
response = connection.getresponse()
payload = json.loads(response.read())
assert response.status == 200, (response.status, payload)
assert payload == {"state": "initialized", "accepting_setup": False, "expires_at": None}, payload
print("Restored first-run setup remains locked: PASS")
PY

echo "Isolated A-to-B disaster-recovery rehearsal: PASS"
