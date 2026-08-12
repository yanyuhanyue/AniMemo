from __future__ import annotations

import os
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from release.contract import build_manifest
from updater.errors import StateError
from updater.slots import ReleaseSlots


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


def manifest(version: str, digit: str):
    return build_manifest(
        version=version,
        channel="stable",
        commit=digit * 40,
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        api_digest="sha256:" + digit * 64,
        web_digest="sha256:" + digit.upper().lower() * 64,
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
            self.assertEqual(state["generation"], 2)
            self.assertTrue((Path(directory) / "release-slots.json").is_file())

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

    def test_import_rejects_a_history_directory_link_without_partial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "releases"
            outside = root / "outside"
            release_root.mkdir()
            outside.mkdir()
            link_directory(release_root / "history", outside)
            slots = ReleaseSlots(release_root)

            with self.assertRaisesRegex(StateError, "directory"):
                slots.import_current(manifest("v1.0.0", "1"))

            self.assertFalse((release_root / "CURRENT.json").exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_read_rejects_a_linked_release_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            ReleaseSlots(outside).import_current(manifest("v1.0.0", "1"))
            link_directory(root / "releases", outside)
            slots = ReleaseSlots(root / "releases")

            with self.assertRaisesRegex(StateError, "directory"):
                slots.read()

    def test_read_rejects_a_linked_history_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "releases"
            outside = root / "outside"
            outside.mkdir()
            release_root.mkdir()
            link_directory(release_root / "history", outside)
            slots = ReleaseSlots(release_root)

            with self.assertRaisesRegex(StateError, "directory"):
                slots.read()

    def test_read_rejects_a_hard_linked_current_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "releases"
            outside_root = root / "outside"
            release_root.mkdir()
            outside_root.mkdir()
            (outside_root / "CURRENT.json").write_text(
                json.dumps(manifest("v1.0.0", "1")),
                encoding="utf-8",
            )
            (release_root / "CURRENT.json").hardlink_to(outside_root / "CURRENT.json")
            slots = ReleaseSlots(release_root)

            with self.assertRaisesRegex(StateError, "file"):
                slots.read()

    def test_read_rejects_a_hard_linked_history_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "releases"
            outside_root = root / "outside"
            first = manifest("v1.0.0", "1")
            release_root.mkdir()
            (release_root / "CURRENT.json").write_text(json.dumps(first), encoding="utf-8")
            (release_root / "history").mkdir()
            (outside_root / "history").mkdir(parents=True)
            history_name = "v1.0.0.json"
            (outside_root / "history" / history_name).write_text(
                json.dumps({"manifest": first, "deployment": {"operationId": None}}),
                encoding="utf-8",
            )
            (release_root / "history" / history_name).hardlink_to(
                outside_root / "history" / history_name
            )
            slots = ReleaseSlots(release_root)

            with self.assertRaisesRegex(StateError, "file"):
                slots.read()

    def test_read_rejects_a_hard_linked_atomic_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            ReleaseSlots(source).import_current(manifest("v1.0.0", "1"))
            target.mkdir()
            (target / "release-slots.json").hardlink_to(source / "release-slots.json")

            with self.assertRaisesRegex(StateError, "file"):
                ReleaseSlots(target).read()

    def test_failed_atomic_generation_commit_preserves_the_previous_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            slots = ReleaseSlots(Path(directory))
            first = manifest("v1.0.0", "1")
            second = manifest("v1.0.1", "2")
            slots.import_current(first)
            original = slots.read()

            with mock.patch("updater.slots._atomic_json", side_effect=OSError("injected commit failure")):
                with self.assertRaisesRegex(OSError, "injected"):
                    slots.promote(second, operation_id="a" * 32)

            self.assertEqual(slots.read(), original)

    def test_read_rejects_cross_slot_inconsistency_inside_an_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slots = ReleaseSlots(root)
            first = manifest("v1.0.0", "1")
            slots.import_current(first)
            envelope = json.loads((root / "release-slots.json").read_text(encoding="utf-8"))
            envelope["previous"] = first
            (root / "release-slots.json").write_text(json.dumps(envelope), encoding="utf-8")

            with self.assertRaisesRegex(StateError, "different releases"):
                slots.read()


if __name__ == "__main__":
    unittest.main()
