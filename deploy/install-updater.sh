#!/usr/bin/env sh
set -eu

# Installs only the AniMemo host Update Agent. It does not deploy AniMemo,
# import CURRENT, restart Docker, or touch any unrelated host service.

INSTALL_ROOT=/opt/animemo-updater
LAUNCHER=/usr/local/bin/animemo-updater
ANIMEMO_LAUNCHER=/usr/local/bin/animemo

die() {
    echo "AniMemo Updater install: $*" >&2
    exit 1
}

[ "$#" -eq 2 ] && [ "$1" = "--instance" ] || die "--instance NAME is required"
INSTANCE=$2
case "$INSTANCE" in
    api|web|postgres|redis|updater|root|system|instances|current|previous|releases|bootstrap|cache|runtime)
        die "reserved instance name" ;;
    *[!a-z0-9-]*|-[a-z0-9-]*|*[a-z0-9-]-|"") die "invalid instance name" ;;
esac
[ "${INSTANCE#?}" != "$INSTANCE" ] || die "invalid instance name"
case "${INSTANCE%${INSTANCE#?}}" in [a-z]) ;; *) die "invalid instance name" ;; esac
[ "${#INSTANCE}" -le 32 ] || die "invalid instance name"
STATE_ROOT=/var/lib/animemo-updater/instances/$INSTANCE
RUNTIME_ROOT=/run/animemo-updater/$INSTANCE
SERVICE=animemo-updater@$INSTANCE.service
[ "$(id -u)" -eq 0 ] || die "run as root (sudo is fine)"

for command in /usr/bin/docker /usr/bin/python3 runuser systemctl systemd-sysusers systemd-tmpfiles install; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done

SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
[ -f "$SCRIPT_ROOT/updater/__init__.py" ] || die "updater package is missing"
[ -f "$SCRIPT_ROOT/release/requirements.lock" ] || die "release requirements lock is missing"
[ -f "$SCRIPT_ROOT/durability/requirements.lock" ] || die "durability requirements lock is missing"
[ -d "$SCRIPT_ROOT/wheelhouse" ] || die "offline wheelhouse is missing"
[ -f "$SCRIPT_ROOT/deploy/updater/animemo-updater" ] || die "verified launcher is missing"
[ -f "$SCRIPT_ROOT/deploy/updater/animemo" ] || die "verified operator launcher is missing"
[ -f "$SCRIPT_ROOT/durability/backup_cli.py" ] || die "production backup CLI is missing"
[ -f "$SCRIPT_ROOT/durability/backup_production.py" ] || die "production backup adapter is missing"
[ -f "$SCRIPT_ROOT/deploy/updater/animemo-updater@.service" ] || die "systemd service asset is missing"
if [ -e "$ANIMEMO_LAUNCHER" ] || [ -L "$ANIMEMO_LAUNCHER" ]; then
    [ -L "$ANIMEMO_LAUNCHER" ] \
        && [ "$(readlink -- "$ANIMEMO_LAUNCHER")" = "$INSTALL_ROOT/animemo-launcher" ] \
        || die "operator launcher path is foreign"
fi

VERSION=$(sed -n 's/^__version__ = "\([0-9][0-9.]*\)"$/\1/p' "$SCRIPT_ROOT/updater/__init__.py")
case "$VERSION" in
    ""|*[!0-9.]*) die "invalid updater version" ;;
esac

RELEASE_ROOT=$INSTALL_ROOT/releases/$VERSION
STAGING=$INSTALL_ROOT/.install-$VERSION-$$
PREVIOUS_TARGET=
if [ -L "$INSTALL_ROOT/current" ]; then
    PREVIOUS_TARGET=$(readlink -f "$INSTALL_ROOT/current")
fi

cleanup() {
    status=$?
    if [ "$status" -ne 0 ] && [ -n "$PREVIOUS_TARGET" ] && [ -d "$PREVIOUS_TARGET" ]; then
        ln -sfn "$PREVIOUS_TARGET" "$INSTALL_ROOT/.current-rollback"
        mv -Tf "$INSTALL_ROOT/.current-rollback" "$INSTALL_ROOT/current"
        systemctl restart "$SERVICE" >/dev/null 2>&1 || true
    fi
    rm -rf "$STAGING"
    exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p "$INSTALL_ROOT/releases"
chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$SCRIPT_ROOT/updater" "$STAGING/updater"
cp -R "$SCRIPT_ROOT/release" "$STAGING/release"
cp -R "$SCRIPT_ROOT/durability" "$STAGING/durability"
cp -R "$SCRIPT_ROOT/installer" "$STAGING/installer"
cp -R "$SCRIPT_ROOT/wheelhouse" "$STAGING/wheelhouse"
find "$STAGING" -type d -name __pycache__ -prune -exec rm -rf {} +
PYTHONPATH="$STAGING" PYTHONSAFEPATH=1 \
    /usr/bin/python3 -P -B -m installer.offline_python_runtime \
    --wheelhouse "$STAGING/wheelhouse" --target "$STAGING/.runtime"
(cd "$STAGING" && PYTHONPATH="$STAGING/.runtime:$STAGING" PYTHONSAFEPATH=1 \
    /usr/bin/python3 -P -B -m updater version >/dev/null)
chmod -R a+rX,go-w "$STAGING"

if [ -e "$RELEASE_ROOT" ]; then
    die "updater release already exists: $RELEASE_ROOT"
fi
mv "$STAGING" "$RELEASE_ROOT"
mkdir -p "$STATE_ROOT" "$RUNTIME_ROOT"

install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater.sysusers.conf" /usr/lib/sysusers.d/animemo-updater.conf
install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater.tmpfiles.conf" /usr/lib/tmpfiles.d/animemo-updater.conf
install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater@.service" /etc/systemd/system/animemo-updater@.service
systemd-sysusers /usr/lib/sysusers.d/animemo-updater.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/animemo-updater.conf

install -m 0755 "$SCRIPT_ROOT/deploy/updater/animemo-updater" "$INSTALL_ROOT/launcher"
install -m 0755 "$SCRIPT_ROOT/deploy/updater/animemo" "$INSTALL_ROOT/animemo-launcher"
ln -sfn "$INSTALL_ROOT/launcher" "$LAUNCHER"
ln -sfn "$INSTALL_ROOT/animemo-launcher" "$ANIMEMO_LAUNCHER"
ln -sfn "$RELEASE_ROOT" "$INSTALL_ROOT/.current-new"
mv -Tf "$INSTALL_ROOT/.current-new" "$INSTALL_ROOT/current"
runuser -u animemo-updater -g animemo-api -- "$LAUNCHER" version >/dev/null \
    || die "installed updater is not executable by the service identity"
runuser -u animemo-updater -g animemo-api -- "$ANIMEMO_LAUNCHER" backup --help >/dev/null \
    || die "installed production backup CLI is not executable by the service identity"

systemctl daemon-reload

trap - EXIT INT TERM
rm -rf "$STAGING"
echo "AniMemo Updater $VERSION installed but not started. Canonical adoption and locator publication are still required."
