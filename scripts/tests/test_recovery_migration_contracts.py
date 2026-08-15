from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCS = {
    "backup": ROOT / "docs" / "backup-contract-v1.md",
    "restore": ROOT / "docs" / "restore-contract-v1.md",
    "migration": ROOT / "docs" / "migration-bundle-v1.md",
    "envelope": ROOT / "docs" / "migration-secret-envelope-v1.md",
    "doctor": ROOT / "docs" / "doctor-basic-contract-v1.md",
    "compatibility": ROOT / "docs" / "compatibility-matrix-v1.md",
}


class RecoveryMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contracts = {
            name: path.read_text(encoding="utf-8") for name, path in DOCS.items()
        }

    def test_all_contracts_are_frozen_and_have_required_headers(self):
        required = (
            "Status: FROZEN FOR v1.1",
            "Version: v1",
            "Scope:",
            "Definitions:",
            "Non-goals:",
            "Dependencies:",
            "Security / Integrity implications:",
            "Compatibility:",
            "Change policy:",
        )
        for name, contract in self.contracts.items():
            normalized = contract.replace("**", "")
            for marker in required:
                with self.subTest(contract=name, marker=marker):
                    self.assertTrue(
                        marker in normalized,
                        f"{name} contract is missing required header: {marker}",
                    )

    def test_contract_set_is_cross_linked(self):
        phase_one = (
            "deployment-boundary-v1.md",
            "filesystem-layout-v1.md",
            "installer-contract-v1.md",
            "public-origin-listen-contract-v1.md",
        )
        for name, contract in self.contracts.items():
            for linked in phase_one:
                with self.subTest(contract=name, linked=linked):
                    self.assertIn(linked, contract)
            for other_name, other_path in DOCS.items():
                if other_name == name:
                    continue
                with self.subTest(contract=name, linked=other_path.name):
                    self.assertIn(other_path.name, contract)

    def test_compatibility_vocabulary_and_memory_integrity_are_shared(self):
        statuses = ("COMPATIBLE", "REQUIRES_UPGRADE", "UNSUPPORTED", "CORRUPT")
        memory_invariants = ("MI-1", "MI-2", "MI-3", "MI-4", "MI-5")
        for name, contract in self.contracts.items():
            for marker in (*statuses, *memory_invariants):
                with self.subTest(contract=name, marker=marker):
                    self.assertIn(marker, contract)

    def test_formats_have_independent_machine_identities(self):
        self.assertIn("animemo.compatibility/v1", self.contracts["compatibility"])
        self.assertIn("format: animemo-instance-backup", self.contracts["backup"])
        self.assertIn("schemaVersion: 1", self.contracts["backup"])
        self.assertIn("format: animemo-migration-bundle", self.contracts["migration"])
        self.assertIn("formatVersion: 1", self.contracts["migration"])
        self.assertIn(
            "animemo.migration-secret-envelope/v1", self.contracts["envelope"]
        )
        self.assertIn("reportFormat: animemo-doctor-report", self.contracts["doctor"])
        self.assertIn("reportVersion: 1", self.contracts["doctor"])

    def test_backup_freezes_logical_database_and_finalize_semantics(self):
        contract = self.contracts["backup"]
        for marker in (
            "pg_dump --format=plain --no-owner --no-privileges",
            "STAGING → VERIFY → FINALIZE",
            "checksums.sha256",
            "artifactBindingDigest",
            "Update Safety Backup",
            "Unknown R2 orphan",
        ):
            self.assertIn(marker, contract)
        self.assertIn("禁止 tar、rsync、snapshot 或复制 live", contract)

    def test_restore_freezes_state_machine_and_failure_boundary(self):
        contract = self.contracts["restore"]
        for marker in (
            "VERIFY",
            "COMPATIBILITY PLAN",
            "RESTORE",
            "VALIDATE",
            "PUBLISHED",
            "RECOVERY_REQUIRED",
            "Fresh",
            "Existing empty",
            "rotate_authentication_epoch --confirm-restore",
            "不得盲拷 source absolute paths",
            "不得启动公网服务",
        ):
            self.assertIn(marker, contract)

    def test_migration_freezes_identity_configuration_and_media_rules(self):
        contract = self.contracts["migration"]
        for marker in (
            "bundleId",
            "instanceId",
            "PRESERVE",
            "RECONFIGURE",
            "TARGET-LOCAL",
            "SAME_R2",
            "TRANSFER_REQUIRED",
            "artifactBindingDigest",
            "secrets/secret-envelope.json",
            "/data/anime-journal",
            "split-brain",
        ):
            self.assertIn(marker, contract)
        self.assertNotIn("envelope.bin", contract)
        self.assertNotIn("checksums.txt", contract)

    def test_secret_envelope_has_no_circular_secret_or_digest_trust(self):
        contract = self.contracts["envelope"]
        for marker in (
            "secrets/secret-envelope.json",
            "artifactBindingDigest",
            "memory-hard password KDF",
            "mature standard AEAD",
            "ENVELOPE_AUTHENTICATION_FAILED",
            "CREDENTIAL_ENCRYPTION_KEY",
            "no-echo interactive TTY",
            "不得声称已secure erase",
        ):
            self.assertIn(marker, contract)
        self.assertIn("MUST NOT", contract)
        self.assertIn("不是最终 Manifest digest", contract)

    def test_doctor_basic_is_read_only_and_secret_safe(self):
        contract = self.contracts["doctor"]
        for marker in (
            "mode: READ-ONLY",
            "PASS",
            "WARN",
            "FAIL",
            "SKIPPED",
            "instance.locator",
            "database.postgresql.connectivity",
            "cache.redis.connectivity",
            "release.identity",
            "backup.readiness",
            "不得报告 secret 长度、hash、fingerprint",
            "不得给 `doctor` 添加隐式 `--fix`",
        ):
            self.assertIn(marker, contract)

    def test_portability_terms_do_not_collapse_into_backup(self):
        context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")
        data_bundle = (ROOT / "docs" / "data-bundle-v1.md").read_text(
            encoding="utf-8"
        )
        for term in (
            "**Backup**:",
            "**Update Safety Backup**:",
            "**Restore**:",
            "**Migration**:",
            "**Migration Bundle**:",
            "**Migration Secret Envelope**:",
            "**Export**:",
            "**Compatibility Decision**:",
            "**Doctor Basic**:",
        ):
            self.assertIn(term, context)
        self.assertIn("Portable Export / Import", data_bundle)
        self.assertIn("不是 Backup、Restore 或 Migration Bundle", data_bundle)


if __name__ == "__main__":
    unittest.main()
