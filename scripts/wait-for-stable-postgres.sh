#!/usr/bin/env bash

set -euo pipefail

if [[ "$#" -ne 0 ]]; then
  echo "PLATFORM_POSTGRES_READINESS_ARGUMENTS_INVALID" >&2
  exit 2
fi
: "${QUALIFICATION_POSTGRES_NAME:?PLATFORM_POSTGRES_CONTAINER_REQUIRED}"
: "${PGPASSWORD:?PLATFORM_POSTGRES_PASSWORD_REQUIRED}"

readonly host="127.0.0.1"
readonly port="55432"
readonly username="qualification"
readonly database="postgres"
ready_streak=0

for attempt in {1..60}; do
  if pg_isready --host "$host" --port "$port" \
      --username "$username" --dbname "$database" --timeout 2 >/dev/null &&
    PGCONNECT_TIMEOUT=2 timeout \
      --foreground --signal=TERM --kill-after=2s 5s psql \
      --host "$host" --port "$port" \
      --username "$username" --dbname "$database" --no-password \
      --tuples-only --no-align --command 'SELECT 1' | grep -qx '1'; then
    ready_streak=$((ready_streak + 1))
    if [[ "$ready_streak" -eq 3 ]]; then
      break
    fi
  else
    ready_streak=0
  fi
  sleep 2
done

if [[ "$ready_streak" -ne 3 ]]; then
  docker logs "$QUALIFICATION_POSTGRES_NAME"
  exit 1
fi

for target in qualification_source qualification_target; do
  if ! PGCONNECT_TIMEOUT=2 timeout \
    --foreground --signal=TERM --kill-after=2s 5s createdb \
    --host "$host" --port "$port" \
    --username "$username" --no-password "$target"; then
    docker logs "$QUALIFICATION_POSTGRES_NAME"
    exit 1
  fi
done
