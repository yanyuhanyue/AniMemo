#!/usr/bin/env sh
set -eu

COMPOSE_FILE=${ANIME_JOURNAL_COMPOSE_FILE:-deploy/docker-compose.yml}
ENV_FILE=${ANIME_JOURNAL_ENV_FILE:-.env.production}
PORT=${ANIME_JOURNAL_PORT:-8088}
BASE_URL=${ANIME_JOURNAL_BASE_URL:-http://127.0.0.1:$PORT}
WAIT_SECONDS=${ANIME_JOURNAL_SMOKE_WAIT_SECONDS:-120}

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for the production smoke test." >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for the production smoke test." >&2
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "Missing production environment file: $ENV_FILE" >&2
    exit 1
fi

compose() {
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

http_get_200() {
    url=$1
    shift
    if ! response=$(curl -sS -w '\n%{http_code}' "$@" "$url"); then
        echo "HTTP request failed: $url" >&2
        return 1
    fi
    HTTP_STATUS=$(printf '%s\n' "$response" | tail -n 1)
    HTTP_BODY=$(printf '%s\n' "$response" | sed '$d')
    if [ "$HTTP_STATUS" != "200" ]; then
        echo "Expected HTTP 200 from $url, got $HTTP_STATUS." >&2
        return 1
    fi
}

PUBLIC_HOST=${ANIME_JOURNAL_PUBLIC_HOST:-}
if [ -z "$PUBLIC_HOST" ]; then
    frontend_url=$(sed -n 's/^FRONTEND_URL=//p' "$ENV_FILE" | tail -n 1 | tr -d "\"'")
    case "$frontend_url" in
        http://*|https://*)
            PUBLIC_HOST=${frontend_url#*://}
            PUBLIC_HOST=${PUBLIC_HOST%%/*}
            ;;
        *)
            echo "Set FRONTEND_URL in $ENV_FILE or ANIME_JOURNAL_PUBLIC_HOST." >&2
            exit 1
            ;;
    esac
fi

wait_for_service() {
    service=$1
    elapsed=0
    while [ "$elapsed" -lt "$WAIT_SECONDS" ]; do
        container_id=$(compose ps -q "$service")
        if [ -n "$container_id" ]; then
            state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")
            if [ "$state" = "healthy" ]; then
                echo "$service: healthy"
                return 0
            fi
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "$service did not become healthy within $WAIT_SECONDS seconds." >&2
    compose ps >&2
    return 1
}

for service in postgres redis api web; do
    wait_for_service "$service"
done

http_get_200 "$BASE_URL/" -H "Host: $PUBLIC_HOST" -H "X-Forwarded-Proto: https"
http_get_200 "$BASE_URL/health/" -H "Host: $PUBLIC_HOST" -H "X-Forwarded-Proto: https"
public_health_body=$HTTP_BODY
compose exec -T -e HEALTH_BODY="$public_health_body" api python -c "import json, os; payload=json.loads(os.environ['HEALTH_BODY']); assert payload.get('status') == 'ok'"
compose exec -T api python -c "import http.client, json, os; host=os.environ['ALLOWED_HOSTS'].split(',')[0].strip(); connection=http.client.HTTPConnection('127.0.0.1', 8000, timeout=3); connection.request('GET', '/health/', headers={'Host': host, 'X-Forwarded-Proto': 'https'}); response=connection.getresponse(); payload=json.loads(response.read()); assert response.status == 200 and payload.get('status') == 'ok'"
echo "web/api HTTP health: PASS"

compose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
compose exec -T redis redis-cli ping | grep -q '^PONG$'
compose exec -T api python manage.py shell -c "from django.core.cache import cache; from django.db import connection; connection.ensure_connection(); cache.set('release-smoke', 'ok', 30); assert cache.get('release-smoke') == 'ok'" >/dev/null
echo "PostgreSQL/Redis application connectivity: PASS"

SMOKE_KEY="smoke-tests/release-$(date +%s)-$$.txt"
cleanup_local_media() {
    compose exec -T -e SMOKE_OBJECT_KEY="$SMOKE_KEY" api python manage.py shell -c "import os; from site_config.media_storage.local import DynamicLocalBackend; from site_config.models import MediaStorageBackend; backend=MediaStorageBackend(backend_type=MediaStorageBackend.BackendType.LOCAL, local_root='', local_public_base_url='/local-media'); DynamicLocalBackend(backend).delete(os.environ['SMOKE_OBJECT_KEY'])" >/dev/null 2>&1 || true
}
trap cleanup_local_media 0 1 2 15

compose exec -T -e SMOKE_OBJECT_KEY="$SMOKE_KEY" api python manage.py shell -c "import os, stat; from site_config.media_storage.local import DynamicLocalBackend; from site_config.models import MediaStorageBackend; backend=MediaStorageBackend(backend_type=MediaStorageBackend.BackendType.LOCAL, local_root='', local_public_base_url='/local-media'); adapter=DynamicLocalBackend(backend); key=os.environ['SMOKE_OBJECT_KEY']; adapter.write(key, b'anime-journal-smoke', content_type='text/plain'); path=adapter.path_for(key); assert stat.S_IMODE(path.stat().st_mode) == 0o644; assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755" >/dev/null
http_get_200 "$BASE_URL/local-media/$SMOKE_KEY" -H "Host: $PUBLIC_HOST" -H "X-Forwarded-Proto: https"
if [ "$HTTP_BODY" != "anime-journal-smoke" ]; then
    echo "Local media smoke response did not match the written object." >&2
    exit 1
fi
cleanup_local_media
trap - 0 1 2 15
echo "Local media write, permissions, Nginx read and cleanup: PASS"
echo "Anime Journal production smoke test: PASS"
