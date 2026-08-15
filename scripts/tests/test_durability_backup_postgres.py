from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from durability import backup


class IsolatedPostgreSQLBackupTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("ANIMEMO_TEST_DATABASE_URL") and shutil.which("pg_dump"),
        "requires coordinator-provided isolated ANIMEMO_TEST_DATABASE_URL and pg_dump",
    )
    def test_real_pg_dump_is_invoked_and_verified_without_restore(self) -> None:
        database_url = os.environ["ANIMEMO_TEST_DATABASE_URL"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for logical_root in (
                "filesystem/config",
                "filesystem/plugins/cas",
                "filesystem/plugins/durable",
                "filesystem/media",
                "filesystem/private",
                "updater-state",
            ):
                source = root / "sources" / logical_root.replace("/", "-")
                source.mkdir(parents=True)
                sources[logical_root] = source
            (sources["filesystem/config"] / "contract.json").write_text("{}\n", encoding="utf-8")
            request = backup.BackupRequest(
                destination_root=root / "backups",
                database_url=database_url,
                source=backup.BackupSourceIdentity(
                    instance_id="11111111-2222-4333-8444-555555555555",
                    source_locator_digest="sha256:" + "1" * 64,
                    release={"version": "1.1.0", "commit": "a" * 40},
                    deployment_contract={"schemaVersion": 1, "digest": "sha256:" + "2" * 64},
                    database_contract={"id": "animemo.database/v1", "serverMajor": 16},
                    configuration_contract={"id": "animemo.configuration/v1"},
                ),
                filesystem_sources=tuple(
                    backup.FilesystemSource(logical_root=logical_root, source=source)
                    for logical_root, source in sources.items()
                ),
                producer={"name": "animemo-durability", "version": "1.1.0"},
                platform={"os": "linux", "architecture": "amd64"},
                quiescence={"method": "coordinator-isolated-postgresql"},
            )
            moments = iter(
                (
                    datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                    datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
                )
            )
            result = backup.create_backup(
                request,
                backup_id=uuid.UUID("12345678-1234-5678-9234-567812345678"),
                clock=lambda: next(moments),
            )
            verification = backup.verify_backup(result.path)
            self.assertEqual(verification.backup_id, str(result.backup_id))
            self.assertGreater(verification.database_uncompressed_bytes, 0)


if __name__ == "__main__":
    unittest.main()
