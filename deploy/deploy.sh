#!/usr/bin/env sh
set -eu

# One-site production deployer. It deliberately knows only the Anime Journal
# paths so a failed release cannot turn into a server-wide cleanup.

DEFAULT_APP_ROOT=/opt/1panel/docker/compose/anime-journal/app
DEFAULT_RELEASE_ROOT=/opt/1panel/docker/compose/anime-journal/releases
DEFAULT_DATA_ROOT=/data/anime-journal
DEFAULT_OPENRESTY_CONF=/opt/1panel/www/conf.d/re-anime.cc.conf
DEFAULT_OPENRESTY_CONTAINER=1Panel-openresty-t1AN

APP_ROOT=${ANIME_JOURNAL_APP_ROOT:-$DEFAULT_APP_ROOT}
RELEASE_ROOT=${ANIME_JOURNAL_RELEASE_ROOT:-$DEFAULT_RELEASE_ROOT}
DATA_ROOT=${ANIME_JOURNAL_DATA_ROOT:-}
OPENRESTY_CONF=${ANIME_JOURNAL_OPENRESTY_CONF:-$DEFAULT_OPENRESTY_CONF}
OPENRESTY_CONTAINER=${ANIME_JOURNAL_OPENRESTY_CONTAINER:-$DEFAULT_OPENRESTY_CONTAINER}
ARCHIVE=${ANIME_JOURNAL_ARCHIVE:-}
SHA_FILE=${ANIME_JOURNAL_SHA256_FILE:-}
ENV_SOURCE=${ANIME_JOURNAL_ENV_FILE:-}
MODE=update
FRESH_REQUESTED=0
RESET_DATA=0
CREATE_ADMIN=0
CONFIRM_RESET=0
SKIP_OPENRESTY=0

usage() {
    cat <<'EOF'
Usage:
  sudo sh deploy/deploy.sh --archive /tmp/anime-journal.zip [options]

Options:
  --archive PATH       Core release ZIP (required)
  --sha256 PATH        SHA-256 file; defaults to PATH.sha256
  --env-file PATH      Existing production env; otherwise keep app/.env.production
  --fresh              Replace only the Anime Journal app tree and remove legacy
                       anime-journal-data volume. Persistent bind-mounted data stays.
  --reset-data         Also clear Anime Journal PostgreSQL/Redis/media data.
                       Requires --fresh and --yes (or an interactive confirmation).
  --create-admin       Create the initial superuser once deployment passes smoke tests.
  --yes                Confirm destructive --reset-data operation.
  --skip-openresty     Do not install or reload the re-anime.cc site config.
  --app-root PATH      Override the exact Anime Journal app path.
  --release-root PATH  Override the exact Anime Journal release archive path.
  --data-root PATH     Override the Anime Journal persistent data path.
  --openresty-conf PATH
                       Override the single Anime Journal OpenResty config path.
  --openresty-container NAME
                       Override the OpenResty container name.
  -h, --help           Show this help.

Update is the default and never deletes application data. Use --fresh only for
the first migration or a deliberate code-tree replacement.
EOF
}

die() {
    echo "Anime Journal deploy: $*" >&2
    exit 1
}

log() {
    echo "[anime-journal] $*"
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
    esac
    case "$path" in
        *..*|*\"*|*\'*|*\`*)
            die "$label contains an unsafe path: $path"
            ;;
    esac
}

while [ "$#" -gt 0 ]; do
    case "$1" in
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
        --fresh)
            MODE=fresh
            FRESH_REQUESTED=1
            shift
            ;;
        --reset-data)
            RESET_DATA=1
            shift
            ;;
        --create-admin)
            CREATE_ADMIN=1
            shift
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
        -* )
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
[ -n "$ARCHIVE" ] || die "--archive is required"

require_cmd docker
require_cmd sha256sum
require_cmd awk
require_cmd sed
require_cmd tr
require_cmd find
require_cmd mktemp
require_cmd install

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
assert_safe_target "data root" "${DATA_ROOT:-/data/anime-journal}"
assert_safe_target "OpenResty config" "$OPENRESTY_CONF"

if [ "$APP_ROOT" != "$DEFAULT_APP_ROOT" ] || [ "$RELEASE_ROOT" != "$DEFAULT_RELEASE_ROOT" ] || [ "$OPENRESTY_CONF" != "$DEFAULT_OPENRESTY_CONF" ]; then
    [ "${ANIME_JOURNAL_ALLOW_CUSTOM_PATHS:-0}" = 1 ] || die "custom server paths require ANIME_JOURNAL_ALLOW_CUSTOM_PATHS=1"
fi

if [ -z "$ENV_SOURCE" ] && [ -f "$APP_ROOT/.env.production" ]; then
    ENV_SOURCE=$APP_ROOT/.env.production
fi
if [ -n "$ENV_SOURCE" ]; then
    ENV_SOURCE=$(canonical_path "$ENV_SOURCE")
    [ -f "$ENV_SOURCE" ] || die "production env not found: $ENV_SOURCE"
fi

if [ -z "$DATA_ROOT" ] && [ -n "$ENV_SOURCE" ]; then
    DATA_ROOT=$(sed -n 's/^ANIME_JOURNAL_DATA_ROOT=//p' "$ENV_SOURCE" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
fi
DATA_ROOT=${DATA_ROOT:-$DEFAULT_DATA_ROOT}
assert_safe_target "data root" "$DATA_ROOT"

case "$ARCHIVE/" in
    "$APP_ROOT/"*) die "release archive must live outside the app root: $ARCHIVE" ;;
esac
case "$RELEASE_ROOT/" in
    "$APP_ROOT/"*) die "release archive directory must live outside the app root: $RELEASE_ROOT" ;;
esac

if [ "$RESET_DATA" -eq 1 ] && [ "$FRESH_REQUESTED" -ne 1 ]; then
    die "--reset-data requires --fresh"
fi
if [ "$RESET_DATA" -eq 1 ] && [ "$CONFIRM_RESET" -ne 1 ] && [ ! -t 0 ]; then
    die "--reset-data is non-interactive; add --yes to confirm"
fi
if [ "$RESET_DATA" -eq 1 ] && [ "$CONFIRM_RESET" -ne 1 ]; then
    printf 'This clears only Anime Journal data under %s. Type RESET anime-journal: ' "$DATA_ROOT" >&2
    read confirmation
    [ "$confirmation" = "RESET anime-journal" ] || die "reset not confirmed"
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

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/anime-journal-deploy.XXXXXX")
EXTRACT_ROOT=$TMP_ROOT/release
PREVIOUS_APP=$TMP_ROOT/previous-app
OPENRESTY_BACKUP=$TMP_ROOT/openresty.conf
mkdir -p "$EXTRACT_ROOT"
SWAPPED=0
OPENRESTY_CHANGED=0

cleanup() {
    status=$?
    set +e
    if [ "$status" -ne 0 ] && [ "$SWAPPED" -eq 1 ]; then
        log "deployment failed; restoring the previous Anime Journal app tree"
        (cd "$APP_ROOT" && docker compose --env-file .env.production -f deploy/docker-compose.yml down --remove-orphans) >/dev/null 2>&1 || true
        [ -d "$APP_ROOT" ] && rm -rf "$APP_ROOT"
        [ -d "$PREVIOUS_APP" ] && mv "$PREVIOUS_APP" "$APP_ROOT"
        if [ -f "$APP_ROOT/.env.production" ]; then
            (cd "$APP_ROOT" && docker compose --env-file .env.production -f deploy/docker-compose.yml up -d) >/dev/null 2>&1 || true
        fi
    fi
    if [ "$status" -ne 0 ] && [ "$OPENRESTY_CHANGED" -eq 1 ] && [ -f "$OPENRESTY_BACKUP" ]; then
        install -m 0644 "$OPENRESTY_BACKUP" "$OPENRESTY_CONF" >/dev/null 2>&1 || true
        docker exec "$OPENRESTY_CONTAINER" openresty -s reload >/dev/null 2>&1 || docker exec "$OPENRESTY_CONTAINER" nginx -s reload >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_ROOT"
    exit "$status"
}
trap cleanup EXIT INT TERM

archive_extract "$ARCHIVE" "$EXTRACT_ROOT" || die "cannot extract release archive"
[ -f "$EXTRACT_ROOT/deploy/docker-compose.yml" ] || die "archive is missing deploy/docker-compose.yml"
[ -f "$EXTRACT_ROOT/deploy/deploy.sh" ] || die "archive is missing deploy/deploy.sh"
[ -f "$EXTRACT_ROOT/deploy/create-admin.sh" ] || die "archive is missing deploy/create-admin.sh"
[ -f "$EXTRACT_ROOT/deploy/prepare-host.sh" ] || die "archive is missing deploy/prepare-host.sh"
[ -f "$EXTRACT_ROOT/deploy/smoke-test.sh" ] || die "archive is missing deploy/smoke-test.sh"
[ -f "$EXTRACT_ROOT/deploy/openresty-re-anime.conf" ] || die "archive is missing deploy/openresty-re-anime.conf"
[ -f "$EXTRACT_ROOT/.env.production.example" ] || die "archive is missing .env.production.example"
[ -f "$EXTRACT_ROOT/package.json" ] || die "archive is missing package.json"
[ ! -e "$EXTRACT_ROOT/.env.production" ] || die "release archive must not contain a real .env.production"
[ -z "$(find "$EXTRACT_ROOT" -type l -print -quit)" ] || die "release archive must not contain symlinks"

if [ -z "$ENV_SOURCE" ]; then
    die "no production env found; create .env.production or pass --env-file"
fi
cp "$ENV_SOURCE" "$EXTRACT_ROOT/.env.production"
chmod 0600 "$EXTRACT_ROOT/.env.production"

env_data_root=$(sed -n 's/^ANIME_JOURNAL_DATA_ROOT=//p' "$EXTRACT_ROOT/.env.production" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
if [ -n "$env_data_root" ] && [ "$env_data_root" != "$DATA_ROOT" ]; then
    die "ANIME_JOURNAL_DATA_ROOT in production env ($env_data_root) does not match --data-root ($DATA_ROOT)"
fi
if [ -z "$env_data_root" ]; then
    printf '\nANIME_JOURNAL_DATA_ROOT=%s\n' "$DATA_ROOT" >> "$EXTRACT_ROOT/.env.production"
fi
env_media_root=$(sed -n 's/^MEDIA_LOCAL_STORAGE_ROOT=//p' "$EXTRACT_ROOT/.env.production" | sed 's/\r$//' | tail -n 1 | tr -d "\"'")
if [ -n "$env_media_root" ] && [ "$env_media_root" != "$DATA_ROOT/media" ]; then
    die "MEDIA_LOCAL_STORAGE_ROOT must be $DATA_ROOT/media for this deployer"
fi
if [ -z "$env_media_root" ]; then
    printf 'MEDIA_LOCAL_STORAGE_ROOT=%s/media\n' "$DATA_ROOT" >> "$EXTRACT_ROOT/.env.production"
fi

ANIME_JOURNAL_DATA_ROOT=$DATA_ROOT
export ANIME_JOURNAL_DATA_ROOT
sh "$EXTRACT_ROOT/deploy/prepare-host.sh"

stage_compose() {
    (cd "$EXTRACT_ROOT" && docker compose --env-file .env.production -f deploy/docker-compose.yml "$@")
}
stage_compose config -q
stage_compose build

stage_compose down --remove-orphans || die "could not stop the current Anime Journal Compose project"

if [ "$RESET_DATA" -eq 1 ]; then
    log "clearing only Anime Journal data under $DATA_ROOT"
    for directory in postgres redis plugins logs backups media; do
        [ -e "$DATA_ROOT/$directory" ] && rm -rf "$DATA_ROOT/$directory"
    done
    sh "$EXTRACT_ROOT/deploy/prepare-host.sh"
fi

if [ "$FRESH_REQUESTED" -eq 1 ] && docker volume inspect anime-journal-data >/dev/null 2>&1; then
    log "removing the legacy Anime Journal named volume anime-journal-data"
    docker volume rm anime-journal-data >/dev/null
fi

mkdir -p "$(dirname "$APP_ROOT")" "$RELEASE_ROOT"
if [ -d "$APP_ROOT" ]; then
    mv "$APP_ROOT" "$PREVIOUS_APP"
fi
mv "$EXTRACT_ROOT" "$APP_ROOT"
SWAPPED=1

cd "$APP_ROOT"
docker compose --env-file .env.production -f deploy/docker-compose.yml up -d --remove-orphans
sh deploy/smoke-test.sh

if [ "$SKIP_OPENRESTY" -eq 0 ]; then
    if [ -f "$OPENRESTY_CONF" ]; then
        cp "$OPENRESTY_CONF" "$OPENRESTY_BACKUP"
    fi
    install -m 0644 deploy/openresty-re-anime.conf "$OPENRESTY_CONF"
    OPENRESTY_CHANGED=1
    if ! docker exec "$OPENRESTY_CONTAINER" openresty -t >/dev/null 2>&1; then
        docker exec "$OPENRESTY_CONTAINER" nginx -t >/dev/null 2>&1 || die "OpenResty rejected $OPENRESTY_CONF"
    fi
    docker exec "$OPENRESTY_CONTAINER" openresty -s reload >/dev/null 2>&1 || docker exec "$OPENRESTY_CONTAINER" nginx -s reload >/dev/null 2>&1 || die "OpenResty reload failed"
    log "re-anime.cc OpenResty config installed and reloaded"
fi

if [ "$CREATE_ADMIN" -eq 1 ]; then
    sh deploy/create-admin.sh
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_name=$(basename "$ARCHIVE")
sha_name=$(basename "$SHA_FILE")
if [ "$ARCHIVE" != "$RELEASE_ROOT/$archive_name" ]; then
    cp "$ARCHIVE" "$RELEASE_ROOT/$archive_name"
fi
tr -d '\r' < "$SHA_FILE" > "$RELEASE_ROOT/$sha_name"
cat > "$RELEASE_ROOT/current.json" <<EOF
{"archive":"$archive_name","sha256":"$actual_sha","deployed_at_utc":"$stamp","mode":"$MODE"}
EOF

[ "$SWAPPED" -eq 1 ] && [ -d "$PREVIOUS_APP" ] && rm -rf "$PREVIOUS_APP"
SWAPPED=0
log "deployment complete: $archive_name ($MODE)"
