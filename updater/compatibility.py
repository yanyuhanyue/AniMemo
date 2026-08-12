from __future__ import annotations

from dataclasses import dataclass

from packaging.version import Version

from release.contract import validate_manifest


@dataclass(frozen=True)
class DeploymentContext:
    current_manifest: dict[str, object] | None
    database_contract: str
    configuration_contract: str
    enabled_plugin_apis: frozenset[int]


@dataclass(frozen=True)
class SwitchPlan:
    allowed: bool
    decision: str
    rollback_mode: str
    migration_required: bool
    migration_policy: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "decision": self.decision,
            "rollbackMode": self.rollback_mode,
            "migrationRequired": self.migration_required,
            "migrationPolicy": self.migration_policy,
            "reasons": list(self.reasons),
        }


def _release_version(manifest: dict[str, object] | None) -> Version | None:
    if manifest is None:
        return None
    return Version(str(manifest["release"]["version"]).removeprefix("v"))


def plan_switch(
    context: DeploymentContext,
    target_manifest: dict[str, object],
    *,
    updater_version: str,
) -> SwitchPlan:
    validate_manifest(target_manifest, updater_version=updater_version)
    compatibility = target_manifest["compatibility"]
    database = compatibility["database"]
    configuration = compatibility["configuration"]
    plugin_sdk = compatibility["pluginSdk"]
    migration = database["migration"]
    reasons: list[str] = []

    if context.database_contract not in database["appAccepts"]:
        reasons.append("database_contract_not_accepted")
    if context.configuration_contract not in configuration["appAccepts"]:
        reasons.append("configuration_contract_not_accepted")
    if not context.enabled_plugin_apis.issubset(set(plugin_sdk["supportedApis"])):
        reasons.append("enabled_plugin_sdk_not_supported")
    if migration["policy"] == "breaking-blocked":
        reasons.append("breaking_migration_blocked")

    target_version = _release_version(target_manifest)
    current_version = _release_version(context.current_manifest)
    is_downgrade = current_version is not None and target_version < current_version
    if reasons:
        return SwitchPlan(
            allowed=False,
            decision="unsafe_downgrade" if is_downgrade else "blocked",
            rollback_mode="blocked",
            migration_required=bool(migration["required"]),
            migration_policy=str(migration["policy"]),
            reasons=tuple(reasons),
        )

    rollback_contract = database["applicationRollback"]
    rollback_mode = "application" if is_downgrade else ("safe" if rollback_contract == "safe" else "application")
    return SwitchPlan(
        allowed=True,
        decision="application_rollback" if is_downgrade else "safe_switch",
        rollback_mode=rollback_mode,
        migration_required=bool(migration["required"]),
        migration_policy=str(migration["policy"]),
        reasons=(),
    )
