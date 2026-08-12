from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import StateError
from updater.plans import PlanStore


def manifest():
    return build_manifest(
        version="v1.0.0",
        channel="stable",
        commit="1" * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + "a" * 64,
        web_digest="sha256:" + "b" * 64,
        deployment_contract_sha256="sha256:0be5fdf5f87275755e06a2e2b6523c24e16d6aa1db48d8d58e8cfea969b674df",
        deployment_files=[
            {"path": "deploy/docker-compose.yml", "sha256": "sha256:" + "d" * 64},
            {"path": "updater/docker-compose.runtime.yml", "sha256": "sha256:" + "e" * 64},
        ],
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from="v1.0.0-rc.1",
    )


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


class PlanStoreTests(unittest.TestCase):
    def test_plan_store_rejects_a_plans_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside = root / "outside"
            state_root.mkdir()
            outside.mkdir()
            link_directory(state_root / "plans", outside)
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.create(manifest(), {"target": "v1.0.0"})

            self.assertEqual(list(outside.iterdir()), [])

    def test_plan_store_rejects_reading_from_a_plans_directory_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / "state"
            outside_root = root / "outside"
            plan = PlanStore(outside_root).create(manifest(), {"target": "v1.0.0"})
            state_root.mkdir()
            link_directory(state_root / "plans", outside_root / "plans")
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "directory"):
                store.get(plan["id"])

    def test_plan_store_rejects_a_hard_linked_plan_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside_root = root / "outside"
            plan = PlanStore(outside_root).create(manifest(), {"target": "v1.0.0"})
            state_root = root / "state"
            plans = state_root / "plans"
            plans.mkdir(parents=True)
            (plans / f"{plan['id']}.json").hardlink_to(
                outside_root / "plans" / f"{plan['id']}.json"
            )
            store = PlanStore(state_root)

            with self.assertRaisesRegex(StateError, "file"):
                store.get(plan["id"])


if __name__ == "__main__":
    unittest.main()
