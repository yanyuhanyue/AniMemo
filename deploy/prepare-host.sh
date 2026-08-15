#!/usr/bin/env sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root / sudo: sudo sh deploy/prepare-host.sh" >&2
    exit 1
fi

DATA_ROOT=${ANIMEMO_DATA_ROOT:-/data/animemo}
APP_UID=10001
APP_GID=10001

case "$DATA_ROOT" in
    /*) ;;
    *)
        echo "ANIMEMO_DATA_ROOT must be an absolute path." >&2
        exit 1
        ;;
esac
if [ "$DATA_ROOT" = "/" ]; then
    echo "ANIMEMO_DATA_ROOT must not be /." >&2
    exit 1
fi

umask 022
mkdir -p "$DATA_ROOT" "$DATA_ROOT/postgres" "$DATA_ROOT/redis"
chmod 0755 "$DATA_ROOT" "$DATA_ROOT/postgres" "$DATA_ROOT/redis"

for name in plugins logs media; do
    directory="$DATA_ROOT/$name"
    mkdir -p "$directory"
    chown -R "$APP_UID:$APP_GID" "$directory"
    chmod 0755 "$directory"
done

backup_directory="$DATA_ROOT/backups"
if [ -L "$backup_directory" ]; then
    echo "Backup path must not be a symbolic link: $backup_directory" >&2
    exit 1
fi
if [ -e "$backup_directory" ] && [ ! -d "$backup_directory" ]; then
    echo "Backup path must be a directory: $backup_directory" >&2
    exit 1
fi
mkdir -p "$backup_directory"
chown "$APP_UID:$APP_GID" "$backup_directory"
chmod 0770 "$backup_directory"

private_directory="$DATA_ROOT/private"
if [ -L "$private_directory" ]; then
    echo "First-run private state path must not be a symbolic link: $private_directory" >&2
    exit 1
fi
if [ -e "$private_directory" ] && [ ! -d "$private_directory" ]; then
    echo "First-run private state path must be a directory: $private_directory" >&2
    exit 1
fi
mkdir -p "$private_directory"
chown "$APP_UID:$APP_GID" "$private_directory"
chmod 0700 "$private_directory"

echo "AniMemo host directories are ready under $DATA_ROOT."
echo "Writable API directories use owner $APP_UID:$APP_GID and mode 0755."
echo "Updater backup directory uses owner $APP_UID:$APP_GID and mode 0770."
echo "First-run private state uses owner $APP_UID:$APP_GID and mode 0700."
