#!/usr/bin/env sh
set -eu

# Installs only the AniMemo host Update Agent. It does not deploy AniMemo,
# import CURRENT, restart Docker, or touch any unrelated host service.

INSTALL_ROOT=/opt/animemo-updater
STATE_ROOT=/var/lib/animemo-updater
RUNTIME_ROOT=/run/animemo-updater
LAUNCHER=/usr/local/bin/animemo-updater
SERVICE=animemo-updater.service

die() {
    echo "AniMemo Updater install: $*" >&2
    exit 1
}

[ "$#" -eq 0 ] || die "this installer accepts no custom paths or commands"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo is fine)"

for command in /usr/bin/docker /usr/bin/gh python3 systemctl systemd-sysusers systemd-tmpfiles install; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done

SCRIPT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
[ -f "$SCRIPT_ROOT/updater/__init__.py" ] || die "updater package is missing"
[ -f "$SCRIPT_ROOT/release/requirements.txt" ] || die "release requirements are missing"
[ -f "$SCRIPT_ROOT/deploy/updater/animemo-updater.service" ] || die "systemd service asset is missing"

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
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$SCRIPT_ROOT/updater" "$STAGING/updater"
cp -R "$SCRIPT_ROOT/release" "$STAGING/release"
find "$STAGING" -type d -name __pycache__ -prune -exec rm -rf {} +
python3 -m venv "$STAGING/.venv" || die "python3-venv is required"
"$STAGING/.venv/bin/python" -m pip install --disable-pip-version-check -r "$STAGING/release/requirements.txt"
(cd "$STAGING" && "$STAGING/.venv/bin/python" -m updater version >/dev/null)

if [ -e "$RELEASE_ROOT" ]; then
    die "updater release already exists: $RELEASE_ROOT"
fi
mv "$STAGING" "$RELEASE_ROOT"
mkdir -p "$STATE_ROOT" "$RUNTIME_ROOT"

install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater.sysusers.conf" /usr/lib/sysusers.d/animemo-updater.conf
install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater.tmpfiles.conf" /usr/lib/tmpfiles.d/animemo-updater.conf
install -m 0644 "$SCRIPT_ROOT/deploy/updater/animemo-updater.service" /etc/systemd/system/animemo-updater.service
systemd-sysusers /usr/lib/sysusers.d/animemo-updater.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/animemo-updater.conf

cat > "$INSTALL_ROOT/launcher" <<'EOF'
#!/usr/bin/env sh
set -eu
cd /opt/animemo-updater/current
exec /opt/animemo-updater/current/.venv/bin/python -m updater "$@"
EOF
chmod 0755 "$INSTALL_ROOT/launcher"
ln -sfn "$INSTALL_ROOT/launcher" "$LAUNCHER"
ln -sfn "$RELEASE_ROOT" "$INSTALL_ROOT/.current-new"
mv -Tf "$INSTALL_ROOT/.current-new" "$INSTALL_ROOT/current"

systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl is-active --quiet "$SERVICE" || die "animemo-updater did not become active"

trap - EXIT INT TERM
rm -rf "$STAGING"
echo "AniMemo Updater $VERSION installed. CURRENT was not imported and production was not changed."
