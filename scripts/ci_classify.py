#!/usr/bin/env python3
"""Classify changed paths into the CI risk gates used by GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import PurePosixPath


OUTPUT_NAMES = (
    "docs_only",
    "frontend",
    "backend",
    "auth",
    "api_contract",
    "plugin",
    "integration",
    "bridge",
    "migration",
    "dependencies",
    "ci",
    "deployment",
    "shared_contract",
    "mixed",
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
    "run_release_full",
    "full_gate",
)


def _is_docs(path: str) -> bool:
    lower = path.lower()
    return (
        lower.startswith("docs/")
        or lower.endswith(".md")
        or lower.endswith(".mdx")
        or lower.endswith(".rst")
    )


def _has(path: str, *parts: str) -> bool:
    return any(part in path for part in parts)


def classify_paths(paths: list[str], *, force_full: bool = False) -> dict[str, str]:
    normalized = sorted({str(PurePosixPath(path.replace("\\", "/"))) for path in paths if path})
    non_docs = [path for path in normalized if not _is_docs(path)]

    frontend = any(
        path.startswith(("src/", "public/"))
        or PurePosixPath(path).name in {
            "package.json",
            "package-lock.json",
            "vite.config.js",
            "vite.config.mjs",
            "vite.config.ts",
            "playwright.config.js",
            "playwright.config.mjs",
            "playwright.config.ts",
            "eslint.config.js",
            "eslint.config.mjs",
            "eslint.config.ts",
        }
        for path in non_docs
    )
    backend = any(path.startswith("backend/") for path in non_docs)
    auth = any(
        _has(path.lower(), "auth", "security", "csrf", "token", "registration", "accounts")
        for path in non_docs
    )
    api_contract = any(
        _has(path.lower(), "openapi", "schema", "serializer", "urls.py", "/api/", "api_contract")
        for path in non_docs
    )
    plugin = any(
        path.startswith(("plugins/", "backend/plugin_host/"))
        or _has(path.lower(), "pluginctl", "plugin-manifest", "plugin_sdk")
        for path in non_docs
    )
    integration = any(
        path.startswith("backend/integrations/")
        or _has(path.lower(), "integration-protocol", "/integrations/", "integration")
        for path in non_docs
    )
    bridge = any(
        path.startswith("bridges/")
        or _has(path.lower(), "astrbot", "bridge")
        for path in non_docs
    )
    migration = any(
        "/migrations/" in f"/{path}"
        or path.endswith("/models.py")
        or path.endswith("schema.sql")
        for path in non_docs
    )
    dependencies = any(
        PurePosixPath(path).name in {
            "package.json",
            "package-lock.json",
            "requirements.in",
            "requirements.txt",
            "requirements-tools.txt",
            "pyproject.toml",
            "uv.lock",
        }
        or path.endswith(("/requirements.txt", "/requirements.in"))
        for path in non_docs
    )
    ci = any(
        path.startswith(".github/")
        or path.startswith("scripts/ci")
        or path in {"scripts/ci_classify.py", "scripts/requirements-ci.txt"}
        for path in non_docs
    )
    deployment = any(
        path.startswith(("deploy/", "release/", "updater/"))
        or path.startswith("scripts/tests/test_release")
        or path.startswith("scripts/tests/test_updater")
        or PurePosixPath(path).name in {"Dockerfile", "docker-compose.yml", "docker-compose.yaml"}
        or _has(path.lower(), "stateful-upgrade", "release-gate")
        for path in non_docs
    )
    shared_contract = any(
        path in {
            "backend/journal/domain_services.py",
            "backend/journal/mutation_ports.py",
            "backend/plugin_host/sdk/types.py",
            "backend/plugin_host/sdk/runtime.py",
        }
        or path.startswith("docs/contracts/")
        or _has(path.lower(), "shared-contract", "public-contract")
        for path in non_docs
    )
    media_storage = any("media_storage" in path.lower() or "test_media_storage" in path.lower() for path in non_docs)

    signals = [
        frontend,
        backend,
        auth,
        api_contract,
        plugin,
        integration,
        bridge,
        migration,
        dependencies,
        ci,
        deployment,
        shared_contract,
    ]
    mixed = sum(bool(signal) for signal in signals) > 1
    full_gate = force_full or any(
        (ci, dependencies, deployment, auth, api_contract, migration, shared_contract)
    ) or (frontend and backend) or (backend and (plugin or bridge or integration))
    docs_only = bool(normalized) and not non_docs
    if not normalized:
        docs_only = False
        full_gate = True

    run_frontend = frontend or full_gate
    run_backend = backend or auth or api_contract or migration or integration or shared_contract or full_gate
    run_bootstrap = ci or deployment or full_gate
    run_plugins = plugin or integration or shared_contract or full_gate
    run_bridge = bridge or integration or shared_contract or full_gate
    run_postgres = auth or api_contract or migration or integration or shared_contract or media_storage or full_gate
    run_runtime = run_bridge or full_gate

    result = {
        "docs_only": docs_only,
        "frontend": frontend,
        "backend": backend,
        "auth": auth,
        "api_contract": api_contract,
        "plugin": plugin,
        "integration": integration,
        "bridge": bridge,
        "migration": migration,
        "dependencies": dependencies,
        "ci": ci,
        "deployment": deployment,
        "shared_contract": shared_contract,
        "mixed": mixed,
        "run_frontend": run_frontend,
        "run_backend": run_backend,
        "run_bootstrap": run_bootstrap,
        "run_plugins": run_plugins,
        "run_bridge": run_bridge,
        "run_postgres": run_postgres,
        "run_runtime": run_runtime,
        "run_release_full": full_gate,
        "full_gate": full_gate,
    }
    return {name: "true" if result[name] else "false" for name in OUTPUT_NAMES}


def changed_paths(base: str | None, head: str | None) -> list[str]:
    if base and base != "0" * 40 and head:
        command = ["git", "diff", "--name-only", f"{base}...{head}"]
    elif head:
        command = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", head]
    else:
        command = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def write_outputs(result: dict[str, str], output_path: str) -> None:
    with open(output_path, "a", encoding="utf-8") as output:
        for name, value in result.items():
            output.write(f"{name}={value}\n")


def self_test() -> None:
    cases = {
        ("docs_only",): ("docs_only", ["docs/ci.md", "README.md"]),
        ("frontend", "run_frontend"): ("frontend", ["src/App.jsx"]),
        ("backend", "run_backend"): ("backend", ["backend/journal/services.py"]),
        ("plugin", "run_plugins"): ("plugin", ["plugins/watch-history-importer/manifest.json"]),
        ("bridge", "run_bridge"): ("bridge", ["bridges/astrbot_plugin_animemo_bridge/plugin.py"]),
        ("auth", "run_postgres", "full_gate"): ("auth", ["backend/accounts/authentication.py"]),
        ("api_contract", "full_gate"): ("api", ["backend/journal/serializers_entries.py"]),
        ("migration", "full_gate"): ("migration", ["backend/journal/migrations/0002_add.py"]),
        ("ci", "full_gate"): ("ci", [".github/workflows/ci.yml"]),
        ("dependencies", "full_gate"): ("dependencies", ["backend/requirements.in"]),
        ("deployment", "full_gate"): ("release", ["release/contract.py"]),
        ("mixed", "full_gate"): ("mixed", ["src/App.jsx", "backend/journal/services.py"]),
    }
    for expected, (label, paths) in cases.items():
        result = classify_paths(paths)
        for key in expected:
            assert result[key] == "true", f"{label}: expected {key}=true, got {result}"
    assert classify_paths(["docs/ci.md"])["frontend"] == "false"
    assert classify_paths(["updater/agent.py"])["deployment"] == "true"
    print("ci_classify self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--github-output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    force_full = os.getenv("GITHUB_EVENT_NAME") in {"merge_group", "workflow_dispatch"}
    paths = args.files if args.files else changed_paths(args.base, args.head)
    result = classify_paths(paths, force_full=force_full)
    result["changed_files"] = json.dumps(sorted(paths), ensure_ascii=False)
    if args.github_output:
        write_outputs(result, args.github_output)
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
