#!/usr/bin/env bash
set -euo pipefail
umask 077

mode=""
workspace=""
session_root=""
runtime_output=""
module_root=""
formal_parent=""
linux_target=""
windows_target=""
go_mod_before=""
go_sum_before=""

fail_runtime() {
  exit 2
}

hash_file() {
  sha256sum -- "$1" 2>/dev/null | awk '{print $1}'
}

validate_module_root() {
  local resolved
  [[ -n "$module_root" && -d "$module_root" && ! -L "$module_root" ]] || \
    return 1
  resolved="$(realpath -e -- "$module_root" 2>/dev/null)" || return 1
  [[ "$resolved" == "$module_root" && \
     "$module_root" == "$workspace/release/release_attestation_verifier" ]]
}

validate_formal_parent() {
  local resolved
  [[ -n "$formal_parent" && -d "$formal_parent" && \
     ! -L "$formal_parent" && -O "$formal_parent" ]] || return 1
  resolved="$(realpath -e -- "$formal_parent" 2>/dev/null)" || return 1
  [[ "$resolved" == "$formal_parent" && \
     "$formal_parent" == "$workspace/release-output/.formal-pretrust-work" && \
     "$(stat -c '%a' -- "$formal_parent" 2>/dev/null)" == "700" ]]
}

module_inputs_unchanged() {
  local go_mod_after go_sum_after
  [[ -n "$go_mod_before" && -n "$go_sum_before" ]] || return 1
  go_mod_after="$(hash_file "$module_root/go.mod")" || return 1
  go_sum_after="$(hash_file "$module_root/go.sum")" || return 1
  [[ "$go_mod_after" == "$go_mod_before" && \
     "$go_sum_after" == "$go_sum_before" ]]
}

finish_runtime() {
  local status=$?
  local cleanup_failed=0
  local session_link=""
  trap - EXIT
  set +e

  if [[ -n "$go_mod_before" ]] && ! module_inputs_unchanged; then
    status=2
  fi
  if [[ -n "$session_root" && -d "$session_root" && ! -L "$session_root" ]]; then
    session_link="$(
      find -P "$session_root" -xdev -type l -print -quit 2>/dev/null
    )"
    if [[ $? -ne 0 || -n "$session_link" ]]; then
      status=2
    fi
  elif [[ -n "$session_root" ]]; then
    status=2
  fi

  if (( status != 0 )) && [[ "$mode" == "build-attestation-verifier" ]]; then
    if [[ -n "$linux_target" && ( -e "$linux_target" || -L "$linux_target" ) ]]; then
      if validate_module_root; then
        rm -f -- "$linux_target" || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    fi
    if [[ -n "$windows_target" && \
          ( -e "$windows_target" || -L "$windows_target" ) ]]; then
      if validate_formal_parent; then
        rm -f -- "$windows_target" || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    fi
  fi
  if (( cleanup_failed != 0 )); then
    status=2
  fi

  if (( status == 0 )); then
    printf '%s\n' 'release producer runtime readiness PASS'
    exit 0
  fi
  printf '%s\n' 'release producer runtime readiness FAIL' >&2
  exit 2
}
trap finish_runtime EXIT

case "${1:-}" in
  check)
    [[ $# -eq 1 ]] || fail_runtime
    mode="check"
    ;;
  build-attestation-verifier)
    [[ $# -eq 1 ]] || fail_runtime
    mode="build-attestation-verifier"
    ;;
  *)
    fail_runtime
    ;;
esac

workspace="${GITHUB_WORKSPACE:-}"
runner_temp="${RUNNER_TEMP:-}"
session_root="${ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT:-}"
[[ -n "$workspace" && "$workspace" == /* && -d "$workspace" && \
   ! -L "$workspace" && "$(realpath -e -- "$workspace" 2>/dev/null)" == \
   "$workspace" ]] || fail_runtime
[[ -n "$runner_temp" && "$runner_temp" == /* && -d "$runner_temp" && \
   ! -L "$runner_temp" && \
   "$(realpath -e -- "$runner_temp" 2>/dev/null)" == "$runner_temp" && \
   "$workspace" != "$runner_temp" && "$workspace" != "$runner_temp/"* && \
   "$runner_temp" != "$workspace/"* ]] || fail_runtime
[[ -n "$session_root" && "$session_root" == /* && -d "$session_root" && \
   ! -L "$session_root" && -O "$session_root" && \
   "$(realpath -e -- "$session_root" 2>/dev/null)" == "$session_root" && \
   "$(dirname -- "$session_root")" == "$runner_temp" && \
   "$(basename -- "$session_root")" =~ \
     ^animemo-release-producer-session\.[A-Za-z0-9]{10}$ && \
   "$(stat -c '%a' -- "$session_root" 2>/dev/null)" == "700" ]] || \
  fail_runtime

state_names=(
  HOME XDG_CACHE_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME
  GOPATH GOMODCACHE GOCACHE GOTMPDIR
)
state_suffixes=(
  home xdg-cache xdg-config xdg-data xdg-state
  go-path go-module-cache go-build-cache go-tmp
)
for state_index in "${!state_names[@]}"; do
  state_name="${state_names[$state_index]}"
  state_path="${!state_name:-}"
  expected_state_path="$session_root/${state_suffixes[$state_index]}"
  [[ "$state_path" == "$expected_state_path" && -d "$state_path" && \
     ! -L "$state_path" && -O "$state_path" && \
     "$(realpath -e -- "$state_path" 2>/dev/null)" == "$state_path" && \
     "$(stat -c '%a' -- "$state_path" 2>/dev/null)" == "700" ]] || \
    fail_runtime
done

runtime_output="$session_root/runtime-output"
[[ -d "$runtime_output" && ! -L "$runtime_output" && -O "$runtime_output" && \
   "$(realpath -e -- "$runtime_output" 2>/dev/null)" == "$runtime_output" && \
   "$(dirname -- "$runtime_output")" == "$session_root" && \
   "$(stat -c '%a' -- "$runtime_output" 2>/dev/null)" == "700" ]] || \
  fail_runtime
runtime_entry="$(
  find -P "$runtime_output" -xdev -mindepth 1 -maxdepth 1 -print -quit \
    2>/dev/null
)" || fail_runtime
[[ -z "$runtime_entry" ]] || fail_runtime

[[ "${GOENV:-}" == "off" && "${GOTOOLCHAIN:-}" == "local" && \
   "${GOWORK:-}" == "off" && \
   "${GOPROXY:-}" == "https://proxy.golang.org,direct" && \
   "${GOSUMDB:-}" == "sum.golang.org" && \
   -v GOPRIVATE && -z "$GOPRIVATE" && \
   -v GONOSUMDB && -z "$GONOSUMDB" && \
   -v GONOPROXY && -z "$GONOPROXY" && \
   -v GOINSECURE && -z "$GOINSECURE" && \
   -v GOFLAGS && -z "$GOFLAGS" && \
   ! -v GOTELEMETRY && ! -v GOTELEMETRYDIR ]] || fail_runtime
go_binary="/usr/local/go/bin/go"
[[ "$(command -v go 2>/dev/null)" == "$go_binary" && \
   "$(realpath -e -- "$go_binary" 2>/dev/null)" == "$go_binary" && \
   -x "$go_binary" && ! -L "$go_binary" ]] || fail_runtime
[[ "$($go_binary env GOTELEMETRY 2>/dev/null)" == "off" ]] || fail_runtime

module_root="$workspace/release/release_attestation_verifier"
validate_module_root || fail_runtime
module_link="$(
  find -P "$module_root" -xdev -type l -print -quit 2>/dev/null
)" || fail_runtime
[[ -z "$module_link" ]] || fail_runtime
for required_file in go.mod go.sum main.go; do
  candidate="$module_root/$required_file"
  [[ -f "$candidate" && ! -L "$candidate" && \
     "$(realpath -e -- "$candidate" 2>/dev/null)" == "$candidate" ]] || \
    fail_runtime
done

go_mod_before="$(hash_file "$module_root/go.mod")" || fail_runtime
go_sum_before="$(hash_file "$module_root/go.sum")" || fail_runtime

check_linux_relative="runtime-output/offline-release-verifier"
check_windows_relative="runtime-output/formal-release-verifier.exe"
build_linux_relative="release/release_attestation_verifier/offline-release-verifier"
build_windows_relative="release-output/.formal-pretrust-work/formal-release-verifier.exe"
if [[ "$mode" == "check" ]]; then
  linux_target="$session_root/$check_linux_relative"
  windows_target="$session_root/$check_windows_relative"
else
  linux_target="$workspace/$build_linux_relative"
  windows_target="$workspace/$build_windows_relative"
  formal_parent="$workspace/release-output/.formal-pretrust-work"
  validate_formal_parent || fail_runtime
fi
[[ ! -e "$linux_target" && ! -L "$linux_target" && \
   ! -e "$windows_target" && ! -L "$windows_target" ]] || fail_runtime

cd -- "$module_root"
[[ "$($go_binary env GOMOD 2>/dev/null)" == "$module_root/go.mod" ]] || \
  fail_runtime
[[ "$($go_binary env GOWORK 2>/dev/null)" == "off" ]] || fail_runtime

if ! GOPROXY=https://proxy.golang.org,direct GOSUMDB=sum.golang.org \
  /usr/local/go/bin/go mod download >"$runtime_output/download.log" 2>&1; then
  fail_runtime
fi
module_inputs_unchanged || fail_runtime
if ! GOPROXY=off GOSUMDB=off \
  /usr/local/go/bin/go mod verify >"$runtime_output/verify.log" 2>&1; then
  fail_runtime
fi
if ! GOPROXY=off GOSUMDB=off \
  /usr/local/go/bin/go test -mod=readonly ./... \
    >"$runtime_output/test.log" 2>&1; then
  fail_runtime
fi
if ! CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOPROXY=off GOSUMDB=off \
  /usr/local/go/bin/go build -mod=readonly -trimpath -o "$linux_target" . \
    >"$runtime_output/build-linux.log" 2>&1; then
  fail_runtime
fi
if ! CGO_ENABLED=0 GOOS=windows GOARCH=amd64 GOPROXY=off GOSUMDB=off \
  /usr/local/go/bin/go build -mod=readonly -trimpath -o "$windows_target" . \
    >"$runtime_output/build-windows.log" 2>&1; then
  fail_runtime
fi

[[ -f "$linux_target" && ! -L "$linux_target" && -s "$linux_target" && \
   -x "$linux_target" ]] || fail_runtime
[[ -f "$windows_target" && ! -L "$windows_target" && \
   -s "$windows_target" ]] || fail_runtime
if ! GOPROXY=off GOSUMDB=off /usr/local/go/bin/go version -m "$linux_target" \
  >"$runtime_output/linux-metadata.log" 2>&1; then
  fail_runtime
fi
if ! GOPROXY=off GOSUMDB=off /usr/local/go/bin/go version -m "$windows_target" \
  >"$runtime_output/windows-metadata.log" 2>&1; then
  fail_runtime
fi
grep -F $'build\tCGO_ENABLED=0' "$runtime_output/linux-metadata.log" >/dev/null || \
  fail_runtime
grep -F $'build\tGOOS=linux' "$runtime_output/linux-metadata.log" >/dev/null || \
  fail_runtime
grep -F $'build\tGOARCH=amd64' "$runtime_output/linux-metadata.log" >/dev/null || \
  fail_runtime
grep -F $'build\tCGO_ENABLED=0' "$runtime_output/windows-metadata.log" >/dev/null || \
  fail_runtime
grep -F $'build\tGOOS=windows' "$runtime_output/windows-metadata.log" >/dev/null || \
  fail_runtime
grep -F $'build\tGOARCH=amd64' "$runtime_output/windows-metadata.log" >/dev/null || \
  fail_runtime

for log_file in "$runtime_output"/*.log; do
  [[ -f "$log_file" && ! -L "$log_file" && -O "$log_file" && \
     "$(stat -c '%a' -- "$log_file" 2>/dev/null)" == "600" ]] || \
    fail_runtime
done
module_inputs_unchanged || fail_runtime
session_link="$(
  find -P "$session_root" -xdev -type l -print -quit 2>/dev/null
)" || fail_runtime
[[ -z "$session_link" ]] || fail_runtime
