#!/usr/bin/env bash
set -euo pipefail

if [[ ! "${ANIMEMO_RELEASE_PRODUCER_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "release producer image identity is absent or invalid" >&2
  exit 2
fi
if [[ -z "${GITHUB_WORKSPACE:-}" || -z "${RUNNER_TEMP:-}" ]]; then
  echo "release producer mount authority is absent" >&2
  exit 2
fi
test -d "$GITHUB_WORKSPACE" && test ! -L "$GITHUB_WORKSPACE"
test -d "$RUNNER_TEMP" && test ! -L "$RUNNER_TEMP"
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

producer_home="$RUNNER_TEMP/animemo-release-producer-home"
producer_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"
producer_release_output="$RUNNER_TEMP/animemo-release-producer-output"
producer_qualification_output="$RUNNER_TEMP/animemo-release-qualification-output"
install -d -m 0700 "$producer_home" "$producer_gotmp"
install -d -m 0700 \
  "$producer_release_output" "$producer_qualification_output"
test -d "$producer_home" && test ! -L "$producer_home" && [[ -O "$producer_home" ]]
test -d "$producer_gotmp" && test ! -L "$producer_gotmp" && [[ -O "$producer_gotmp" ]]
test -d "$producer_release_output" && test ! -L "$producer_release_output" && \
  [[ -O "$producer_release_output" ]]
test -d "$producer_qualification_output" && \
  test ! -L "$producer_qualification_output" && \
  [[ -O "$producer_qualification_output" ]]
test "$(stat -c '%a' "$producer_home")" = "700"
test "$(stat -c '%a' "$producer_gotmp")" = "700"
test "$(stat -c '%a' "$producer_release_output")" = "700"
test "$(stat -c '%a' "$producer_qualification_output")" = "700"
docker run --rm --init --interactive --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777 \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$GITHUB_WORKSPACE,dst=$GITHUB_WORKSPACE" \
  --mount "type=bind,src=$RUNNER_TEMP,dst=$RUNNER_TEMP" \
  --mount "type=bind,src=$producer_release_output,dst=$GITHUB_WORKSPACE/release-output" \
  --mount "type=bind,src=$producer_qualification_output,dst=$GITHUB_WORKSPACE/release-qualification" \
  --workdir "$GITHUB_WORKSPACE" \
  --env "HOME=$producer_home" \
  --env "GH_CONFIG_DIR=$producer_home/gh" \
  --env "GOTMPDIR=$producer_gotmp" \
  --env "RUNNER_TEMP=$RUNNER_TEMP" \
  --env "GITHUB_WORKSPACE=$GITHUB_WORKSPACE" \
  --env "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID=$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID" \
  "${environment[@]}" \
  "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID" "$@"
