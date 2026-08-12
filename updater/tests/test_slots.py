from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import StateError
from updater.slots import ReleaseSlots


def manifest(version: str, digit: str):
    return build_manifest(
        version=version,
        channel="stable",
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit.upper().lower() * 64,
        minimum_updater_version="1.0.0",
        database_contract="animemo-db-v1",
        database_accepts=["animemo-db-v1"],
        migration_required=False,
        migration_policy="none",
        application_rollback="safe",
        configuration_contract="animemo-config-v1",
        configuration_accepts=["animemo-config-v1"],
        plugin_sdk_apis=[2],
        promoted_from=f"{version}-rc.1",
    )


class ReleaseSlotTests(unittest.TestCase):
    def test_promote_keeps_current_previous_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            slots = ReleaseSlots(Path(directory))
            first = manifest("v1.0.0", "1")
            second = manifest("v1.0.1", "2")

            slots.import_current(first)
            slots.promote(second, operation_id="a" * 32)
            state = slots.read()

            self.assertEqual(state["current"]["release"]["version"], "v1.0.1")
            self.assertEqual(state["previous"]["release"]["version"], "v1.0.0")
            self.assertEqual([item["manifest"]["release"]["version"] for item in state["history"]], ["v1.0.0", "v1.0.1"])

    def test_failed_switch_does_not_change_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            slots = ReleaseSlots(Path(directory))
            first = manifest("v1.0.0", "1")
            slots.import_current(first)

            self.assertEqual(slots.read()["current"], first)
            self.assertIsNone(slots.read()["previous"])

    def test_import_current_is_strictly_one_time_even_for_same_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            slots = ReleaseSlots(Path(directory))
            first = manifest("v1.0.0", "1")
            slots.import_current(first)

            with self.assertRaisesRegex(StateError, "already initialized"):
                slots.import_current(first)

    def test_repeated_rollback_swaps_current_and_previous_each_time(self):
        with tempfile.TemporaryDirectory() as directory:
            slots = ReleaseSlots(Path(directory))
            first = manifest("v1.0.0", "1")
            second = manifest("v1.0.1", "2")
            slots.import_current(first)
            slots.promote(second, operation_id="a" * 32)

            slots.restore_previous(operation_id="b" * 32)
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.0")
            self.assertEqual(slots.read()["previous"]["release"]["version"], "v1.0.1")

            slots.restore_previous(operation_id="c" * 32)
            self.assertEqual(slots.read()["current"]["release"]["version"], "v1.0.1")
            self.assertEqual(slots.read()["previous"]["release"]["version"], "v1.0.0")


if __name__ == "__main__":
    unittest.main()
