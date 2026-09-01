#!/usr/bin/env bash
set -euo pipefail
umask 077

fail_workspace() {
  echo "release producer workspace authority is absent or invalid" >&2
  exit 2
}

fail_import() {
  echo "release producer repository import authority is invalid" >&2
  exit 2
}

fail_session() {
  echo "release producer Go session authority is invalid" >&2
  exit 2
}

fail_go_state() {
  echo "release producer Go writable state is invalid" >&2
  exit 2
}

fail_go_supply() {
  echo "release producer Go supply-chain environment is invalid" >&2
  exit 2
}

fail_go_module() {
  echo "release producer Go module authority is invalid" >&2
  exit 2
}

require_not_mountpoint() {
  local candidate="$1"
  local mountpoint_status
  if mountpoint -q -- "$candidate" 2>/dev/null; then
    return 1
  else
    mountpoint_status=$?
  fi
  (( mountpoint_status == 32 ))
}

validate_output_staging() {
  local target="$1"
  local source="$2"
  local target_real source_real

  [[ -d "$target" && ! -L "$target" && -O "$target" && \
     -d "$source" && ! -L "$source" && -O "$source" ]] || return 1
  target_real="$(realpath -e -- "$target" 2>/dev/null)" || return 1
  source_real="$(realpath -e -- "$source" 2>/dev/null)" || return 1
  [[ "$target_real" == "$target" && "$source_real" == "$source" && \
     "$(stat -c '%a' -- "$target" 2>/dev/null)" == "700" && \
     "$(stat -c '%a' -- "$source" 2>/dev/null)" == "700" && \
     "$(stat -c '%d:%i' -- "$target" 2>/dev/null)" == \
       "$(stat -c '%d:%i' -- "$source" 2>/dev/null)" ]] || return 1
  mountpoint -q -- "$target" 2>/dev/null || return 1
  require_not_mountpoint "$source"
}

if [[ -z "${GITHUB_WORKSPACE:-}" || "$GITHUB_WORKSPACE" != /* || \
      "$GITHUB_WORKSPACE" == *$'\n'* || "$GITHUB_WORKSPACE" == *$'\r'* || \
      "$GITHUB_WORKSPACE" == *,* || ! -d "$GITHUB_WORKSPACE" || \
      -L "$GITHUB_WORKSPACE" ]]; then
  fail_workspace
fi
workspace_real="$(realpath -e -- "$GITHUB_WORKSPACE" 2>/dev/null)" || fail_workspace
if [[ "$workspace_real" != "$GITHUB_WORKSPACE" || \
      "$(pwd -P)" != "$GITHUB_WORKSPACE" ]]; then
  fail_workspace
fi
if [[ -z "${RUNNER_TEMP:-}" || "$RUNNER_TEMP" != /* || \
      "$RUNNER_TEMP" == *$'\n'* || "$RUNNER_TEMP" == *$'\r'* || \
      "$RUNNER_TEMP" == *,* || ! -d "$RUNNER_TEMP" || -L "$RUNNER_TEMP" ]]; then
  echo "release producer runner temporary authority is absent or invalid" >&2
  exit 2
fi
runner_temp_real="$(realpath -e -- "$RUNNER_TEMP" 2>/dev/null)" || {
  echo "release producer runner temporary authority is absent or invalid" >&2
  exit 2
}
if [[ "$runner_temp_real" != "$RUNNER_TEMP" || \
      "$workspace_real" == "$runner_temp_real" || \
      "$workspace_real" == "$runner_temp_real/"* || \
      "$runner_temp_real" == "$workspace_real/"* ]]; then
  echo "release producer runner temporary authority is absent or invalid" >&2
  exit 2
fi

command -v mountpoint >/dev/null 2>&1 || fail_session
session_root="${ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT:-}"
if [[ -z "$session_root" || "$session_root" != /* || \
      "$session_root" == *$'\n'* || "$session_root" == *$'\r'* || \
      "$session_root" == *,* || ! -d "$session_root" || \
      -L "$session_root" || ! -O "$session_root" ]]; then
  fail_session
fi
session_real="$(realpath -e -- "$session_root" 2>/dev/null)" || fail_session
session_parent="$(dirname -- "$session_root")"
session_name="$(basename -- "$session_root")"
if [[ "$session_real" != "$session_root" || \
      "$session_parent" != "$runner_temp_real" || \
      ! "$session_name" =~ ^animemo-release-producer-session\.[A-Za-z0-9]{10}$ || \
      "$(stat -c '%a' -- "$session_root" 2>/dev/null)" != "700" ]]; then
  fail_session
fi
require_not_mountpoint "$session_root" || fail_session

writable_names=(
  HOME
  XDG_CACHE_HOME
  XDG_CONFIG_HOME
  XDG_DATA_HOME
  XDG_STATE_HOME
  GOPATH
  GOMODCACHE
  GOCACHE
  GOTMPDIR
)
writable_suffixes=(
  home
  xdg-cache
  xdg-config
  xdg-data
  xdg-state
  go-path
  go-module-cache
  go-build-cache
  go-tmp
)
for index in "${!writable_names[@]}"; do
  variable_name="${writable_names[$index]}"
  actual_path="${!variable_name:-}"
  expected_path="$session_root/${writable_suffixes[$index]}"
  [[ "$actual_path" == "$expected_path" && -d "$actual_path" && \
     ! -L "$actual_path" && -O "$actual_path" ]] || fail_go_state
  actual_real="$(realpath -e -- "$actual_path" 2>/dev/null)" || fail_go_state
  [[ "$actual_real" == "$actual_path" && \
     "$(dirname -- "$actual_path")" == "$session_root" && \
     "$(stat -c '%a' -- "$actual_path" 2>/dev/null)" == "700" ]] || \
    fail_go_state
  require_not_mountpoint "$actual_path" || fail_go_state
  initial_entry="$(
    find -P "$actual_path" -xdev -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null
  )" || fail_go_state
  [[ -z "$initial_entry" ]] || fail_go_state
done
shopt -s nullglob dotglob
session_entries=("$session_root"/*)
shopt -u nullglob dotglob
[[ ${#session_entries[@]} -eq 10 ]] || fail_go_state
runtime_output="$session_root/runtime-output"
[[ -d "$runtime_output" && ! -L "$runtime_output" && \
   -O "$runtime_output" ]] || fail_go_state
runtime_output_real="$(realpath -e -- "$runtime_output" 2>/dev/null)" || \
  fail_go_state
[[ "$runtime_output_real" == "$runtime_output" && \
   "$(dirname -- "$runtime_output")" == "$session_root" && \
   "$(stat -c '%a' -- "$runtime_output" 2>/dev/null)" == "700" ]] || \
  fail_go_state
require_not_mountpoint "$runtime_output" || fail_go_state
initial_runtime_entry="$(
  find -P "$runtime_output" -xdev -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null
)" || fail_go_state
[[ -z "$initial_runtime_entry" ]] || fail_go_state

producer_output="$workspace_real/release-output"
qualification_output="$workspace_real/release-qualification"
producer_output_source="$runner_temp_real/animemo-release-producer-output"
qualification_output_source="$runner_temp_real/animemo-release-qualification-output"
validate_output_staging "$producer_output" "$producer_output_source" || \
  fail_go_state
validate_output_staging "$qualification_output" "$qualification_output_source" || \
  fail_go_state
[[ -d /go && ! -L /go && ! -w /go && \
   -d /root && ! -L /root && ! -w /root ]] || fail_go_state

expected_gh_config="$XDG_CONFIG_HOME/gh"
[[ "${GH_CONFIG_DIR:-}" == "$expected_gh_config" && \
   ! -e "$GH_CONFIG_DIR" && ! -L "$GH_CONFIG_DIR" ]] || fail_go_state

if [[ "${GOENV:-}" != "off" || \
      "${GOTOOLCHAIN:-}" != "local" || \
      "${GOWORK:-}" != "off" || \
      "${GOPROXY:-}" != "https://proxy.golang.org,direct" || \
      "${GOSUMDB:-}" != "sum.golang.org" || \
      ! -v GOPRIVATE || -n "$GOPRIVATE" || \
      ! -v GONOSUMDB || -n "$GONOSUMDB" || \
      ! -v GONOPROXY || -n "$GONOPROXY" || \
      ! -v GOINSECURE || -n "$GOINSECURE" || \
      ! -v GOFLAGS || -n "$GOFLAGS" || \
      -v GOTELEMETRY || -v GOTELEMETRYDIR ]]; then
  fail_go_supply
fi

go_command="$(command -v go 2>/dev/null)" || fail_go_state
go_binary="$(realpath -e -- "$go_command" 2>/dev/null)" || fail_go_state
[[ "$go_command" == "/usr/local/go/bin/go" && \
   "$go_binary" == "/usr/local/go/bin/go" && \
   -x "$go_binary" && ! -L "$go_binary" ]] || fail_go_state

# This is intentionally the first Go process: Go 1.26.6 handles the telemetry
# mode command before opening counters, so all later probes inherit mode off.
/usr/local/go/bin/go telemetry off >/dev/null 2>&1 || fail_go_state
go_version="$($go_binary version 2>/dev/null)" || fail_go_state
[[ "$go_version" == "go version go1.26.6 linux/amd64" ]] || fail_go_state

assert_go_env() {
  local field="$1"
  local expected="$2"
  local failure="$3"
  local actual
  actual="$($go_binary env "$field" 2>/dev/null)" || "$failure"
  [[ "$actual" == "$expected" ]] || "$failure"
}

assert_go_env GOROOT /usr/local/go fail_go_state
assert_go_env GOHOSTOS linux fail_go_state
assert_go_env GOHOSTARCH amd64 fail_go_state
assert_go_env GOPATH "$GOPATH" fail_go_state
assert_go_env GOMODCACHE "$GOMODCACHE" fail_go_state
assert_go_env GOCACHE "$GOCACHE" fail_go_state
assert_go_env GOTMPDIR "$GOTMPDIR" fail_go_state
assert_go_env GOENV off fail_go_state
assert_go_env GOTOOLCHAIN local fail_go_state
assert_go_env GOWORK off fail_go_state
assert_go_env GOPROXY https://proxy.golang.org,direct fail_go_supply
assert_go_env GOSUMDB sum.golang.org fail_go_supply
assert_go_env GOPRIVATE "" fail_go_supply
assert_go_env GONOSUMDB "" fail_go_supply
assert_go_env GONOPROXY "" fail_go_supply
assert_go_env GOINSECURE "" fail_go_supply
assert_go_env GOFLAGS "" fail_go_supply
assert_go_env GOTELEMETRY off fail_go_state
expected_telemetry_dir="$XDG_CONFIG_HOME/go/telemetry"
assert_go_env GOTELEMETRYDIR "$expected_telemetry_dir" fail_go_state

telemetry_go_dir="$XDG_CONFIG_HOME/go"
telemetry_mode_file="$expected_telemetry_dir/mode"
for telemetry_directory in "$telemetry_go_dir" "$expected_telemetry_dir"; do
  [[ -d "$telemetry_directory" && ! -L "$telemetry_directory" && \
     -O "$telemetry_directory" ]] || fail_go_state
  telemetry_real="$(realpath -e -- "$telemetry_directory" 2>/dev/null)" || \
    fail_go_state
  [[ "$telemetry_real" == "$telemetry_directory" && \
     "$telemetry_directory" == "$XDG_CONFIG_HOME/"* && \
     "$(stat -c '%a' -- "$telemetry_directory" 2>/dev/null)" == "700" ]] || \
    fail_go_state
done
[[ -f "$telemetry_mode_file" && ! -L "$telemetry_mode_file" && \
   -O "$telemetry_mode_file" && \
   "$(stat -c '%a' -- "$telemetry_mode_file" 2>/dev/null)" == "600" ]] || \
  fail_go_state
telemetry_mode="$(<"$telemetry_mode_file")" || fail_go_state
[[ "$telemetry_mode" =~ ^off[[:space:]][0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || \
  fail_go_state
[[ ! -e "$XDG_CONFIG_HOME/go/env" && ! -L "$XDG_CONFIG_HOME/go/env" ]] || \
  fail_go_state
session_link="$(
  find -P "$session_root" -xdev -type l -print -quit 2>/dev/null
)" || fail_go_state
[[ -z "$session_link" ]] || fail_go_state

go_module_root="$GITHUB_WORKSPACE/release/release_attestation_verifier"
[[ -d "$go_module_root" && ! -L "$go_module_root" ]] || fail_go_module
go_module_real="$(realpath -e -- "$go_module_root" 2>/dev/null)" || \
  fail_go_module
[[ "$go_module_real" == "$go_module_root" ]] || fail_go_module
module_link="$(
  find -P "$go_module_root" -xdev -type l -print -quit 2>/dev/null
)" || fail_go_module
[[ -z "$module_link" ]] || fail_go_module
for module_file in go.mod go.sum main.go; do
  module_candidate="$go_module_root/$module_file"
  [[ -f "$module_candidate" && ! -L "$module_candidate" ]] || fail_go_module
  module_candidate_real="$(realpath -e -- "$module_candidate" 2>/dev/null)" || \
    fail_go_module
  [[ "$module_candidate_real" == "$module_candidate" ]] || fail_go_module
done

if [[ "${PYTHONSAFEPATH:-}" != "1" || \
      "${PYTHONNOUSERSITE:-}" != "1" || \
      "${PYTHONPATH:-}" != "$GITHUB_WORKSPACE" ]]; then
  fail_import
fi

required_directories=(release scripts updater durability installer)
required_files=(
  release/__init__.py
  release/cli.py
  release/producer_toolchain.py
  release/requirements.lock
  release/release_attestation_verifier/go.mod
  release/release_attestation_verifier/go.sum
  release/release_attestation_verifier/main.go
  scripts/formal_windows_pretrust.py
  scripts/platform_qualification.py
  scripts/release-producer-runtime-readiness.sh
  scripts/release_authority.py
  deploy/release-producer.Dockerfile
)
for relative in "${required_directories[@]}"; do
  candidate="$GITHUB_WORKSPACE/$relative"
  [[ -d "$candidate" && ! -L "$candidate" ]] || fail_import
  candidate_real="$(realpath -e -- "$candidate" 2>/dev/null)" || fail_import
  [[ "$candidate_real" = "$candidate" ]] || fail_import
  linked_entry="$(find "$candidate" -type l -print -quit 2>/dev/null)" || fail_import
  [[ -z "$linked_entry" ]] || fail_import
done
for relative in "${required_files[@]}"; do
  candidate="$GITHUB_WORKSPACE/$relative"
  [[ -f "$candidate" && ! -L "$candidate" ]] || fail_import
  candidate_real="$(realpath -e -- "$candidate" 2>/dev/null)" || fail_import
  [[ "$candidate_real" = "$candidate" ]] || fail_import
done
root_python_shadow="$(
  find "$GITHUB_WORKSPACE" -mindepth 1 -maxdepth 1 \
    \( -name '*.py' -o -name 'sitecustomize*' -o -name 'usercustomize*' \) \
    -print -quit 2>/dev/null
)" || fail_import
[[ -z "$root_python_shadow" ]] || fail_import

if ! cmp -s /opt/animemo-locks/release.requirements.lock \
  "$GITHUB_WORKSPACE/release/requirements.lock"; then
  echo "release producer dependency authority is invalid" >&2
  exit 2
fi
if ! cmp -s /opt/animemo-locks/release-producer.Dockerfile \
  "$GITHUB_WORKSPACE/deploy/release-producer.Dockerfile"; then
  echo "release producer Dockerfile authority is invalid" >&2
  exit 2
fi

python -I -S -B <<'ANIMEMO_IMPORT_AUTHORITY' || fail_import
import importlib.util
import importlib.machinery
import os
import sys
from pathlib import Path

workspace = Path(os.environ["GITHUB_WORKSPACE"])
expected_modules = {
    "release.cli": "release/cli.py",
    "release.producer_toolchain": "release/producer_toolchain.py",
    "scripts.formal_windows_pretrust": "scripts/formal_windows_pretrust.py",
    "scripts.release_authority": "scripts/release_authority.py",
}

try:
    workspace = workspace.resolve(strict=True)
    normalized_path = []
    for raw_path in sys.path:
        if not raw_path:
            raise ValueError("unsafe empty import path")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("relative import path")
        normalized_path.append(path.resolve(strict=False))
    if normalized_path.count(workspace) != 0:
        raise ValueError("premature workspace import root")
    import_suffixes = (
        *importlib.machinery.SOURCE_SUFFIXES,
        *importlib.machinery.BYTECODE_SUFFIXES,
        *importlib.machinery.EXTENSION_SUFFIXES,
    )
    allowed_workspace_roots = {
        "backend",
        "bridges",
        "deploy",
        "docs",
        "durability",
        "installer",
        "node_modules",
        "plugins",
        "public",
        "release",
        "scripts",
        "sites",
        "src",
        "tests",
        "updater",
    }
    for candidate in workspace.iterdir():
        if candidate.name.isidentifier() and (
            candidate.is_dir() or candidate.is_symlink()
        ):
            if candidate.name not in allowed_workspace_roots:
                raise ValueError("unexpected workspace import root")
            if candidate.name == "node_modules" and (
                candidate.is_symlink()
                or not candidate.is_dir()
                or candidate.resolve(strict=True) != candidate
            ):
                raise ValueError("linked node dependency root")
        for suffix in import_suffixes:
            if candidate.name.endswith(suffix):
                import_name = candidate.name[: -len(suffix)]
                if import_name.isidentifier():
                    raise ValueError("unexpected workspace module root")
    for module_name in sys.stdlib_module_names:
        candidates = [workspace / module_name]
        candidates.extend(workspace / f"{module_name}{suffix}" for suffix in import_suffixes)
        if any(candidate.exists() or candidate.is_symlink() for candidate in candidates):
            raise ValueError("standard library shadow")
    sys.path.insert(0, str(workspace))
    normalized_path.insert(0, workspace)
    if normalized_path.count(workspace) != 1 or sys.path[0] != str(workspace):
        raise ValueError("workspace import root cardinality")

    for module_name, relative_path in expected_modules.items():
        expected = workspace / relative_path
        if expected.resolve(strict=True) != expected:
            raise ValueError("linked module authority")
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise ValueError("module spec authority")
        if Path(spec.origin).resolve(strict=True) != expected:
            raise ValueError("module spec provenance")
except Exception:
    raise SystemExit(2) from None
ANIMEMO_IMPORT_AUTHORITY

python -P -B <<'ANIMEMO_ACTIVE_IMPORT_AUTHORITY' || fail_import
import importlib
import importlib.util
import os
import sys
from pathlib import Path

workspace = Path(os.environ["GITHUB_WORKSPACE"])
expected_modules = {
    "release.cli": "release/cli.py",
    "release.producer_toolchain": "release/producer_toolchain.py",
    "scripts.formal_windows_pretrust": "scripts/formal_windows_pretrust.py",
    "scripts.release_authority": "scripts/release_authority.py",
}

try:
    workspace = workspace.resolve(strict=True)
    normalized_path = []
    for raw_path in sys.path:
        if not raw_path:
            raise ValueError("unsafe empty import path")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("relative import path")
        normalized_path.append(path.resolve(strict=False))
    if normalized_path.count(workspace) != 1:
        raise ValueError("workspace import root cardinality")
    for module_name, relative_path in expected_modules.items():
        expected = workspace / relative_path
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise ValueError("module spec authority")
        if Path(spec.origin).resolve(strict=True) != expected:
            raise ValueError("module spec provenance")
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if module_file is None or Path(module_file).resolve(strict=True) != expected:
            raise ValueError("module file provenance")

    repository_roots = {
        name: workspace / name
        for name in ("release", "scripts", "updater", "durability", "installer")
    }
    for loaded_name, loaded_module in tuple(sys.modules.items()):
        root_name = loaded_name.partition(".")[0]
        if root_name not in repository_roots:
            continue
        module_file = getattr(loaded_module, "__file__", None)
        if module_file is not None:
            Path(module_file).resolve(strict=True).relative_to(
                repository_roots[root_name]
            )
            continue
        search_locations = getattr(loaded_module, "__path__", None)
        if search_locations is None:
            raise ValueError("repository module provenance")
        normalized_locations = {
            Path(location).resolve(strict=True) for location in search_locations
        }
        if normalized_locations != {repository_roots[root_name]}:
            raise ValueError("repository namespace provenance")
except Exception:
    raise SystemExit(2) from None
ANIMEMO_ACTIVE_IMPORT_AUTHORITY
exec "$@"
