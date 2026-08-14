from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import dr_backup


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


if __name__ == "__main__":
    unittest.main()
