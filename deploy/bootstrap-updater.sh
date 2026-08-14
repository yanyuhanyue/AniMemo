#!/usr/bin/env sh
set -eu

FIXED_STATE_ROOT=/var/lib/animemo-updater
FIXED_MANIFEST=$FIXED_STATE_ROOT/bootstrap/release-manifest.json

die() {
    echo "AniMemo Updater bootstrap: $*" >&2
    exit 1
}

usage() {
    echo "Usage: sudo sh deploy/bootstrap-updater.sh RELEASE_MANIFEST_JSON" >&2
}

[ "$(id -u)" -eq 0 ] || die "run as root (sudo is fine)"
[ "$#" -eq 1 ] || { usage; exit 2; }
[ -f "$1" ] || die "release manifest not found: $1"
[ -x /usr/local/bin/animemo-updater ] || die "AniMemo Updater is not installed"

SOURCE=$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")
install -o animemo-updater -g animemo-api -m 0600 "$SOURCE" "$FIXED_MANIFEST"
runuser \
    -u animemo-updater \
    -g animemo-api \
    -G docker \
    -- /usr/local/bin/animemo-updater import-current
systemctl restart animemo-updater.service
echo "AniMemo CURRENT identity imported once; no application containers were changed."
