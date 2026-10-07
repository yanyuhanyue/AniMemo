#!/usr/bin/env bash
# Source this file before manual commands to keep generated files in this project.
ANIMEMO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ANIMEMO_LOCAL="$ANIMEMO_ROOT/.local"
mkdir -p "$ANIMEMO_LOCAL"/{tmp,logs,output,bin,tools,data,cache/npm,cache/go-build,cache/go-mod,cache/go}
export NPM_CONFIG_CACHE="$ANIMEMO_LOCAL/cache/npm"
export GOCACHE="$ANIMEMO_LOCAL/cache/go-build"
export GOMODCACHE="$ANIMEMO_LOCAL/cache/go-mod"
export GOPATH="$ANIMEMO_LOCAL/cache/go"
export GOENV=off GOTOOLCHAIN=local GOTELEMETRY=off
export TMPDIR="$ANIMEMO_LOCAL/tmp" TMP="$ANIMEMO_LOCAL/tmp" TEMP="$ANIMEMO_LOCAL/tmp"
# Docker must retain the host's registry credentials and managed proxy defaults.
if [[ -x "$ANIMEMO_LOCAL/tools/go/bin/go" ]]; then
  export PATH="$ANIMEMO_LOCAL/tools/go/bin:$PATH"
fi
export PLAYWRIGHT_BROWSERS_PATH="$ANIMEMO_LOCAL/cache/browsers"
