#!/usr/bin/env sh
set -eu

# Legacy bootstrap / break-glass deployer only. Normal updates use the AniMemo
# Update Agent with immutable GHCR digests and never call this script.

DEFAULT_APP_ROOT=/opt/1panel/docker/compose/animemo/app
DEFAULT_RELEASE_ROOT=/opt/1panel/docker/compose/animemo/releases
DEFAULT_DATA_ROOT=/data/animemo
DEFAULT_OPENRESTY_CONF=/opt/1panel/www/conf.d/animemo.cc.conf
DEFAULT_OPENRESTY_CONTAINER=1Panel-openresty-t1AN

APP_ROOT=${ANIMEMO_APP_ROOT:-$DEFAULT_APP_ROOT}
RELEASE_ROOT=${ANIMEMO_RELEASE_ROOT:-$DEFAULT_RELEASE_ROOT}
DATA_ROOT=${ANIMEMO_DATA_ROOT:-}
OPENRESTY_CONF=${ANIMEMO_OPENRESTY_CONF:-$DEFAULT_OPENRESTY_CONF}
OPENRESTY_CONTAINER=${ANIMEMO_OPENRESTY_CONTAINER:-$DEFAULT_OPENRESTY_CONTAINER}
ARCHIVE=${ANIMEMO_ARCHIVE:-}
SHA_FILE=${ANIMEMO_SHA256_FILE:-}
ENV_SOURCE=${ANIMEMO_ENV_FILE:-}
MODE=
RESET_DATA=0
CONFIRM_RESET=0
SKIP_OPENRESTY=0

usage() {
    cat <<'EOF'
Usage:
  sudo sh deploy/deploy.sh --bootstrap --archive /tmp/animemo.zip [options]
  sudo sh deploy/deploy.sh --break-glass --archive /tmp/animemo.zip [options]

Modes (exactly one is required):
  --bootstrap          First installation or one-time legacy-to-Updater cutover.
  --break-glass        Manual recovery when the immutable Update Agent path cannot run.

Options:
  --archive PATH       Legacy Core source ZIP (required).
  --sha256 PATH        SHA-256 file; defaults to PATH.sha256.
  --env-file PATH      Existing production env; otherwise keep app/.env.production.
  --reset-data         Bootstrap-only destructive reset of AniMemo data. Requires --yes
                       or an interactive exact confirmation.
  --create-admin       Removed. The browser /setup flow is the only supported first-admin path.
  --yes                Confirm --reset-data in non-interactive use.
  --skip-openresty     Do not install or reload the AniMemo site config.
  --app-root PATH      Override the exact AniMemo app path.
  --release-root PATH  Override the exact AniMemo legacy archive path.
  --data-root PATH     Override the AniMemo persistent data path.
  --openresty-conf PATH
                       Override the single AniMemo OpenResty config path.
  --openresty-container NAME
                       Override the OpenResty container name.
  -h, --help           Show this help.

This path performs a server-side source build and is not a normal update path.
It never automatically reverses migrations or restores the database.
EOF
}

die() {
    echo "AniMemo legacy deploy: $*" >&2
    exit 1
}

log() {
    echo "[animemo legacy] $*"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

canonical_path() {
    path=$1
    case "$path" in
        /*) ;;
        *) path=$(pwd -P)/$path ;;
    esac
    directory=$(dirname "$path")
    filename=$(basename "$path")
    directory=$(cd "$directory" 2>/dev/null && pwd -P) || die "parent directory does not exist: $path"
    printf '%s/%s\n' "$directory" "$filename"
}

assert_safe_target() {
    label=$1
    path=$2
    case "$path" in
        /*) ;;
        *) die "$label must be an absolute path: $path" ;;
    esac
    case "$path" in
        ""|/|/opt|/data|/var|/etc|/root|/home)
            die "$label is a dangerous broad target: $path"
            ;;
        *..*|*\"*|*\'*|*\`*)
            die "$label contains an unsafe path: $path"
            ;;
    esac
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bootstrap)
            [ -z "$MODE" ] || die "choose exactly one of --bootstrap or --break-glass"
            MODE=bootstrap
            shift
            ;;
        --break-glass)
            [ -z "$MODE" ] || die "choose exactly one of --bootstrap or --break-glass"
            MODE=break-glass
            shift
            ;;
        --archive)
            [ "$#" -ge 2 ] || die "--archive requires a path"
            ARCHIVE=$2
            shift 2
            ;;
        --sha256)
            [ "$#" -ge 2 ] || die "--sha256 requires a path"
            SHA_FILE=$2
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || die "--env-file requires a path"
            ENV_SOURCE=$2
            shift 2
            ;;
        --reset-data)
            RESET_DATA=1
            shift
            ;;
        --create-admin)
            die "--create-admin was removed; complete first-admin creation through the browser /setup flow"
            ;;
        --yes)
            CONFIRM_RESET=1
            shift
            ;;
        --skip-openresty)
            SKIP_OPENRESTY=1
            shift
            ;;
        --app-root)
            [ "$#" -ge 2 ] || die "--app-root requires a path"
            APP_ROOT=$2
            shift 2
            ;;
        --release-root)
            [ "$#" -ge 2 ] || die "--release-root requires a path"
            RELEASE_ROOT=$2
            shift 2
            ;;
        --data-root)
            [ "$#" -ge 2 ] || die "--data-root requires a path"
            DATA_ROOT=$2
            shift 2
            ;;
        --openresty-conf)
            [ "$#" -ge 2 ] || die "--openresty-conf requires a path"
            OPENRESTY_CONF=$2
            shift 2
            ;;
        --openresty-container)
            [ "$#" -ge 2 ] || die "--openresty-container requires a name"
            OPENRESTY_CONTAINER=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option: $1"
            ;;
        *)
            [ -z "$ARCHIVE" ] || die "unexpected argument: $1"
            ARCHIVE=$1
            shift
            ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo is fine)"
[ -n "$MODE" ] || die "choose exactly one of --bootstrap or --break-glass; normal updates use the AniMemo Update Agent"
[ -n "$ARCHIVE" ] || die "--archive is required"
if [ "$RESET_DATA" -eq 1 ] && [ "$MODE" != bootstrap ]; then
    die "--reset-data requires --bootstrap"
fi
for command in docker sha256sum awk sed tr find mktemp install; do
    require_cmd "$command"
done

if command -v unzip >/dev/null 2>&1; then
    ARCHIVE_BACKEND=unzip
elif command -v python3 >/dev/null 2>&1; then
    ARCHIVE_BACKEND=python3
else
    die "release extraction requires unzip or python3"
fi

archive_list() {
    archive=$1
    if [ "$ARCHIVE_BACKEND" = unzip ]; then
        unzip -Z1 "$archive"
    else
        python3 - "$archive" <<'PY'
import sys
from zipfile import ZipFile

with ZipFile(sys.argv[1]) as archive_file:
    for name in archive_file.namelist():
        print(name)
PY
    fi
}

archive_extract() {
    archive=$1
    destination=$2
    if [ "$ARCHIVE_BACKEND" = unzip ]; then
        unzip -q "$archive" -d "$destination"
    else
        python3 - "$archive" "$destination" <<'PY'
import os
import sys
from pathlib import Path
from zipfile import ZipFile

archive_path, destination_path = sys.argv[1:]
destination = Path(destination_path).resolve()
with ZipFile(archive_path) as archive_file:
    for member in archive_file.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and not str(target).startswith(f"{destination}{os.sep}"):
            raise SystemExit(f"unsafe archive path: {member.filename}")
    archive_file.extractall(destination)
PY
    fi
}

ARCHIVE=$(canonical_path "$ARCHIVE")
[ -f "$ARCHIVE" ] || die "release archive not found: $ARCHIVE"
if [ -z "$SHA_FILE" ]; then
    SHA_FILE=${ARCHIVE%.zip}.sha256
    [ -f "$SHA_FILE" ] || SHA_FILE=${ARCHIVE}.sha256
fi
SHA_FILE=$(canonical_path "$SHA_FILE")
[ -f "$SHA_FILE" ] || die "SHA-256 file not found: $SHA_FILE"

assert_safe_target "app root" "$APP_ROOT"
assert_safe_target "release root" "$RELEASE_ROOT"
assert_safe_target "data root" "${DATA_ROOT:-$DEFAULT_DATA_ROOT}"
assert_safe_target "OpenResty config" "$OPENRESTY_CONF"
if [ "$APP_ROOT" != "$DEFAULT_APP_ROOT" ] || [ "$RELEASE_ROOT" != "$DEFAULT_RELEASE_ROOT" ] || [ "$OPENRESTY_CONF" != "$DEFAULT_OPENRESTY_CONF" ]; then
    [ "${ANIMEMO_ALLOW_CUSTOM_PATHS:-0}" = 1 ] || die "custom server paths require ANIMEMO_ALLOW_CUSTOM_PATHS=1"
fi

if [ -z "$ENV_SOURCE" ] && [ -f "$APP_ROOT/.env.production" ]; then
    ENV_SOURCE=$APP_ROOT/.env.production
fi
if [ -n "$ENV_SOURCE" ]; then
    ENV_SOURCE=$(canonical_path "$ENV_SOURCE")
    [ -f "$ENV_SOURCE" ] || die "production env not found: $ENV_SOURCE"
fi
if [ -z "$DATA_ROOT" ] && [ -n "$ENV_SOURCE" ]; then
    DATA_ROOT=$(sed -n 's/^ANIMEMO_DATA_ROOT=//p' "$ENV_SOURCE" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
fi
DATA_ROOT=${DATA_ROOT:-$DEFAULT_DATA_ROOT}
assert_safe_target "data root" "$DATA_ROOT"

case "$ARCHIVE/" in
    "$APP_ROOT/"*) die "release archive must live outside the app root: $ARCHIVE" ;;
esac
case "$RELEASE_ROOT/" in
    "$APP_ROOT/"*) die "release archive directory must live outside the app root: $RELEASE_ROOT" ;;
esac

if [ "$RESET_DATA" -eq 1 ] && [ "$CONFIRM_RESET" -ne 1 ] && [ ! -t 0 ]; then
    die "--reset-data is non-interactive; add --yes to confirm"
fi
if [ "$RESET_DATA" -eq 1 ] && [ "$CONFIRM_RESET" -ne 1 ]; then
    printf 'This clears only AniMemo data under %s. Type RESET animemo: ' "$DATA_ROOT" >&2
    read confirmation
    [ "$confirmation" = "RESET animemo" ] || die "reset not confirmed"
fi

expected_sha=$(tr -d '\r' < "$SHA_FILE" | awk 'NF {print $1; exit}')
printf '%s\n' "$expected_sha" | grep -Eq '^[[:xdigit:]]{64}$' || die "invalid SHA-256 file: $SHA_FILE"
actual_sha=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[ "$actual_sha" = "$expected_sha" ] || die "release SHA-256 mismatch (expected $expected_sha, got $actual_sha)"
log "release SHA-256 verified: $actual_sha"

zip_entries=$(archive_list "$ARCHIVE") || die "cannot list release archive"
printf '%s\n' "$zip_entries" | awk '
    /^\// || /(^|\/)\.\.(\/|$)/ || /\\/ { exit 1 }
' || die "release archive contains an unsafe path"
for entry in $zip_entries; do
    case "$entry" in
        qa|qa/*|node_modules|node_modules/*|dist|dist/*|.npm-cache|.npm-cache/*|__pycache__|*/__pycache__/*|.env.production|*/.env.production|*.ajplugin)
            die "core-only archive contains forbidden entry: $entry"
            ;;
    esac
done

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/animemo-legacy.XXXXXX")
EXTRACT_ROOT=$TMP_ROOT/release
OPENRESTY_BACKUP=$TMP_ROOT/openresty.conf
OPENRESTY_CHANGED=0
mkdir -p "$EXTRACT_ROOT"

cleanup() {
    status=$?
    set +e
    if [ "$status" -ne 0 ] && [ "$OPENRESTY_CHANGED" -eq 1 ] && [ -f "$OPENRESTY_BACKUP" ]; then
        install -m 0644 "$OPENRESTY_BACKUP" "$OPENRESTY_CONF" >/dev/null 2>&1 || true
        docker exec "$OPENRESTY_CONTAINER" openresty -s reload >/dev/null 2>&1 || docker exec "$OPENRESTY_CONTAINER" nginx -s reload >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_ROOT"
    if [ "$status" -ne 0 ]; then
        echo "Legacy deploy failed. Database state was not reversed; inspect migration/bootstrap output before manual recovery." >&2
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

archive_extract "$ARCHIVE" "$EXTRACT_ROOT" || die "cannot extract release archive"
for required in \
    deploy/docker-compose.yml deploy/docker-compose.build.yml deploy/deploy.sh \
    deploy/prepare-host.sh deploy/smoke-test.sh \
    deploy/openresty-animemo.conf .env.production.example package.json; do
    [ -f "$EXTRACT_ROOT/$required" ] || die "archive is missing $required"
done
[ ! -e "$EXTRACT_ROOT/.env.production" ] || die "release archive must not contain a real .env.production"
[ -z "$(find "$EXTRACT_ROOT" -type l -print -quit)" ] || die "release archive must not contain symlinks"
[ -n "$ENV_SOURCE" ] || die "no production env found; create .env.production or pass --env-file"
cp "$ENV_SOURCE" "$EXTRACT_ROOT/.env.production"
chmod 0600 "$EXTRACT_ROOT/.env.production"

env_data_root=$(sed -n 's/^ANIMEMO_DATA_ROOT=//p' "$EXTRACT_ROOT/.env.production" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
if [ -n "$env_data_root" ] && [ "$env_data_root" != "$DATA_ROOT" ]; then
    die "ANIMEMO_DATA_ROOT in production env ($env_data_root) does not match --data-root ($DATA_ROOT)"
fi
if [ -z "$env_data_root" ]; then
    printf '\nANIMEMO_DATA_ROOT=%s\n' "$DATA_ROOT" >> "$EXTRACT_ROOT/.env.production"
fi
env_media_root=$(sed -n 's/^MEDIA_LOCAL_STORAGE_ROOT=//p' "$EXTRACT_ROOT/.env.production" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
if [ -n "$env_media_root" ] && [ "$env_media_root" != "$DATA_ROOT/media" ]; then
    die "MEDIA_LOCAL_STORAGE_ROOT must be $DATA_ROOT/media for this deployer"
fi
if [ -z "$env_media_root" ]; then
    printf 'MEDIA_LOCAL_STORAGE_ROOT=%s/media\n' "$DATA_ROOT" >> "$EXTRACT_ROOT/.env.production"
fi

ANIMEMO_DATA_ROOT=$DATA_ROOT
export ANIMEMO_DATA_ROOT
sh "$EXTRACT_ROOT/deploy/prepare-host.sh"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
ANIMEMO_API_IMAGE=animemo-api:legacy-$stamp
ANIMEMO_WEB_IMAGE=animemo-web:legacy-$stamp
export ANIMEMO_API_IMAGE ANIMEMO_WEB_IMAGE

stage_compose() {
    (cd "$EXTRACT_ROOT" && docker compose \
        --project-name animemo \
        --env-file .env.production \
        -f deploy/docker-compose.yml \
        -f deploy/docker-compose.build.yml \
        "$@")
}

live_compose() {
    (cd "$APP_ROOT" && docker compose \
        --project-name animemo \
        --env-file .env.production \
        -f deploy/docker-compose.yml \
        "$@")
}

stage_compose config --quiet
stage_compose build api web

if [ "$RESET_DATA" -eq 1 ]; then
    if [ -d "$APP_ROOT" ] && [ -f "$APP_ROOT/.env.production" ]; then
        live_compose stop api web postgres redis || die "could not stop the exact AniMemo project for reset"
    fi
    log "clearing only AniMemo data under $DATA_ROOT"
    for directory in postgres redis plugins logs backups media private; do
        [ ! -e "$DATA_ROOT/$directory" ] || rm -rf "$DATA_ROOT/$directory"
    done
    sh "$EXTRACT_ROOT/deploy/prepare-host.sh"
fi

stage_compose up -d --wait --wait-timeout 120 postgres redis
stage_compose run --rm --no-deps migration
stage_compose run --rm --no-deps bootstrap

mkdir -p "$(dirname "$APP_ROOT")" "$RELEASE_ROOT"
PREVIOUS_APP=
if [ -d "$APP_ROOT" ]; then
    PREVIOUS_APP=$RELEASE_ROOT/app-before-$stamp
    [ ! -e "$PREVIOUS_APP" ] || die "legacy recovery tree already exists: $PREVIOUS_APP"
    mv "$APP_ROOT" "$PREVIOUS_APP"
    log "previous application tree retained at $PREVIOUS_APP"
fi
mv "$EXTRACT_ROOT" "$APP_ROOT"

live_compose up -d --no-deps --force-recreate api web
(cd "$APP_ROOT" && sh deploy/smoke-test.sh)

if [ "$SKIP_OPENRESTY" -eq 0 ]; then
    if [ -f "$OPENRESTY_CONF" ]; then
        cp "$OPENRESTY_CONF" "$OPENRESTY_BACKUP"
    fi
    install -m 0644 "$APP_ROOT/deploy/openresty-animemo.conf" "$OPENRESTY_CONF"
    OPENRESTY_CHANGED=1
    if ! docker exec "$OPENRESTY_CONTAINER" openresty -t >/dev/null 2>&1; then
        docker exec "$OPENRESTY_CONTAINER" nginx -t >/dev/null 2>&1 || die "OpenResty rejected $OPENRESTY_CONF"
    fi
    docker exec "$OPENRESTY_CONTAINER" openresty -s reload >/dev/null 2>&1 || docker exec "$OPENRESTY_CONTAINER" nginx -s reload >/dev/null 2>&1 || die "OpenResty reload failed"
    log "AniMemo OpenResty config installed and reloaded"
fi

archive_name=$(basename "$ARCHIVE")
sha_name=$(basename "$SHA_FILE")
if [ "$ARCHIVE" != "$RELEASE_ROOT/$archive_name" ]; then
    cp "$ARCHIVE" "$RELEASE_ROOT/$archive_name"
fi
tr -d '\r' < "$SHA_FILE" > "$RELEASE_ROOT/$sha_name"
cat > "$RELEASE_ROOT/current.json" <<EOF
{"archive":"$archive_name","sha256":"$actual_sha","deployed_at_utc":"$stamp","mode":"$MODE","normal_update_path":"animemo-updater"}
EOF

trap - EXIT INT TERM
rm -rf "$TMP_ROOT"
log "legacy $MODE complete: $archive_name; normal future updates must use the AniMemo Update Agent"
