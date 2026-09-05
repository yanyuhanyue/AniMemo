#!/usr/bin/env python3
"""Classify changed paths into deterministic AniMemo CI risk gates."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

SCHEMA_VERSION = "animemo.ci-risk/v1"
RISK_LEVELS = ("LOW", "STANDARD", "HIGH", "CRITICAL")
RISK_RANK = {level: rank for rank, level in enumerate(RISK_LEVELS, start=1)}

FROZEN_CONTRACT_DOCUMENTS = frozenset(
    {
        "docs/api-v1-contract.md",
        "docs/auth-contract.md",
        "docs/external-media-identity.md",
        "docs/integration-protocol-v1.md",
        "docs/plugin-sdk-contract.md",
        "docs/plugin-sdk-v2.md",
        "docs/release-contract-v1.md",
        "docs/update-agent-v1.md",
    }
)

SIGNAL_NAMES = (
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
    "first_run",
    "recovery",
    "media_storage",
    "tooling",
)

GATE_NAMES = (
    "docs_only",
    "mixed",
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
    "run_release_full",
    "run_release_updater",
    "run_release_docker",
    "run_release_stateful",
    "full_gate",
    "critical_gate",
)

OUTPUT_NAMES = (
    "schema_version",
    "risk_level",
    "risk_rank",
    "execution_force_full",
    "reasons",
    "matched_rules",
    "unknown_paths",
    "classification_json",
    "docs_only",
    *SIGNAL_NAMES,
    "mixed",
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
    "run_release_full",
    "run_release_updater",
    "run_release_docker",
    "run_release_stateful",
    "full_gate",
    "critical_gate",
)


@dataclass(frozen=True)
class RiskRule:
    rule_id: str
    level: str
    reason: str
    categories: tuple[str, ...]
    matches: Callable[[str], bool]


def _is_docs(path: str) -> bool:
    lower = path.lower()
    return lower.startswith("docs/") or lower.endswith((".md", ".mdx", ".rst"))


def _has(path: str, *parts: str) -> bool:
    return any(part in path for part in parts)


def _name(path: str) -> str:
    return PurePosixPath(path).name.lower()


def _is_python_route_module(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith(".py"):
        return False
    stem = PurePosixPath(lower).stem
    route_stems = ("urls", "routes", "router", "routing")
    return (
        stem in route_stems
        or stem.startswith(tuple(f"{route}_" for route in route_stems))
        or stem.endswith(tuple(f"_{route}" for route in route_stems))
        or any(f"/{route}/" in f"/{lower}/" for route in route_stems)
    )


def _is_frontend_setup_route(path: str) -> bool:
    lower = path.lower()
    if not lower.startswith("src/") or PurePosixPath(lower).suffix not in {
        ".js",
        ".jsx",
        ".mjs",
        ".mts",
        ".ts",
        ".tsx",
    }:
        return False
    source_path = PurePosixPath(lower)
    stem = source_path.stem
    route_parts = {"route", "routes", "router", "routing"}
    setup_parts = {"setup", "first-run", "first_run", "firstrun"}
    root_app = lower in {
        "src/app.js",
        "src/app.jsx",
        "src/app.ts",
        "src/app.tsx",
    }
    setup_named = _has(stem, "setup", "first-run", "first_run", "firstrun")
    setup_directory = any(part in setup_parts for part in source_path.parts)
    route_module = (
        any(part in route_parts for part in source_path.parts)
        or stem in route_parts
        or stem.startswith(tuple(f"{route}_" for route in route_parts))
        or stem.endswith(tuple(f"_{route}" for route in route_parts))
    )
    return root_app or setup_directory or (setup_named and ("pages" in source_path.parts or route_module))


def _is_release_image_rehearsal(path: str) -> bool:
    lower = path.lower()
    return (
        lower.startswith(("release/", "scripts/", "tests/"))
        and "release" in lower
        and "image" in lower
        and _has(lower, "rehearse", "rehearsal")
    )


def _is_ci_authority(path: str) -> bool:
    return (
        path.startswith((".github/", "scripts/tests/test_ci_"))
        or path
        in {
            "scripts/ci_classify.py",
            "scripts/ci_gate_authority.py",
            "scripts/ci_premerge.py",
            "scripts/ci_refs.py",
            "scripts/release_authority.py",
        }
        or path
        in {
            "scripts/tests/test_release_authority.py",
            "scripts/tests/test_release_workflows.py",
        }
    )


def _is_release_core(path: str) -> bool:
    return (
        path.startswith(("release/", "scripts/tests/test_release_"))
        or _is_release_image_rehearsal(path)
        or path
        in {
            "scripts/release_authority.py",
            "scripts/tests/test_deployment_updater_contract.py",
            "tests/release-gate.test.mjs",
        }
    )


def _is_updater(path: str) -> bool:
    return path.startswith(("updater/", "deploy/updater/")) or path in {
        "backend/journal/staff_update_views.py",
        "backend/journal/test_staff_updates.py",
        "backend/journal/update_agent_client.py",
        "scripts/tests/test_deployment_updater_contract.py",
        "src/components/admin/AdminUpdatePanel.jsx",
        "src/components/admin/updatePresentation.js",
        "tests/admin-update-ui.test.mjs",
    }


def _is_deployment(path: str) -> bool:
    return (
        path.startswith("deploy/")
        or path in {".dockerignore", ".env.production.example"}
        or _name(path) in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
    )


def _is_recovery(path: str) -> bool:
    return not _is_docs(path) and _has(
        path.lower(),
        "backup",
        "restore",
        "rollback",
        "recovery",
        "disaster",
        "stateful-upgrade",
        "stateful_upgrade",
    )


def _is_first_run(path: str) -> bool:
    if _is_docs(path):
        return False
    lower = path.lower()
    site_config_entrypoint = lower.startswith("backend/site_config/") and (
        _is_python_route_module(lower)
        or PurePosixPath(lower).stem == "views"
        or PurePosixPath(lower).stem.endswith("_views")
        or "/views/" in f"/{lower}/"
    )
    return _has(
        lower,
        "first_run",
        "first-run",
        "bootstrap_animemo",
        "provision_first_run",
        "ci_first_run",
    ) or site_config_entrypoint or _is_frontend_setup_route(lower) or lower in {
        "public/bootstrap.css",
        "tests/first-run-setup.test.mjs",
    }


def _is_database(path: str) -> bool:
    lower = path.lower()
    return not _is_docs(path) and (
        "/migrations/" in f"/{lower}"
        or _name(lower) in {"models.py", "schema.sql"}
        or "/models/" in f"/{lower}/"
        or _has(lower, "database", "postgres", "test_db_")
    )


def _is_settings(path: str) -> bool:
    lower = path.lower()
    return not _is_docs(path) and (
        _name(lower)
        in {
            "settings.py",
            ".env.example",
            ".env.development.example",
            ".env.production.example",
        }
        or lower.startswith("backend/config/credentials")
    )


def _is_auth_or_security(path: str) -> bool:
    lower = path.lower()
    return not _is_docs(path) and (
        lower.startswith("backend/accounts/")
        or _has(
            lower,
            "auth",
            "security",
            "csrf",
            "token",
            "registration",
            "credential",
            "secret",
            "permission",
            "turnstile",
            "anti_abuse",
        )
    )


def _is_api_contract(path: str) -> bool:
    lower = path.lower()
    name = _name(lower)
    return (
        not _is_docs(path)
        and lower.startswith("backend/")
        and (
            _has(lower, "openapi", "api_contract", "/api/")
            or _is_python_route_module(lower)
            or name.startswith("serializers")
            or name in {"api_errors.py", "api_renderers.py", "rest_exceptions.py"}
        )
    )


def _is_plugin_contract(path: str) -> bool:
    lower = path.lower()
    name = _name(lower)
    plugin_schema = (
        lower.startswith("plugins/")
        and PurePosixPath(lower).suffix in {".json", ".yaml", ".yml"}
        and (
            ".schema." in name
            or name.startswith("schema.")
            or name.endswith(
                (
                    "_schema.json",
                    "_schema.yaml",
                    "_schema.yml",
                    "-schema.json",
                    "-schema.yaml",
                    "-schema.yml",
                )
            )
            or any(part in {"schema", "schemas"} for part in PurePosixPath(lower).parts)
        )
    )
    return (
        lower.startswith("backend/plugin_host/")
        or plugin_schema
        or (
            lower.startswith("plugins/")
            and name in {"manifest.json", "package.json", "package-lock.json"}
        )
        or _has(
            lower,
            "pluginctl",
            "plugin-manifest",
            "plugin_sdk",
            "validate-plugins",
            "official_plugin_immutability",
        )
    )


def _is_integration_contract(path: str) -> bool:
    lower = path.lower()
    return lower.startswith(
        (
            "backend/integrations/",
            "bridges/",
            "backend/journal/external_accounts/",
            "backend/journal/external_media/",
            "backend/journal/external_sync/",
        )
    ) or _has(lower, "astrbot", "integration-protocol")


def _is_media_write(path: str) -> bool:
    lower = path.lower()
    return not _is_docs(path) and (
        lower.startswith(
            ("backend/site_config/media_storage/", "backend/journal/external_media/")
        )
        or _has(
            lower,
            "media_storage",
            "poster_security",
            "image_security",
            "media_write",
            "orphan_media",
            "storage_usage",
        )
    )


def _is_shared_contract(path: str) -> bool:
    lower = path.lower()
    return (
        path
        in {
            "backend/journal/domain_services.py",
            "backend/journal/mutation_ports.py",
            "backend/plugin_host/sdk/types.py",
            "backend/plugin_host/sdk/runtime.py",
        }
        or lower.startswith("docs/contracts/")
        or _has(
            lower, "shared-contract", "public-contract", "resource_identity_contract"
        )
    )


def _is_dependency(path: str) -> bool:
    name = _name(path)
    return (
        name
        in {
            "package.json",
            "package-lock.json",
            "requirements.in",
            "requirements.txt",
            "requirements-tools.txt",
            "requirements-ci.txt",
            "pyproject.toml",
            "uv.lock",
        }
        or path == ".npmrc"
    )


def _is_sensitive_documentation(path: str) -> bool:
    lower = path.lower()
    return lower in {
        "license",
        "notice",
        "third_party_notices",
        "trademarks",
    } or _is_docs(path) and (
        lower in FROZEN_CONTRACT_DOCUMENTS
        or lower.startswith("docs/contracts/")
        or _has(
            lower,
            "contract",
            "protocol",
            "plugin-sdk",
            "resource-identity",
            "resource_identity",
            "external-media-identity",
            "update-agent",
            "release",
            "upgrade",
            "updater",
            "deployment",
            "recovery",
            "backup",
            "restore",
            "rollback",
            "first-run",
            "security",
            "production-acceptance",
            "final-rc",
        )
    )


RULES = (
    RiskRule(
        "ci-authority",
        "CRITICAL",
        "CI authority, workflow selection, or exact-SHA gate logic changed.",
        ("ci", "tooling"),
        _is_ci_authority,
    ),
    RiskRule(
        "release-core",
        "CRITICAL",
        "Release producer, image rehearsal, manifest, provenance, or release authority code changed.",
        ("deployment",),
        _is_release_core,
    ),
    RiskRule(
        "updater-core",
        "CRITICAL",
        "Updater state, execution, deployment, or operator control changed.",
        ("deployment",),
        _is_updater,
    ),
    RiskRule(
        "deployment-runtime",
        "CRITICAL",
        "Production-like image, Compose, service, or deployment configuration changed.",
        ("deployment",),
        _is_deployment,
    ),
    RiskRule(
        "recovery-rollback",
        "CRITICAL",
        "Backup, restore, rollback, or stateful recovery behavior changed.",
        ("deployment", "recovery"),
        _is_recovery,
    ),
    RiskRule(
        "first-run-security-boundary",
        "CRITICAL",
        "First-run public setup entrypoint, route lock, or bootstrap administrator security behavior changed.",
        ("backend", "auth", "first_run"),
        _is_first_run,
    ),
    RiskRule(
        "database-schema",
        "HIGH",
        "Database models, migrations, or PostgreSQL behavior changed.",
        ("backend", "migration"),
        _is_database,
    ),
    RiskRule(
        "runtime-settings",
        "HIGH",
        "Runtime settings or credential wiring changed.",
        ("backend",),
        _is_settings,
    ),
    RiskRule(
        "auth-security",
        "HIGH",
        "Authentication, authorization, secret, anti-abuse, or security behavior changed.",
        ("backend", "auth"),
        _is_auth_or_security,
    ),
    RiskRule(
        "api-core-contract",
        "HIGH",
        "API routing, serialization, error, or OpenAPI contract changed.",
        ("backend", "api_contract"),
        _is_api_contract,
    ),
    RiskRule(
        "plugin-contract",
        "HIGH",
        "Plugin host, SDK, package manifest/schema, or plugin validation contract changed.",
        ("backend", "plugin"),
        _is_plugin_contract,
    ),
    RiskRule(
        "integration-protocol",
        "HIGH",
        "Integration or bridge protocol behavior changed.",
        ("backend", "integration", "bridge"),
        _is_integration_contract,
    ),
    RiskRule(
        "media-write-path",
        "HIGH",
        "Media write, storage accounting, or external-media behavior changed.",
        ("backend", "media_storage"),
        _is_media_write,
    ),
    RiskRule(
        "shared-domain-contract",
        "HIGH",
        "A shared domain or public resource identity contract changed.",
        ("backend", "shared_contract"),
        _is_shared_contract,
    ),
    RiskRule(
        "dependency-input",
        "HIGH",
        "A dependency declaration or resolved dependency set changed.",
        ("dependencies",),
        _is_dependency,
    ),
    RiskRule(
        "performance-gate",
        "HIGH",
        "Performance evidence generation or regression policy changed.",
        ("backend", "tooling"),
        lambda path: (
            path.startswith(("scripts/perf/", "backend/performance/"))
            or "performance" in path.lower()
        ),
    ),
    RiskRule(
        "sensitive-release-documentation",
        "HIGH",
        "Release, recovery, security, contract, or first-run operational documentation changed.",
        (),
        _is_sensitive_documentation,
    ),
    RiskRule(
        "automation-script",
        "HIGH",
        "Repository automation or its tests changed and requires broad validation.",
        ("ci", "tooling"),
        lambda path: path.startswith("scripts/"),
    ),
    RiskRule(
        "backend-product",
        "STANDARD",
        "Backend product code or backend tests changed.",
        ("backend",),
        lambda path: path.startswith("backend/"),
    ),
    RiskRule(
        "frontend-product",
        "STANDARD",
        "Frontend product code, assets, configuration, or browser tests changed.",
        ("frontend",),
        lambda path: (
            path.startswith(("src/", "public/", "tests/"))
            or path
            in {
                "index.html",
                "eslint.config.js",
                "eslint.config.mjs",
                "eslint.config.ts",
                "postcss.config.js",
                "tailwind.config.js",
                "vite.config.js",
                "vite.config.mjs",
                "vite.config.ts",
                "playwright.config.js",
                "playwright.config.mjs",
                "playwright.config.ts",
            }
        ),
    ),
    RiskRule(
        "official-plugin-product",
        "STANDARD",
        "Official plugin implementation or plugin-local tests changed.",
        ("plugin",),
        lambda path: path.startswith("plugins/"),
    ),
    RiskRule(
        "safe-documentation",
        "LOW",
        "Documentation-only content changed.",
        (),
        _is_docs,
    ),
    RiskRule(
        "repository-metadata",
        "LOW",
        "Non-runtime repository metadata changed.",
        (),
        lambda path: path in {".gitattributes", ".gitignore"},
    ),
)


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    normalized: set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        path = str(PurePosixPath(str(raw_path).replace("\\", "/")))
        if path and path != ".":
            normalized.add(path)
    return sorted(normalized)


def force_full_for_event(event_name: str, *, explicitly_forced: bool = False) -> bool:
    return explicitly_forced or event_name in {
        "merge_group",
        "workflow_dispatch",
        "workflow_call",
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _classification_document(
    paths: Iterable[str], *, force_full: bool = False
) -> dict[str, object]:
    normalized = _normalize_paths(paths)
    unknown_paths: list[str] = []
    path_matches: list[dict[str, object]] = []
    reason_paths: dict[tuple[str, str, str], set[str]] = {}
    signal_values = {name: False for name in SIGNAL_NAMES}

    for path in normalized:
        matched = [rule for rule in RULES if rule.matches(path)]
        if not matched:
            unknown_paths.append(path)
            matched = [
                RiskRule(
                    "unknown-path-fail-closed",
                    "CRITICAL",
                    "Path did not match an audited rule and was escalated fail-closed.",
                    (),
                    lambda _path: True,
                )
            ]

        path_level = max((rule.level for rule in matched), key=RISK_RANK.__getitem__)
        path_matches.append(
            {
                "path": path,
                "risk_level": path_level,
                "rules": [rule.rule_id for rule in matched],
            }
        )
        for rule in matched:
            reason_paths.setdefault((rule.rule_id, rule.level, rule.reason), set()).add(
                path
            )
            for category in rule.categories:
                signal_values[category] = True

    if not normalized:
        reason_paths[
            (
                "empty-change-set-fail-closed",
                "CRITICAL",
                "No changed path was available, so classification was escalated fail-closed.",
            )
        ] = set()

    reasons = [
        {
            "rule": rule_id,
            "level": level,
            "reason": reason,
            "paths": sorted(paths_for_reason),
        }
        for (rule_id, level, reason), paths_for_reason in sorted(
            reason_paths.items(), key=lambda item: (-RISK_RANK[item[0][1]], item[0][0])
        )
    ]
    risk_level = max((reason["level"] for reason in reasons), key=RISK_RANK.__getitem__)

    docs_only = (
        bool(normalized)
        and not force_full
        and all(
            _is_docs(path)
            and max(
                (RISK_RANK[rule.level] for rule in RULES if rule.matches(path)),
                default=RISK_RANK["CRITICAL"],
            )
            == RISK_RANK["LOW"]
            for path in normalized
        )
    )
    full_gate = force_full or RISK_RANK[risk_level] >= RISK_RANK["HIGH"]
    critical_gate = force_full or risk_level == "CRITICAL"

    primary_signals = (
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
        "first_run",
        "recovery",
        "media_storage",
    )
    mixed = sum(signal_values[name] for name in primary_signals) > 1

    run_frontend = signal_values["frontend"] or full_gate
    run_backend = (
        signal_values["backend"]
        or signal_values["auth"]
        or signal_values["api_contract"]
        or signal_values["migration"]
        or signal_values["integration"]
        or signal_values["shared_contract"]
        or signal_values["first_run"]
        or full_gate
    )
    run_bootstrap = (
        signal_values["ci"]
        or signal_values["deployment"]
        or signal_values["first_run"]
        or full_gate
    )
    run_plugins = (
        signal_values["plugin"]
        or signal_values["integration"]
        or signal_values["shared_contract"]
        or full_gate
    )
    run_bridge = (
        signal_values["bridge"]
        or signal_values["integration"]
        or signal_values["shared_contract"]
        or full_gate
    )
    run_postgres = (
        signal_values["auth"]
        or signal_values["api_contract"]
        or signal_values["migration"]
        or signal_values["integration"]
        or signal_values["shared_contract"]
        or signal_values["media_storage"]
        or signal_values["first_run"]
        or full_gate
    )
    run_runtime = run_bridge or signal_values["deployment"] or full_gate

    gates = {
        "docs_only": docs_only,
        "mixed": mixed,
        "run_frontend": run_frontend,
        "run_backend": run_backend,
        "run_bootstrap": run_bootstrap,
        "run_plugins": run_plugins,
        "run_bridge": run_bridge,
        "run_postgres": run_postgres,
        "run_runtime": run_runtime,
        "run_release_full": full_gate,
        "run_release_updater": critical_gate,
        "run_release_docker": full_gate,
        "run_release_stateful": full_gate,
        "full_gate": full_gate,
        "critical_gate": critical_gate,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "risk": {
            "level": risk_level,
            "rank": RISK_RANK[risk_level],
            "reasons": reasons,
        },
        "execution": {
            "force_full": force_full,
            "reasons": (
                [
                    {
                        "rule": "authority-event-force-full",
                        "reason": "The event is an authoritative or explicitly forced full validation.",
                    }
                ]
                if force_full
                else []
            ),
        },
        "paths": path_matches,
        "unknown_paths": unknown_paths,
        "signals": signal_values,
        "gates": gates,
    }


def classify_paths(paths: list[str], *, force_full: bool = False) -> dict[str, str]:
    document = _classification_document(paths, force_full=force_full)
    risk = document["risk"]
    assert isinstance(risk, dict)
    execution = document["execution"]
    assert isinstance(execution, dict)
    signals = document["signals"]
    gates = document["gates"]
    assert isinstance(signals, dict)
    assert isinstance(gates, dict)

    result = {
        "schema_version": SCHEMA_VERSION,
        "risk_level": str(risk["level"]),
        "risk_rank": str(risk["rank"]),
        "execution_force_full": "true" if execution["force_full"] else "false",
        "reasons": _json(risk["reasons"]),
        "matched_rules": _json(document["paths"]),
        "unknown_paths": _json(document["unknown_paths"]),
        "classification_json": _json(document),
    }
    result.update({name: "true" if signals[name] else "false" for name in SIGNAL_NAMES})
    result.update({name: "true" if gates[name] else "false" for name in GATE_NAMES})
    return {name: result[name] for name in OUTPUT_NAMES}


def changed_paths(base: str | None, head: str | None) -> list[str]:
    if base and base != "0" * 40 and head:
        command = [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base}...{head}",
        ]
    elif head:
        command = [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-r",
            "-z",
            head,
        ]
    else:
        command = [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-r",
            "-z",
            "HEAD",
        ]
    completed = subprocess.run(command, check=True, capture_output=True)
    return _normalize_paths(
        os.fsdecode(raw_path) for raw_path in completed.stdout.split(b"\0") if raw_path
    )


def write_outputs(result: dict[str, str], output_path: str) -> None:
    with open(output_path, "a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in result.items())


def self_test() -> None:
    cases = {
        "LOW": ["docs/architecture.md", "README.md"],
        "STANDARD": ["src/pages/Journal.jsx"],
        "HIGH": ["backend/journal/migrations/0002_add.py"],
        "CRITICAL": ["updater/agent.py"],
    }
    for expected_level, paths in cases.items():
        result = classify_paths(paths)
        assert result["risk_level"] == expected_level, (expected_level, result)
        assert json.loads(result["matched_rules"]), result
    unknown = classify_paths(["future-system/new-control-plane.bin"])
    assert unknown["risk_level"] == "CRITICAL", unknown
    assert json.loads(unknown["unknown_paths"]) == [
        "future-system/new-control-plane.bin"
    ], unknown
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

    force_full = force_full_for_event(
        os.getenv("GITHUB_EVENT_NAME", ""),
        explicitly_forced=os.getenv("CI_FORCE_FULL", "").strip().lower() == "true",
    )
    paths = (
        args.files if args.files is not None else changed_paths(args.base, args.head)
    )
    result = classify_paths(paths, force_full=force_full)
    result["changed_files"] = _json(_normalize_paths(paths))
    if args.github_output:
        write_outputs(result, args.github_output)
    else:
        print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
