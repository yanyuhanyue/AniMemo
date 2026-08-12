from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from release.contract import build_manifest
from updater.compatibility import DeploymentContext, plan_switch

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def manifest(
    version: str,
    *,
    database_contract: str = "animemo-db-v1",
    database_accepts: list[str] | None = None,
    migration_required: bool = False,
    migration_policy: str = "none",
    application_rollback: str = "safe",
    configuration_contract: str = "animemo-config-v1",
    configuration_accepts: list[str] | None = None,
    plugin_sdk_apis: list[int] | None = None,
):
    channel = "stable" if "-" not in version else version.split("-")[1].split(".")[0]
    promoted_from = f"{version}-rc.1" if channel == "stable" else None
    return build_manifest(
        version=version,
        channel=channel,
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest=DIGEST_A,
        web_digest=DIGEST_B,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract=database_contract,
        database_accepts=database_accepts or [database_contract],
        migration_required=migration_required,
        migration_policy=migration_policy,
        application_rollback=application_rollback,
        configuration_contract=configuration_contract,
        configuration_accepts=configuration_accepts or [configuration_contract],
        plugin_sdk_apis=plugin_sdk_apis or [2],
        promoted_from=promoted_from,
    )


class CompatibilityPlanTests(unittest.TestCase):
    def context(self, current):
        return DeploymentContext(
            current_manifest=current,
            database_contract=current["compatibility"]["database"]["contract"],
            configuration_contract=current["compatibility"]["configuration"]["contract"],
            enabled_plugin_apis=frozenset({2}),
        )

    def test_no_migration_release_is_a_safe_switch(self):
        current = manifest("v1.0.0")
        target = manifest("v1.0.1")

        plan = plan_switch(self.context(current), target, updater_version="1.0.0")

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.decision, "safe_switch")
        self.assertEqual(plan.rollback_mode, "safe")
        self.assertFalse(plan.migration_required)

    def test_additive_migration_can_keep_application_rollback(self):
        current = manifest("v1.0.0", database_accepts=["animemo-db-v1", "animemo-db-v2"])
        target = manifest(
            "v1.1.0",
            database_contract="animemo-db-v2",
            database_accepts=["animemo-db-v1", "animemo-db-v2"],
            migration_required=True,
            migration_policy="additive-backward-compatible",
            application_rollback="conditional",
        )

        plan = plan_switch(self.context(current), target, updater_version="1.0.0")

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.decision, "safe_switch")
        self.assertEqual(plan.rollback_mode, "application")
        self.assertTrue(plan.migration_required)

    def test_old_application_that_accepts_current_schema_is_application_rollback(self):
        current = manifest("v1.1.0", database_contract="animemo-db-v2")
        target = manifest(
            "v1.0.0",
            database_contract="animemo-db-v1",
            database_accepts=["animemo-db-v1", "animemo-db-v2"],
        )
        context = DeploymentContext(
            current_manifest=current,
            database_contract="animemo-db-v2",
            configuration_contract="animemo-config-v1",
            enabled_plugin_apis=frozenset({2}),
        )

        plan = plan_switch(context, target, updater_version="1.0.0")

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.decision, "application_rollback")
        self.assertEqual(plan.rollback_mode, "application")

    def test_unsafe_database_downgrade_is_blocked(self):
        current = manifest("v1.1.0", database_contract="animemo-db-v2")
        target = manifest("v1.0.0", database_contract="animemo-db-v1")
        context = DeploymentContext(
            current_manifest=current,
            database_contract="animemo-db-v2",
            configuration_contract="animemo-config-v1",
            enabled_plugin_apis=frozenset({2}),
        )

        plan = plan_switch(context, target, updater_version="1.0.0")

        self.assertFalse(plan.allowed)
        self.assertEqual(plan.decision, "unsafe_downgrade")
        self.assertIn("database_contract_not_accepted", plan.reasons)

    def test_enabled_plugin_sdk_must_be_supported(self):
        current = manifest("v1.0.0")
        target = copy.deepcopy(manifest("v1.0.1"))
        target["compatibility"]["pluginSdk"]["supportedApis"] = [1]

        plan = plan_switch(self.context(current), target, updater_version="1.0.0")

        self.assertFalse(plan.allowed)
        self.assertIn("enabled_plugin_sdk_not_supported", plan.reasons)

    def test_breaking_migration_is_never_executable(self):
        current = manifest("v1.0.0")
        target = manifest(
            "v2.0.0",
            database_contract="animemo-db-v2",
            database_accepts=["animemo-db-v1", "animemo-db-v2"],
            migration_required=True,
            migration_policy="breaking-blocked",
            application_rollback="blocked",
        )

        plan = plan_switch(self.context(current), target, updater_version="1.0.0")

        self.assertFalse(plan.allowed)
        self.assertIn("breaking_migration_blocked", plan.reasons)


if __name__ == "__main__":
    unittest.main()
