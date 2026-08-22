from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import dr_backup


@unittest.skipUnless(
    dr_backup._descriptor_relative_io_available(),
    "descriptor-relative directory binding unavailable",
)
class DisasterRecoveryBackupTests(unittest.TestCase):
    def make_source(self, root: Path) -> tuple[Path, dict[str, Path]]:
        database = root / "database.sql.gz"
        database.write_bytes(b"fake pg_dump\n")
        sources = {}
        for name in ("plugins", "media", "private", "updater_state"):
            path = root / name
            path.mkdir()
            (path / "nested").mkdir()
            (path / "nested" / f"{name}.txt").write_text(name, encoding="utf-8")
            sources[name] = path
        return database, sources

    def test_create_verify_restore_round_trip_and_manifest_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            backup = root / "backup"
            target = root / "restored"
            args = type(
                "Args",
                (),
                {
                    "output": str(backup),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            self.assertEqual(dr_backup.create(args), 0)
            dr_backup.verify_manifest(backup)
            manifest = json.loads((backup / dr_backup.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["members"]), set(dr_backup.MEMBERS))
            self.assertIn("env", manifest["exclusions"])

            restore_args = type("Args", (), {"backup_set": str(backup), "target_root": str(target)})()
            self.assertEqual(dr_backup.restore(restore_args), 0)
            self.assertEqual((target / "database.sql.gz").read_bytes(), database.read_bytes())
            self.assertEqual(
                (target / "plugins" / "nested" / "plugins.txt").read_text(encoding="utf-8"),
                "plugins",
            )

    def test_verify_rejects_tampered_member_and_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            backup = root / "backup"
            args = type(
                "Args",
                (),
                {
                    "output": str(backup),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            dr_backup.create(args)
            (backup / "media" / "nested" / "media.txt").write_text("tampered", encoding="utf-8")
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.verify_manifest(backup)

            link = root / "plugins-link"
            try:
                link.symlink_to(sources["plugins"], target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(dr_backup.BackupError):
                dr_backup._copy_tree(link, root / "copy", label="plugins")

    def test_restore_requires_empty_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            backup = root / "backup"
            args = type(
                "Args",
                (),
                {
                    "output": str(backup),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            dr_backup.create(args)
            target = root / "target"
            target.mkdir()
            (target / "existing").write_text("refuse", encoding="utf-8")
            restore_args = type("Args", (), {"backup_set": str(backup), "target_root": str(target)})()
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.restore(restore_args)

    def test_restore_rejects_member_changed_after_manifest_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            backup = root / "backup"
            target = root / "restored"
            create_args = type(
                "Args",
                (),
                {
                    "output": str(backup),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            dr_backup.create(create_args)
            real_verify = dr_backup._verify_manifest_bound

            def verify_then_change_member(source):
                manifest = real_verify(source)
                (backup / "plugins" / "nested" / "plugins.txt").write_text(
                    "tampered after verification",
                    encoding="utf-8",
                )
                return manifest

            restore_args = type(
                "Args",
                (),
                {"backup_set": str(backup), "target_root": str(target)},
            )()
            with (
                mock.patch.object(
                    dr_backup,
                    "_verify_manifest_bound",
                    side_effect=verify_then_change_member,
                ),
                self.assertRaisesRegex(dr_backup.BackupError, "integrity verification"),
            ):
                dr_backup.restore(restore_args)

    @unittest.skipUnless(
        dr_backup._descriptor_relative_io_available(),
        "descriptor-relative directory binding unavailable",
    )
    def test_create_rejects_source_and_output_root_rebinding_without_outside_write(self):
        for rebound_label in ("plugins", "output"):
            with self.subTest(rebound_label=rebound_label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database, sources = self.make_source(root)
                backup = root / "backup"
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_bytes(b"unchanged")
                detached = root / f"detached-{rebound_label}"
                args = type(
                    "Args",
                    (),
                    {
                        "output": str(backup),
                        "database_dump": str(database),
                        "plugins": str(sources["plugins"]),
                        "media": str(sources["media"]),
                        "private": str(sources["private"]),
                        "updater_state": str(sources["updater_state"]),
                    },
                )()
                real_enter = dr_backup.BoundEvidenceTree.__enter__
                rebound = False

                def enter_then_rebind(
                    bound,
                    *,
                    _real_enter=real_enter,
                    _label=rebound_label,
                    _detached=detached,
                    _outside=outside,
                ):
                    nonlocal rebound
                    result = _real_enter(bound)
                    if not rebound and bound.label == _label:
                        rebound = True
                        bound.original.rename(_detached)
                        bound.original.symlink_to(
                            _outside,
                            target_is_directory=True,
                        )
                    return result

                with (
                    mock.patch.object(
                        dr_backup.BoundEvidenceTree,
                        "__enter__",
                        enter_then_rebind,
                    ),
                    self.assertRaisesRegex(dr_backup.BackupError, "root changed"),
                ):
                    dr_backup.create(args)

                self.assertEqual(sentinel.read_bytes(), b"unchanged")
                self.assertEqual(list(outside.iterdir()), [sentinel])
                if rebound_label == "plugins":
                    self.assertFalse(backup.exists())
                else:
                    self.assertEqual(list(detached.iterdir()), [])

    def test_create_and_restore_reject_overlapping_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            args = type(
                "Args",
                (),
                {
                    "output": str(sources["plugins"] / "backup"),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.create(args)
            backup = root / "backup"
            args.output = str(backup)
            dr_backup.create(args)
            restore_args = type(
                "Args",
                (),
                {"backup_set": str(backup), "target_root": str(backup / "restored")},
            )()
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.restore(restore_args)

    def test_verify_rejects_manifest_contract_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            backup = root / "backup"
            args = type(
                "Args",
                (),
                {
                    "output": str(backup),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            dr_backup.create(args)
            manifest_path = backup / dr_backup.MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["exclusions"].pop("env")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.verify_manifest(backup)

    def test_create_rejects_symlink_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, sources = self.make_source(root)
            real_output = root / "real-output"
            real_output.mkdir()
            output_link = root / "output-link"
            try:
                output_link.symlink_to(real_output, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            args = type(
                "Args",
                (),
                {
                    "output": str(output_link),
                    "database_dump": str(database),
                    "plugins": str(sources["plugins"]),
                    "media": str(sources["media"]),
                    "private": str(sources["private"]),
                    "updater_state": str(sources["updater_state"]),
                },
            )()
            with self.assertRaises(dr_backup.BackupError):
                dr_backup.create(args)


class DisasterRecoveryPlatformGuardTests(unittest.TestCase):
    @unittest.skipIf(
        dr_backup._descriptor_relative_io_available(),
        "descriptor-relative directory binding is available",
    )
    def test_operations_fail_closed_without_descriptor_relative_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                self.assertRaisesRegex(
                    dr_backup.BackupError,
                    "descriptor-relative DR I/O is required",
                ),
                dr_backup.BoundEvidenceTree(root, label="test"),
            ):
                pass


if __name__ == "__main__":
    unittest.main()
