#!/usr/bin/env python3
"""Classify changed paths into deterministic AniMemo CI risk gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

SCHEMA_VERSION = "animemo.ci-risk/v2"
RISK_LEVELS = ("LOW", "STANDARD", "HIGH", "CRITICAL")
RISK_RANK = {level: rank for rank, level in enumerate(RISK_LEVELS, start=1)}
EXECUTION_PROFILES = (
    "DOCS_ONLY",
    "CONTRACT_VALIDATION_ONLY",
    "TARGETED",
    "FULL_AUTHORITY",
)

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

ROOT_LEGAL_DOCUMENTS = frozenset(
    {
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES",
        "TRADEMARKS",
    }
)

AUDITED_CONTRACT_PRIMARY_DOCUMENTS = frozenset(
    {
        "docs/backup-contract-v1.md",
        "docs/compatibility-matrix-v1.md",
        "docs/doctor-basic-contract-v1.md",
        "docs/migration-bundle-v1.md",
        "docs/migration-secret-envelope-v1.md",
        "docs/restore-contract-v1.md",
    }
)
AUDITED_CONTRACT_SUPPORT_DOCUMENTS = frozenset(
    {
        "CONTEXT.md",
        "README.md",
        "docs/data-bundle-v1.md",
    }
)
AUDITED_CONTRACT_VALIDATION_TESTS = frozenset(
    {"scripts/tests/test_recovery_migration_contracts.py"}
)
AUDITED_CONTRACT_CHANGE_PATHS = frozenset(
    {
        *AUDITED_CONTRACT_PRIMARY_DOCUMENTS,
        *AUDITED_CONTRACT_SUPPORT_DOCUMENTS,
        *AUDITED_CONTRACT_VALIDATION_TESTS,
    }
)

RECOVERY_RUNTIME_PREFIXES = (
    "durability/",
    "scripts/tests/test_durability_",
    "scripts/tests/test_production_backup_",
)
RECOVERY_RUNTIME_PATHS = frozenset(
    {
        "scripts/dr-rehearsal.sh",
        "scripts/dr_backup.py",
        "scripts/dr_recovery_paths.py",
        "scripts/tests/test_dr_backup.py",
        "scripts/tests/test_dr_recovery_paths.py",
        "scripts/tests/test_dr_rehearsal_contract.py",
    }
)
STATEFUL_RUNTIME_PATHS = frozenset(
    {
        "scripts/stateful-upgrade-gate.sh",
        "scripts/stateful_upgrade_fixture.py",
        "scripts/tests/test_stateful_upgrade_diagnostics.py",
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
    "database",
    "dependencies",
    "ci",
    "deployment",
    "release",
    "updater",
    "shared_contract",
    "first_run",
    "recovery",
    "media_storage",
    "tooling",
)

GATE_NAMES = (
    "docs_only",
    "run_contract_validation",
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
    "run_release_dr",
    "full_gate",
    "critical_gate",
)

OUTPUT_NAMES = (
    "schema_version",
    "risk_level",
    "risk_rank",
    "execution_profile",
    "execution_force_full",
    "reasons",
    "matched_rules",
    "unknown_paths",
    "classification_json",
    "docs_only",
    "run_contract_validation",
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
    "run_release_dr",
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
    return (
        path in ROOT_LEGAL_DOCUMENTS
        or lower.startswith("docs/")
        or lower.endswith((".md", ".mdx", ".rst"))
    )


def _has(path: str, *parts: str) -> bool:
    return any(part in path for part in parts)


def _name(path: str) -> str:
    return PurePosixPath(path).name.lower()


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
    return path.startswith(("release/", "scripts/tests/test_release_")) or path in {
        "scripts/release_authority.py",
        "scripts/tests/test_deployment_updater_contract.py",
        "tests/release-gate.test.mjs",
    }


def _is_installer(path: str) -> bool:
    return path.startswith("installer/")


def _is_managed_configuration(path: str) -> bool:
    return path in {
        "durability/managed_config.py",
        "scripts/tests/test_managed_config.py",
        "updater/configuration.py",
        "updater/tests/test_configuration.py",
    }


def _is_platform_qualification(path: str) -> bool:
    return path in {
        "durability/platform.py",
        "scripts/platform_qualification.py",
        "scripts/tests/test_platform_qualification.py",
    }


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


def _is_durability(path: str) -> bool:
    return path.startswith(RECOVERY_RUNTIME_PREFIXES)


def _is_recovery(path: str) -> bool:
    return path.startswith(RECOVERY_RUNTIME_PREFIXES) or path in RECOVERY_RUNTIME_PATHS


def _is_migration_runtime(path: str) -> bool:
    return (
        path not in AUDITED_CONTRACT_VALIDATION_TESTS
        and not _is_docs(path)
        and path.startswith(
            (
                "durability/migration",
                "scripts/migration",
                "scripts/tests/test_migration_runtime",
            )
        )
    )


def _is_stateful_runtime(path: str) -> bool:
    return path in STATEFUL_RUNTIME_PATHS


def _is_audited_contract_change_set(paths: list[str]) -> bool:
    return (
        bool(paths)
        and any(path in AUDITED_CONTRACT_PRIMARY_DOCUMENTS for path in paths)
        and all(path in AUDITED_CONTRACT_CHANGE_PATHS for path in paths)
    )


def _is_automation_script(path: str) -> bool:
    return path.startswith("scripts/") and path not in AUDITED_CONTRACT_VALIDATION_TESTS


def _dependency_signal_names(path: str) -> tuple[str, ...]:
    if path in {"package.json", "package-lock.json", ".npmrc"}:
        return ("frontend", "plugin")
    if path.startswith("backend/"):
        return ("backend", "database")
    if path.startswith("bridges/"):
        return ("bridge", "integration")
    if path.startswith("durability/"):
        return ("recovery",)
    if path.startswith("release/"):
        return ("release",)
    if path.startswith("updater/"):
        return ("updater",)
    if path.startswith("scripts/"):
        return ("ci", "tooling")
    return ("frontend", "backend", "plugin", "database")


def _is_first_run(path: str) -> bool:
    if _is_docs(path):
        return False
    lower = path.lower()
    return _has(
        lower,
        "first_run",
        "first-run",
        "bootstrap_animemo",
        "provision_first_run",
        "ci_first_run",
    ) or path in {
        "src/pages/SetupPage.jsx",
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
    if lower.startswith(("durability/", "scripts/tests/test_durability_")):
        return False
    return not _is_docs(path) and (
        lower.startswith("backend/accounts/")
        or _has(
            lower,
            "/auth/",
            "/auth_",
            "_auth.",
            "_auth_",
            "authentication",
            "authorization",
            "oauth",
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
        or _name(lower) in {"auth.py", "auth.ts", "auth.tsx", "auth.js", "auth.jsx"}
    )


def _is_api_contract(path: str) -> bool:
    lower = path.lower()
    name = _name(lower)
    return (
        not _is_docs(path)
        and lower.startswith("backend/")
        and (
            _has(lower, "openapi", "api_contract", "/api/")
            or name == "urls.py"
            or name.startswith("serializers")
            or name in {"api_errors.py", "api_renderers.py", "rest_exceptions.py"}
        )
    )


def _is_plugin_contract(path: str) -> bool:
    lower = path.lower()
    return (
        lower.startswith("backend/plugin_host/")
        or (
            lower.startswith("plugins/")
            and _name(lower) in {"manifest.json", "package.json", "package-lock.json"}
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
    return _is_docs(path) and (
        lower in FROZEN_CONTRACT_DOCUMENTS
        or lower in AUDITED_CONTRACT_PRIMARY_DOCUMENTS
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
        "Release producer, manifest, provenance, or release authority code changed.",
        ("release", "tooling"),
        _is_release_core,
    ),
    RiskRule(
        "install-portal-bootstrap",
        "CRITICAL",
        "Install portal bootstrap transport or installation UX changed.",
        ("release", "tooling"),
        lambda path: path.startswith(
            ("sites/install-portal/", "install.animemo.cc/")
        ),
    ),
    RiskRule(
        "installer-runtime",
        "CRITICAL",
        "Installer planning, mutation, Restore, or acceptance behavior changed.",
        ("deployment", "release", "updater", "recovery"),
        _is_installer,
    ),
    RiskRule(
        "managed-configuration-runtime",
        "CRITICAL",
        "Canonical managed configuration or its apply transaction changed.",
        ("deployment", "updater", "recovery"),
        _is_managed_configuration,
    ),
    RiskRule(
        "platform-qualification-authority",
        "CRITICAL",
        "Hosted platform qualification evidence or its verifier changed.",
        ("deployment", "release", "recovery"),
        _is_platform_qualification,
    ),
    RiskRule(
        "updater-core",
        "CRITICAL",
        "Updater state, execution, deployment, or operator control changed.",
        ("updater",),
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
        "durability-runtime",
        "CRITICAL",
        "Durable deployment, recovery, secret envelope, or diagnostic runtime changed.",
        ("recovery",),
        _is_durability,
    ),
    RiskRule(
        "migration-runtime",
        "CRITICAL",
        "Instance migration runtime behavior changed.",
        ("migration", "recovery"),
        _is_migration_runtime,
    ),
    RiskRule(
        "recovery-rollback",
        "CRITICAL",
        "Backup, restore, rollback, or stateful recovery behavior changed.",
        ("recovery",),
        _is_recovery,
    ),
    RiskRule(
        "stateful-upgrade-runtime",
        "CRITICAL",
        "Stateful production upgrade behavior or its diagnostics changed.",
        ("deployment",),
        _is_stateful_runtime,
    ),
    RiskRule(
        "first-run-security-boundary",
        "CRITICAL",
        "First-run setup or bootstrap administrator security behavior changed.",
        ("backend", "auth", "first_run"),
        _is_first_run,
    ),
    RiskRule(
        "database-schema",
        "HIGH",
        "Database models, migrations, or PostgreSQL behavior changed.",
        ("backend", "database"),
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
        "Plugin host, SDK, package manifest, or plugin validation contract changed.",
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
        "audited-contract-validation-test",
        "HIGH",
        "An audited pure contract consistency test changed.",
        (),
        lambda path: path in AUDITED_CONTRACT_VALIDATION_TESTS,
    ),
    RiskRule(
        "automation-script",
        "HIGH",
        "Repository automation or its tests changed and requires broad validation.",
        ("ci", "tooling"),
        _is_automation_script,
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
    return explicitly_forced or event_name == "merge_group"


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
        if _is_dependency(path):
            for category in _dependency_signal_names(path):
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

    intrinsic_docs_only = (
        bool(normalized)
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

    if force_full:
        execution_profile = "FULL_AUTHORITY"
        execution_rule = "authority-force-full"
        execution_reason = "Explicit authority context selected the complete matrices."
    elif intrinsic_docs_only:
        execution_profile = "DOCS_ONLY"
        execution_rule = "intrinsic-docs-only"
        execution_reason = "Every changed path is audited LOW documentation."
    elif _is_audited_contract_change_set(normalized):
        execution_profile = "CONTRACT_VALIDATION_ONLY"
        execution_rule = "audited-contract-change-set"
        execution_reason = (
            "Every changed path belongs to the audited contract validation allowlist."
        )
    else:
        execution_profile = "TARGETED"
        execution_rule = "targeted-signals"
        execution_reason = "Component signals selected the minimum sufficient PR gates."

    docs_only = execution_profile == "DOCS_ONLY"
    run_contract_validation = execution_profile == "CONTRACT_VALIDATION_ONLY"
    full_gate = execution_profile == "FULL_AUTHORITY"
    critical_gate = risk_level == "CRITICAL"
    conservative_broad = execution_profile == "TARGETED" and (
        bool(unknown_paths) or not normalized
    )

    primary_signals = (
        "frontend",
        "backend",
        "auth",
        "api_contract",
        "plugin",
        "integration",
        "bridge",
        "migration",
        "database",
        "dependencies",
        "ci",
        "deployment",
        "release",
        "updater",
        "shared_contract",
        "first_run",
        "recovery",
        "media_storage",
    )
    mixed = sum(signal_values[name] for name in primary_signals) > 1

    targeted = execution_profile == "TARGETED"
    run_frontend = full_gate or conservative_broad or (
        targeted and signal_values["frontend"]
    )
    run_backend = (
        full_gate
        or conservative_broad
        or (
            targeted
            and any(
                signal_values[name]
                for name in (
                    "backend",
                    "auth",
                    "api_contract",
                    "migration",
                    "database",
                    "integration",
                    "shared_contract",
                    "first_run",
                    "media_storage",
                )
            )
        )
    )
    run_bootstrap = (
        full_gate
        or conservative_broad
        or (
            targeted
            and any(
                signal_values[name]
                for name in ("ci", "deployment", "first_run", "updater")
            )
        )
    )
    run_plugins = (
        full_gate
        or conservative_broad
        or (
            targeted
            and any(
                signal_values[name]
                for name in (
                    "plugin",
                    "integration",
                    "shared_contract",
                    "migration",
                    "recovery",
                    "release",
                )
            )
        )
    )
    run_bridge = (
        full_gate
        or conservative_broad
        or (
            targeted
            and any(
                signal_values[name]
                for name in ("bridge", "integration", "shared_contract")
            )
        )
    )
    run_postgres = (
        full_gate
        or conservative_broad
        or (
            targeted
            and any(
                signal_values[name]
                for name in (
                    "auth",
                    "api_contract",
                    "plugin",
                    "migration",
                    "database",
                    "integration",
                    "shared_contract",
                    "media_storage",
                    "first_run",
                    "recovery",
                )
            )
        )
    )
    run_runtime = full_gate or conservative_broad or (
        targeted
        and any(
            signal_values[name]
            for name in ("bridge", "integration", "shared_contract")
        )
    )
    run_release_updater = full_gate or conservative_broad or (
        targeted and (signal_values["updater"] or signal_values["release"])
    )
    run_release_docker = full_gate or conservative_broad or (
        targeted
        and any(
            signal_values[name]
            for name in ("deployment", "release", "first_run")
        )
    )
    run_release_stateful = full_gate or conservative_broad or (
        targeted
        and any(
            signal_values[name]
            for name in (
                "database",
                "deployment",
                "release",
                "updater",
                "first_run",
            )
        )
    )
    run_release_dr = full_gate or conservative_broad or (
        targeted and (signal_values["recovery"] or signal_values["migration"])
    )

    gates = {
        "docs_only": docs_only,
        "run_contract_validation": run_contract_validation,
        "mixed": mixed,
        "run_frontend": run_frontend,
        "run_backend": run_backend,
        "run_bootstrap": run_bootstrap,
        "run_plugins": run_plugins,
        "run_bridge": run_bridge,
        "run_postgres": run_postgres,
        "run_runtime": run_runtime,
        "run_release_full": full_gate,
        "run_release_updater": run_release_updater,
        "run_release_docker": run_release_docker,
        "run_release_stateful": run_release_stateful,
        "run_release_dr": run_release_dr,
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
            "profile": execution_profile,
            "force_full": force_full,
            "reasons": [{"rule": execution_rule, "reason": execution_reason}],
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
        "execution_profile": str(execution["profile"]),
        "execution_force_full": "true" if execution["force_full"] else "false",
        "reasons": _json(risk["reasons"]),
        "matched_rules": _json(document["paths"]),
        "unknown_paths": _json(document["unknown_paths"]),
        "classification_json": _json(document),
    }
    result.update({name: "true" if signals[name] else "false" for name in SIGNAL_NAMES})
    result.update({name: "true" if gates[name] else "false" for name in GATE_NAMES})
    return {name: result[name] for name in OUTPUT_NAMES}


_ZERO_COMMIT_SHA = "0" * 40
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}\Z")


def _canonical_commit_sha(
    value: str | None,
    name: str,
    *,
    allow_zero: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _COMMIT_SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact 40-character commit SHA")
    canonical = f"{int(value, 16):040x}"
    if canonical == _ZERO_COMMIT_SHA and not allow_zero:
        raise ValueError(f"{name} must be a non-zero 40-character commit SHA")
    return canonical


def changed_paths(base: str | None, head: str | None) -> list[str]:
    base_sha = _canonical_commit_sha(base, "base", allow_zero=True)
    head_sha = _canonical_commit_sha(head, "head")
    if base_sha and base_sha != _ZERO_COMMIT_SHA and head_sha:
        command = [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base_sha}...{head_sha}",
            "--",
        ]
    elif head_sha:
        command = [
            "git",
            "diff-tree",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-r",
            "-z",
            head_sha,
            "--",
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
            "--",
        ]
    completed = subprocess.run(command, check=True, capture_output=True)
    return _normalize_paths(
        os.fsdecode(raw_path) for raw_path in completed.stdout.split(b"\0") if raw_path
    )


def write_outputs(result: dict[str, str], output_path: str) -> None:
    with open(output_path, "a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in result.items())


def self_test() -> None:
    cases = (
        ("LOW", "DOCS_ONLY", ["docs/architecture.md", "README.md"]),
        ("STANDARD", "TARGETED", ["src/App.jsx"]),
        (
            "HIGH",
            "CONTRACT_VALIDATION_ONLY",
            [
                "docs/backup-contract-v1.md",
                "scripts/tests/test_recovery_migration_contracts.py",
            ],
        ),
        ("HIGH", "TARGETED", ["backend/journal/migrations/0002_add.py"]),
        ("CRITICAL", "TARGETED", ["updater/agent.py"]),
    )
    for expected_level, expected_profile, paths in cases:
        result = classify_paths(paths)
        assert result["risk_level"] == expected_level, (expected_level, result)
        assert result["execution_profile"] == expected_profile, (
            expected_profile,
            result,
        )
        assert result["full_gate"] == "false", result
        assert json.loads(result["matched_rules"]), result
    unknown = classify_paths(["future-system/new-control-plane.bin"])
    assert unknown["risk_level"] == "CRITICAL", unknown
    assert unknown["execution_profile"] == "TARGETED", unknown
    product_gates = (
        "run_frontend",
        "run_backend",
        "run_bootstrap",
        "run_plugins",
        "run_bridge",
        "run_postgres",
        "run_runtime",
    )
    assert all(unknown[name] == "true" for name in product_gates), unknown
    assert all(
        unknown[name] == "true"
        for name in (
            "run_release_updater",
            "run_release_docker",
            "run_release_stateful",
            "run_release_dr",
        )
    ), unknown
    assert json.loads(unknown["unknown_paths"]) == [
        "future-system/new-control-plane.bin"
    ], unknown
    forced = classify_paths(["README.md"], force_full=True)
    assert forced["risk_level"] == "LOW", forced
    assert forced["execution_profile"] == "FULL_AUTHORITY", forced
    assert forced["full_gate"] == "true", forced
    assert forced["run_release_full"] == "true", forced
    assert all(forced[name] == "true" for name in product_gates), forced
    assert forced["run_release_stateful"] == "true", forced
    assert forced["run_release_dr"] == "true", forced
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
