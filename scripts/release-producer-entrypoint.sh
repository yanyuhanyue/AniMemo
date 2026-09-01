#!/usr/bin/env bash
set -euo pipefail

fail_workspace() {
  echo "release producer workspace authority is absent or invalid" >&2
  exit 2
}

fail_import() {
  echo "release producer repository import authority is invalid" >&2
  exit 2
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
if [[ "$runner_temp_real" != "$RUNNER_TEMP" ]]; then
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
  scripts/formal_windows_pretrust.py
  scripts/platform_qualification.py
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
