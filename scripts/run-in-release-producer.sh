#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ! "${ANIMEMO_RELEASE_PRODUCER_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "release producer image identity is absent or invalid" >&2
  exit 2
fi
if [[ -z "${GITHUB_WORKSPACE:-}" || -z "${RUNNER_TEMP:-}" || \
      "$GITHUB_WORKSPACE" != /* || "$RUNNER_TEMP" != /* || \
      "$GITHUB_WORKSPACE" == *$'\n'* || "$GITHUB_WORKSPACE" == *$'\r'* || \
      "$GITHUB_WORKSPACE" == *,* || \
      "$RUNNER_TEMP" == *$'\n'* || "$RUNNER_TEMP" == *$'\r'* || \
      "$RUNNER_TEMP" == *,* ]]; then
  echo "release producer mount authority is absent" >&2
  exit 2
fi
if [[ ! -d "$GITHUB_WORKSPACE" || -L "$GITHUB_WORKSPACE" || \
      ! -d "$RUNNER_TEMP" || -L "$RUNNER_TEMP" ]]; then
  echo "release producer mount authority is invalid" >&2
  exit 2
fi
workspace_real="$(realpath -e -- "$GITHUB_WORKSPACE" 2>/dev/null)" || {
  echo "release producer workspace authority is invalid" >&2
  exit 2
}
runner_temp_real="$(realpath -e -- "$RUNNER_TEMP" 2>/dev/null)" || {
  echo "release producer runner temporary authority is invalid" >&2
  exit 2
}
wrapper_file="$(realpath -e -- "${BASH_SOURCE[0]}" 2>/dev/null)" || {
  echo "release producer wrapper authority is invalid" >&2
  exit 2
}
wrapper_root="$(realpath -e -- "$(dirname -- "$wrapper_file")/.." 2>/dev/null)" || {
  echo "release producer wrapper authority is invalid" >&2
  exit 2
}
if [[ "$workspace_real" != "$GITHUB_WORKSPACE" || \
      "$workspace_real" != "$wrapper_root" || \
      "$wrapper_file" != "$workspace_real/scripts/run-in-release-producer.sh" || \
      "$runner_temp_real" != "$RUNNER_TEMP" ]]; then
  echo "release producer mount authority is not canonical" >&2
  exit 2
fi
if [[ "$workspace_real" == "$runner_temp_real" || \
      "$workspace_real" == "$runner_temp_real/"* || \
      "$runner_temp_real" == "$workspace_real/"* ]]; then
  echo "release producer mount authority overlaps" >&2
  exit 2
fi
test "$(docker image inspect --format '{{.Id}}' "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID")" = \
  "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID"

allowed='^(API_DIGEST|API_REPOSITORY|BUILDX_NODES|CANDIDATE_SHA|CHANNEL|CREATED_AT|CRANE_REQUIRED_VERSION|DRY_RUN_ARTIFACT_DIGEST|DRY_RUN_ARTIFACT_ID|EVENT_NAME|GH_TOKEN|GITHUB_REPOSITORY|GITHUB_RUN_ATTEMPT|GITHUB_RUN_ID|GITHUB_SHA|ImageOS|ImageVersion|NEEDS_JSON|OPERATION|PLATFORM_ARTIFACT_DIGEST|PLATFORM_ARTIFACT_ID|POSTGRES_IMAGE|PREVIOUS_STABLE|QUALIFICATION_ARTIFACT_PATH|REDIS_IMAGE|RELEASE_NOTES_IDENTITY_FILE|RELEASE_NOTES_MARKDOWN_FILE|RELEASE_TAG|RUNNER_ARCH|RUNNER_OS|RUN_ATTEMPT|RUN_ID|TARGET_VERSION|UPGRADE_BASE_SHA|WEB_DIGEST|WEB_REPOSITORY|WORKFLOW_REF|WORKFLOW_SHA)$'
environment=()
while [[ $# -gt 0 && "$1" != "--" ]]; do
  name="$1"
  shift
  [[ "$name" =~ $allowed ]] || {
    echo "release producer environment name is not allowlisted" >&2
    exit 2
  }
  [[ -v "$name" ]] || {
    echo "release producer required environment is absent" >&2
    exit 2
  }
  environment+=(--env "$name")
done
[[ $# -gt 1 && "$1" = "--" ]] || {
  echo "release producer command boundary is invalid" >&2
  exit 2
}
shift

fail_session() {
  echo "release producer Go session authority is invalid" >&2
  exit 2
}

validate_session_root() {
  local candidate="$1"
  local candidate_identity candidate_real candidate_parent candidate_name
  local mountpoint_status

  [[ -n "$candidate" && "$candidate" == /* && -d "$candidate" && \
     ! -L "$candidate" && -O "$candidate" ]] || return 1
  candidate_real="$(realpath -e -- "$candidate" 2>/dev/null)" || return 1
  [[ "$candidate_real" == "$candidate" ]] || return 1
  candidate_parent="$(dirname -- "$candidate")"
  candidate_name="$(basename -- "$candidate")"
  [[ "$candidate_parent" == "$runner_temp_real" && \
     "$candidate_name" =~ ^animemo-release-producer-session\.[A-Za-z0-9]{10}$ && \
     "$(stat -c '%a' -- "$candidate" 2>/dev/null)" == "700" ]] || return 1
  if [[ -n "$producer_session_identity" ]]; then
    candidate_identity="$(stat -c '%d:%i' -- "$candidate" 2>/dev/null)" || \
      return 1
    [[ "$candidate_identity" == "$producer_session_identity" ]] || return 1
  fi
  if mountpoint -q -- "$candidate" 2>/dev/null; then
    return 1
  else
    mountpoint_status=$?
  fi
  (( mountpoint_status == 32 ))
}

validate_private_child() {
  local candidate="$1"
  local candidate_real first_entry

  [[ "$candidate" == "$producer_session/"* && -d "$candidate" && \
     ! -L "$candidate" && -O "$candidate" ]] || return 1
  candidate_real="$(realpath -e -- "$candidate" 2>/dev/null)" || return 1
  [[ "$candidate_real" == "$candidate" && \
     "$(dirname -- "$candidate")" == "$producer_session" && \
     "$(stat -c '%a' -- "$candidate" 2>/dev/null)" == "700" ]] || return 1
  first_entry="$(
    find -P "$candidate" -xdev -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null
  )" || return 1
  [[ -z "$first_entry" ]]
}

ensure_output_directory() {
  local candidate="$1"
  local candidate_real

  if [[ -e "$candidate" || -L "$candidate" ]]; then
    [[ -d "$candidate" && ! -L "$candidate" ]] || fail_session
  else
    install -d -m 0700 -- "$candidate" || fail_session
  fi
  candidate_real="$(realpath -e -- "$candidate" 2>/dev/null)" || fail_session
  [[ "$candidate_real" == "$candidate" && \
     "$(dirname -- "$candidate")" == "$runner_temp_real" && \
     -O "$candidate" && \
     "$(stat -c '%a' -- "$candidate" 2>/dev/null)" == "700" ]] || fail_session
}

command -v mountpoint >/dev/null 2>&1 || fail_session
producer_session=""
producer_session_identity=""

cleanup_session() {
  local command_status=$?
  local cleanup_failed=0
  trap - EXIT HUP INT TERM
  set +e

  if validate_session_root "$producer_session"; then
    if find -P "$producer_session" -xdev -type d \
      -exec chmod u+rwx -- '{}' \;; then
      if validate_session_root "$producer_session"; then
        rm -rf --one-file-system -- "$producer_session" || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    else
      cleanup_failed=1
    fi
    if (( cleanup_failed == 0 )) && \
       [[ -e "$producer_session" || -L "$producer_session" ]]; then
      cleanup_failed=1
    fi
  else
    cleanup_failed=1
  fi
  if (( cleanup_failed != 0 )); then
    echo "release producer Go session cleanup failed" >&2
    if (( command_status == 0 )); then
      command_status=70
    fi
  fi
  exit "$command_status"
}

producer_session="$(
  mktemp -d -- "$RUNNER_TEMP/animemo-release-producer-session.XXXXXXXXXX"
)" || fail_session
trap cleanup_session EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

producer_session_identity="$(
  stat -c '%d:%i' -- "$producer_session" 2>/dev/null
)" || fail_session
validate_session_root "$producer_session" || fail_session
private_children=(
  home
  xdg-cache
  xdg-config
  xdg-data
  xdg-state
  go-path
  go-module-cache
  go-build-cache
  go-tmp
  runtime-output
)
for child in "${private_children[@]}"; do
  install -d -m 0700 -- "$producer_session/$child" || fail_session
  validate_private_child "$producer_session/$child" || fail_session
done

producer_home="$producer_session/home"
producer_xdg_cache="$producer_session/xdg-cache"
producer_xdg_config="$producer_session/xdg-config"
producer_xdg_data="$producer_session/xdg-data"
producer_xdg_state="$producer_session/xdg-state"
producer_gopath="$producer_session/go-path"
producer_gomodcache="$producer_session/go-module-cache"
producer_gocache="$producer_session/go-build-cache"
producer_gotmp="$producer_session/go-tmp"
producer_release_output="$RUNNER_TEMP/animemo-release-producer-output"
producer_qualification_output="$RUNNER_TEMP/animemo-release-qualification-output"
ensure_output_directory "$producer_release_output"
ensure_output_directory "$producer_qualification_output"

command_status=0
docker run --rm --init --interactive --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777 \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$GITHUB_WORKSPACE,dst=$GITHUB_WORKSPACE" \
  --mount "type=bind,src=$RUNNER_TEMP,dst=$RUNNER_TEMP" \
  --mount "type=bind,src=$producer_release_output,dst=$GITHUB_WORKSPACE/release-output" \
  --mount "type=bind,src=$producer_qualification_output,dst=$GITHUB_WORKSPACE/release-qualification" \
  --workdir "$GITHUB_WORKSPACE" \
  --env "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT=$producer_session" \
  --env "HOME=$producer_home" \
  --env "XDG_CACHE_HOME=$producer_xdg_cache" \
  --env "XDG_CONFIG_HOME=$producer_xdg_config" \
  --env "XDG_DATA_HOME=$producer_xdg_data" \
  --env "XDG_STATE_HOME=$producer_xdg_state" \
  --env "GH_CONFIG_DIR=$producer_xdg_config/gh" \
  --env "GOPATH=$producer_gopath" \
  --env "GOMODCACHE=$producer_gomodcache" \
  --env "GOCACHE=$producer_gocache" \
  --env "GOTMPDIR=$producer_gotmp" \
  --env "GOENV=off" \
  --env "GOTOOLCHAIN=local" \
  --env "GOWORK=off" \
  --env "GOPROXY=https://proxy.golang.org,direct" \
  --env "GOSUMDB=sum.golang.org" \
  --env "GOPRIVATE=" \
  --env "GONOSUMDB=" \
  --env "GONOPROXY=" \
  --env "GOINSECURE=" \
  --env "GOFLAGS=" \
  --env "RUNNER_TEMP=$RUNNER_TEMP" \
  --env "GITHUB_WORKSPACE=$GITHUB_WORKSPACE" \
  --env "PYTHONNOUSERSITE=1" \
  --env "PYTHONSAFEPATH=1" \
  --env "PYTHONPATH=$GITHUB_WORKSPACE" \
  --env "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID=$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID" \
  "${environment[@]}" \
  "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID" "$@" || command_status=$?
exit "$command_status"
