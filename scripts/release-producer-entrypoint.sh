#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${GITHUB_WORKSPACE:-}" || "$GITHUB_WORKSPACE" != /* ]]; then
  echo "release producer workspace authority is absent or invalid" >&2
  exit 2
fi
if [[ -z "${RUNNER_TEMP:-}" || "$RUNNER_TEMP" != /* ]]; then
  echo "release producer runner temporary authority is absent or invalid" >&2
  exit 2
fi
expected_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"
if [[ -z "${GOTMPDIR:-}" || "$GOTMPDIR" != "$expected_gotmp" || \
      ! -d "$GOTMPDIR" || -L "$GOTMPDIR" || ! -O "$GOTMPDIR" || \
      "$(stat -c '%a' "$GOTMPDIR")" != "700" ]]; then
  echo "release producer Go temporary authority is absent or invalid" >&2
  exit 2
fi
cmp /opt/animemo-locks/release.requirements.lock \
  "$GITHUB_WORKSPACE/release/requirements.lock"
cmp /opt/animemo-locks/release-producer.Dockerfile \
  "$GITHUB_WORKSPACE/deploy/release-producer.Dockerfile"
exec "$@"
