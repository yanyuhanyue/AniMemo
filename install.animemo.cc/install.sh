#!/bin/sh
set -eu

REPOSITORY="yanyuhanyue/AniMemo"
OFFICIAL_MIRROR_ROOT="https://download.animemo.app/github/yanyuhanyue/AniMemo/releases"
SOURCE="github"
VERSION=""
PUBLIC_ORIGIN=""
STAGING=""

usage() {
  printf '%s\n' \
    'usage: install.sh [--source github|official-mirror|local-bundle] [--version vX.Y.Z] [--public-origin https://animemo.example]' >&2
}

fail() {
  printf 'animemo-bootstrap: %s\n' "$1" >&2
  exit "${2:-1}"
}

cleanup() {
  if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
    rm -rf -- "$STAGING"
  fi
}
trap cleanup EXIT HUP INT TERM

install_runtime_dependencies() {
  command -v apt-get >/dev/null 2>&1 || fail "supported package manager is unavailable" 69
  [ -r /etc/os-release ] || fail "supported operating system identity is unavailable" 69
  grep -Eq '^ID="?ubuntu"?$' /etc/os-release || fail "unsupported operating system" 69
  apt-get update || fail "runtime dependency catalog refresh failed" 69
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    coreutils \
    curl \
    docker.io \
    docker-compose-v2 \
    gh \
    python3 \
    python3-venv \
    tar \
    || fail "runtime dependency installation failed" 69
  systemctl enable --now docker || fail "Docker runtime activation failed" 69
}

main() {
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      SOURCE=$2
      shift 2
      ;;
    --version)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      VERSION=$2
      shift 2
      ;;
    --public-origin)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      PUBLIC_ORIGIN=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unsupported argument" 2
      ;;
  esac
done

case "$SOURCE" in
  github|official-mirror) ;;
  local-bundle)
    fail "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY" 78
    ;;
  *)
    fail "TRANSPORT_SOURCE_UNSUPPORTED" 2
    ;;
esac

[ "$(uname -s 2>/dev/null || true)" = "Linux" ] || fail "unsupported environment" 69
[ "$(id -u)" -eq 0 ] || fail "run with sudo or as root" 77
if [ -z "$PUBLIC_ORIGIN" ]; then
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    printf '%s' 'AniMemo HTTPS Public Origin (example: https://animemo.example): ' >/dev/tty
    IFS= read -r PUBLIC_ORIGIN </dev/tty || fail "public origin input failed" 2
  else
    fail "--public-origin is required in non-interactive mode" 2
  fi
fi
case "$PUBLIC_ORIGIN" in
  https://*) ;;
  *) fail "public origin must use https" 2 ;;
esac

dependencies_missing=0
for tool in curl docker gh python3 tar sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    dependencies_missing=1
  fi
done
if [ "$dependencies_missing" -eq 1 ]; then
  install_runtime_dependencies
fi

for tool in curl docker gh python3 tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || fail "required tool is unavailable: $tool" 69
done
docker info >/dev/null 2>&1 || fail "Docker runtime is unavailable" 69

STAGING=$(mktemp -d "${TMPDIR:-/tmp}/animemo-bootstrap.XXXXXX") || fail "temporary directory creation failed" 73
chmod 700 "$STAGING" || fail "temporary directory permission failed" 73

if [ -z "$VERSION" ]; then
  VERSION=$(curl -fsSL --proto '=https' --tlsv1.2 --max-redirs 0 \
    -H 'Accept: application/vnd.github+json' \
    -H 'User-Agent: AniMemo-Installer-Bootstrap' \
    "https://api.github.com/repos/$REPOSITORY/releases/latest" \
    | python3 -c 'import json,sys; value=json.load(sys.stdin).get("tag_name"); print(value if isinstance(value, str) else "")') \
    || fail "GitHub Release discovery failed" 69
fi
case "$VERSION" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) fail "release version is invalid" 2 ;;
esac
case "${VERSION#v}" in
  *[!0-9.]*|.*|*.|*..*|*.*.*.*) fail "release version is invalid" 2 ;;
esac

for name in release-manifest.json deployment-contract.json installer-materials.tar checksums.txt; do
  destination="$STAGING/$name"
  case "$SOURCE" in
    github)
      gh release download "$VERSION" --repo "$REPOSITORY" --pattern "$name" --dir "$STAGING" --clobber >/dev/null \
        || fail "GitHub transport failed: $name" 69
      ;;
    official-mirror)
      curl -fL --proto '=https' --tlsv1.2 --max-redirs 0 --output "$destination.part" \
        "$OFFICIAL_MIRROR_ROOT/$VERSION/$name" || fail "Official Mirror transport failed: $name" 69
      [ -s "$destination.part" ] || fail "Official Mirror returned an empty object: $name" 69
      mv "$destination.part" "$destination"
      ;;
  esac
  [ -f "$destination" ] && [ ! -L "$destination" ] || fail "transport object is not a regular file: $name" 65
done

(
  cd "$STAGING"
  sha256sum -c checksums.txt >/dev/null
) || fail "AUTHORITY_CHECKSUM_MISMATCH" 65

WORKFLOW=".github/workflows/release.yml"
for name in release-manifest.json deployment-contract.json installer-materials.tar; do
  gh attestation verify "$STAGING/$name" \
    --repo "$REPOSITORY" \
    --cert-identity "https://github.com/$REPOSITORY/$WORKFLOW@refs/heads/main" \
    --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
    >/dev/null || fail "AUTHORITY_ATTESTATION_INVALID: $name" 65
done

MATERIAL_ROOT="$STAGING/materials"
python3 - "$STAGING/installer-materials.tar" "$MATERIAL_ROOT" <<'PY'
import os
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.mkdir(mode=0o700)
with tarfile.open(archive, mode="r:") as bundle:
    members = bundle.getmembers()
    if not members:
        raise SystemExit("installer materials are empty")
    seen = set()
    for member in members:
        pure = pathlib.PurePosixPath(member.name)
        if pure.is_absolute() or ".." in pure.parts or member.name in seen:
            raise SystemExit("installer materials path is unsafe")
        if not member.isfile():
            raise SystemExit("installer materials contain a non-file entry")
        seen.add(member.name)
    bundle.extractall(target, members=members, filter="data")
for path in target.rglob("*"):
    if path.is_symlink():
        raise SystemExit("installer materials contain a link")
PY

[ -f "$MATERIAL_ROOT/installer/__main__.py" ] || fail "canonical Installer is unavailable" 65
[ -d "$MATERIAL_ROOT/wheelhouse" ] || fail "offline wheelhouse is unavailable" 65

python3 -m venv "$STAGING/venv" || fail "bootstrap environment creation failed" 69
"$STAGING/venv/bin/python" -m pip install --no-index --find-links "$MATERIAL_ROOT/wheelhouse" \
  -r "$MATERIAL_ROOT/durability/requirements.txt" -r "$MATERIAL_ROOT/release/requirements.txt" >/dev/null \
  || fail "offline bootstrap dependencies are incomplete" 69

PYTHONPATH="$MATERIAL_ROOT" "$STAGING/venv/bin/python" -m installer install \
  --version "$VERSION" \
  --source "$SOURCE" \
  --public-origin "$PUBLIC_ORIGIN"
}

main "$@"
