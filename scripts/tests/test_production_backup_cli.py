from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from durability import backup
from durability.backup_cli import (
    EXIT_CODES,
    _closed_public_record,
    _parser,
    _run_create,
    _run_verify,
)
from durability.backup_production import ProductionBackupError


class ProductionBackupCliContractTests(unittest.TestCase):
    def test_create_parser_exposes_closed_operator_contract(self) -> None:
        parser = _parser()
        args = parser.parse_args(
            [
                "backup",
                "create",
                "--instance",
                "blue",
                "--destination",
                "/srv/animemo-backups",
                "--one-time-key-output",
                "/run/operator/blue.key",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(args.instance, "blue")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.protection_kind, "one-time-key")

    def test_verify_parser_accepts_protected_inputs_without_secret_argv(self) -> None:
        parser = _parser()
        args = parser.parse_args(
            [
                "backup",
                "verify",
                "--backup",
                "/srv/backup",
                "--passphrase-fd",
                "7",
                "--json",
            ]
        )
        self.assertEqual(args.instance, "default")
        self.assertEqual(args.passphrase_fd, 7)
        self.assertNotIn("passphrase", parser.format_help())

    def test_inspect_parser_defaults_to_default_instance(self) -> None:
        args = _parser().parse_args(
            ["backup", "inspect", "--backup", "/srv/backup", "--json"]
        )
        self.assertEqual(args.instance, "default")

    def test_protection_group_is_required_and_mutually_exclusive(self) -> None:
        parser = _parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["backup", "create", "--dry-run"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "backup",
                    "create",
                    "--one-time-key-output",
                    "/a",
                    "--passphrase-file",
                    "/b",
                ]
            )

    def test_exit_codes_are_stable_and_distinct(self) -> None:
        self.assertEqual(
            EXIT_CODES,
            {
                "SUCCESS": 0,
                "USAGE": 2,
                "VALIDATION": 3,
                "COMPATIBILITY": 4,
                "RECOVERY": 5,
                "ENVIRONMENT": 6,
            },
        )

    def test_operator_output_is_closed_and_never_accepts_secret_material(self) -> None:
        self.assertEqual(
            _closed_public_record({"secretMode": "envelope"}),
            {"secretMode": "envelope"},
        )
        with self.assertRaisesRegex(ValueError, "closed public record"):
            _closed_public_record({"credential": "must-not-be-rendered"})

    def test_non_interactive_create_requires_explicit_acceptance(self) -> None:
        args = SimpleNamespace(
            instance="blue",
            destination=None,
            dry_run=False,
            non_interactive=True,
            accept=False,
            json=True,
            protection_kind="one-time-key",
            one_time_key_output=Path("/run/operator/key"),
            passphrase_file=None,
            passphrase_fd=None,
            secret_reference_file=None,
        )
        runtime = SimpleNamespace(
            plan=lambda **_kwargs: SimpleNamespace(
                plan_digest="sha256:" + "1" * 64,
                as_dict=dict,
            )
        )
        with (
            patch(
                "durability.backup_production.production_backup_runtime",
                return_value=runtime,
            ),
            self.assertRaisesRegex(
                ProductionBackupError, "BACKUP_PLAN_ACCEPTANCE_REQUIRED"
            ),
        ):
            _run_create(args)

    def test_verify_rejects_backup_from_another_instance(self) -> None:
        args = SimpleNamespace(
            backup=Path("/srv/backup"),
            instance="blue",
            protection_kind="one-time-key",
            one_time_key_file=Path("/run/operator/key"),
            passphrase_file=None,
            passphrase_fd=None,
            secret_reference_file=None,
            json=True,
        )
        with (
            patch(
                "durability.backup_production.verify_protected_backup",
                return_value={"sourceInstance": {"name": "green"}},
            ),
            self.assertRaisesRegex(
                ProductionBackupError, "BACKUP_INSTANCE_MISMATCH"
            ),
        ):
            _run_verify(args)


class BackupInspectionContractTests(unittest.TestCase):
    def test_inspect_is_manifest_only_and_never_claims_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "format": backup.FORMAT,
                "schemaVersion": backup.SCHEMA_VERSION,
                "backupId": "12345678-1234-4678-9234-567812345678",
                "startedAt": "2026-01-02T03:04:05Z",
                "completedAt": "2026-01-02T03:04:06Z",
                "source": {
                    "instanceName": "blue",
                    "instanceId": "11111111-2222-4333-8444-555555555555",
                    "release": {"version": "v1.1.0-rc.7", "commit": "a" * 40},
                },
                "filesystem": {"members": [{"path": "database.sql.gz"}]},
                "secrets": {"mode": "envelope"},
            }
            (root / backup.MANIFEST_NAME).write_bytes(
                (
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            result = backup.inspect_backup(root)
            self.assertEqual(result["sourceInstance"]["name"], "blue")
            self.assertEqual(result["memberCount"], 1)
            self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
