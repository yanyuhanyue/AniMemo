#!/usr/bin/env sh
set -eu

COMPOSE_FILE=${ANIME_JOURNAL_COMPOSE_FILE:-deploy/docker-compose.yml}
ENV_FILE=${ANIME_JOURNAL_ENV_FILE:-.env.production}
USERNAME=${ANIME_JOURNAL_ADMIN_USERNAME:-animeadmin}
EMAIL=${ANIME_JOURNAL_ADMIN_EMAIL:-admin@re-anime.cc}
PASSWORD=${ANIME_JOURNAL_ADMIN_PASSWORD:-}
PASSWORD_FILE=${ANIME_JOURNAL_ADMIN_PASSWORD_FILE:-/root/anime-journal-initial-admin.txt}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root / sudo: sudo sh deploy/create-admin.sh" >&2
    exit 1
fi
command -v docker >/dev/null 2>&1 || { echo "docker is required." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Missing production environment file: $ENV_FILE" >&2; exit 1; }

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

if [ -z "$PASSWORD" ] && [ -f "$PASSWORD_FILE" ]; then
    stored_username=$(sed -n 's/^username=//p' "$PASSWORD_FILE" | tail -n 1)
    stored_email=$(sed -n 's/^email=//p' "$PASSWORD_FILE" | tail -n 1)
    stored_password=$(sed -n 's/^password=//p' "$PASSWORD_FILE" | tail -n 1)
    if [ -n "$stored_password" ]; then
        [ -z "$stored_username" ] || USERNAME=$stored_username
        [ -z "$stored_email" ] || EMAIL=$stored_email
        PASSWORD=$stored_password
    fi
fi

if [ -z "$PASSWORD" ]; then
    if command -v openssl >/dev/null 2>&1; then
        PASSWORD=$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9' | cut -c 1-32)
    else
        PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    fi
fi
[ "${#PASSWORD}" -ge 16 ] || { echo "Admin password must be at least 16 characters." >&2; exit 1; }

result=$(compose exec -T \
    -e AJ_ADMIN_USERNAME="$USERNAME" \
    -e AJ_ADMIN_EMAIL="$EMAIL" \
    -e AJ_ADMIN_PASSWORD="$PASSWORD" \
    api python manage.py shell -c "import os; from accounts.models import User; username=os.environ['AJ_ADMIN_USERNAME']; email=os.environ['AJ_ADMIN_EMAIL']; password=os.environ['AJ_ADMIN_PASSWORD']; user, created=User.objects.get_or_create(username=username, defaults={'email': email}); print('created' if created else 'exists'); user.email=email; user.is_active=True; user.is_staff=True; user.is_superuser=True; user.set_password(password) if created else None; user.save(update_fields=['email','is_active','is_staff','is_superuser','password'] if created else ['email','is_active','is_staff','is_superuser'])")
printf '%s\n' "$result"

umask 077
case "$result" in
    *created*)
        if [ ! -e "$PASSWORD_FILE" ]; then
            printf 'Anime Journal initial admin\nusername=%s\nemail=%s\npassword=%s\n' "$USERNAME" "$EMAIL" "$PASSWORD" > "$PASSWORD_FILE"
            chmod 0600 "$PASSWORD_FILE"
            echo "Initial admin created; credentials saved to $PASSWORD_FILE"
        else
            echo "Initial admin created; existing credentials file was left untouched at $PASSWORD_FILE"
        fi
        ;;
    *exists*)
        echo "Admin $USERNAME already exists; password was not reset."
        ;;
    *)
        echo "Could not determine whether the admin was created." >&2
        exit 1
        ;;
esac
