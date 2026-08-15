from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE_HOST = ROOT / "deploy/prepare-host.sh"
APP_UID = 10001
APP_GID = 10001
UPDATER_UID = 20001
UNRELATED_UID = 20002


def _has_passwordless_sudo() -> bool:
    if sys.platform != "linux" or shutil.which("sudo") is None:
        return False
    return subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        check=False,
    ).returncode == 0


@unittest.skipUnless(_has_passwordless_sudo(), "requires Linux with passwordless sudo")
class PrepareHostPermissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="animemo-prepare-host-"))
        self.root.chmod(0o755)

    def tearDown(self):
        temporary_root = Path(tempfile.gettempdir()).resolve()
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, temporary_root)
        self.assertTrue(resolved.name.startswith("animemo-prepare-host-"))
        subprocess.run(
            [
                "sudo",
                "-n",
                sys.executable,
                "-c",
                "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)",
                str(resolved),
            ],
            check=True,
        )

    def _run_prepare_host(self, data_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "sudo",
                "-n",
                "env",
                f"ANIMEMO_DATA_ROOT={data_root}",
                "sh",
                str(PREPARE_HOST),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_as(
        self,
        uid: int,
        gid: int,
        code: str,
        *arguments: Path,
    ) -> subprocess.CompletedProcess[str]:
        drop_privileges = (
            "import os,sys; "
            "os.setgroups([]); "
            "os.setgid(int(sys.argv[1])); "
            "os.setuid(int(sys.argv[2])); "
        )
        return subprocess.run(
            [
                "sudo",
                "-n",
                sys.executable,
                "-c",
                drop_privileges + code,
                str(gid),
                str(uid),
                *(str(argument) for argument in arguments),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _assert_directory(self, path: Path, *, uid: int, gid: int, mode: int) -> None:
        metadata = path.stat()
        self.assertTrue(stat.S_ISDIR(metadata.st_mode), path)
        self.assertEqual(metadata.st_uid, uid, path)
        self.assertEqual(metadata.st_gid, gid, path)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), mode, path)

    def _root_file_metadata(self, path: Path) -> tuple[int, int, int]:
        result = subprocess.run(
            ["sudo", "-n", "stat", "-c", "%u %g %a", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        uid, gid, mode = result.stdout.split()
        return int(uid), int(gid), int(mode, 8)

    def _root_path_exists(self, path: Path) -> bool:
        return subprocess.run(
            ["sudo", "-n", "test", "-e", str(path)],
            check=False,
        ).returncode == 0

    def _assert_all_directory_contracts(self, data_root: Path) -> None:
        for name in ("plugins", "logs", "media"):
            self._assert_directory(
                data_root / name,
                uid=APP_UID,
                gid=APP_GID,
                mode=0o755,
            )
        self._assert_directory(
            data_root / "backups",
            uid=APP_UID,
            gid=APP_GID,
            mode=0o770,
        )
        self._assert_directory(
            data_root / "private",
            uid=APP_UID,
            gid=APP_GID,
            mode=0o700,
        )

    def test_fresh_backup_directory_allows_only_the_contract_group_to_write(self):
        data_root = self.root / "fresh"

        result = self._run_prepare_host(data_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_all_directory_contracts(data_root)
        group_probe = data_root / "backups" / "group-writer"
        renamed_probe = data_root / "backups" / "group-writer-renamed"
        group_write = self._run_as(
            UPDATER_UID,
            APP_GID,
            (
                "from pathlib import Path; import os; "
                "source=Path(sys.argv[3]); target=Path(sys.argv[4]); "
                "descriptor=os.open(source,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                "os.write(descriptor,b'payload'); os.fsync(descriptor); os.close(descriptor); "
                "source.rename(target); assert target.read_bytes()==b'payload'; target.unlink()"
            ),
            group_probe,
            renamed_probe,
        )
        self.assertEqual(group_write.returncode, 0, group_write.stderr)
        self.assertFalse(self._root_path_exists(group_probe))
        self.assertFalse(self._root_path_exists(renamed_probe))

        unrelated_probe = data_root / "backups" / "unrelated-writer"
        unrelated_write = self._run_as(
            UNRELATED_UID,
            UNRELATED_UID,
            "from pathlib import Path; Path(sys.argv[3]).write_bytes(b'denied')",
            unrelated_probe,
        )
        self.assertNotEqual(unrelated_write.returncode, 0)
        self.assertFalse(self._root_path_exists(unrelated_probe))

    def test_existing_backup_artifact_survives_repair_and_idempotent_rerun(self):
        data_root = self.root / "existing"
        backup_root = data_root / "backups"
        artifact = backup_root / "animemo-pre-existing.sql.gz"
        setup = (
            "from pathlib import Path; import os,sys; "
            "root=Path(sys.argv[1]); artifact=Path(sys.argv[2]); "
            "root.mkdir(parents=True); artifact.write_bytes(b'existing backup'); "
            f"os.chown(root,{APP_UID},{APP_GID}); os.chmod(root,0o755); "
            f"os.chown(artifact,{UPDATER_UID},{APP_GID}); os.chmod(artifact,0o600)"
        )
        subprocess.run(
            ["sudo", "-n", sys.executable, "-c", setup, str(backup_root), str(artifact)],
            check=True,
        )

        first = self._run_prepare_host(data_root)
        second = self._run_prepare_host(data_root)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self._assert_all_directory_contracts(data_root)
        self.assertEqual(
            self._root_file_metadata(artifact),
            (UPDATER_UID, APP_GID, 0o600),
        )
        updater_read = self._run_as(
            UPDATER_UID,
            APP_GID,
            "from pathlib import Path; assert Path(sys.argv[3]).read_bytes()==b'existing backup'",
            artifact,
        )
        self.assertEqual(updater_read.returncode, 0, updater_read.stderr)

    def test_backup_link_and_non_directory_fail_closed_before_metadata_changes(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        original = outside.stat()

        symlink_root = self.root / "symlink"
        symlink_root.mkdir()
        (symlink_root / "backups").symlink_to(outside, target_is_directory=True)
        symlink_result = self._run_prepare_host(symlink_root)

        self.assertNotEqual(symlink_result.returncode, 0)
        self.assertIn("Backup path must not be a symbolic link", symlink_result.stderr)
        current = outside.stat()
        self.assertEqual(
            (current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)),
            (original.st_uid, original.st_gid, stat.S_IMODE(original.st_mode)),
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

        file_root = self.root / "file"
        file_root.mkdir()
        backup_file = file_root / "backups"
        backup_file.write_text("unchanged", encoding="utf-8")
        file_result = self._run_prepare_host(file_root)

        self.assertNotEqual(file_result.returncode, 0)
        self.assertIn("Backup path must be a directory", file_result.stderr)
        self.assertEqual(backup_file.read_text(encoding="utf-8"), "unchanged")
