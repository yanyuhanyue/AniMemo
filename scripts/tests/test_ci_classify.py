from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.ci_classify import (
    AUDITED_CONTRACT_PRIMARY_DOCUMENTS,
    SCHEMA_VERSION,
    changed_paths,
    classify_paths,
    force_full_for_event,
)

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_GATES = (
    "run_frontend",
    "run_backend",
    "run_bootstrap",
    "run_plugins",
    "run_bridge",
    "run_postgres",
    "run_runtime",
)
RELEASE_GATES = (
    "run_release_updater",
    "run_release_docker",
    "run_release_stateful",
    "run_release_dr",
)
PR_93_PATHS = [
    "CONTEXT.md",
    "README.md",
    "docs/backup-contract-v1.md",
    "docs/compatibility-matrix-v1.md",
    "docs/data-bundle-v1.md",
    "docs/doctor-basic-contract-v1.md",
    "docs/migration-bundle-v1.md",
    "docs/migration-secret-envelope-v1.md",
    "docs/restore-contract-v1.md",
    "scripts/tests/test_recovery_migration_contracts.py",
]


def parsed(result: dict[str, str]) -> dict[str, object]:
    return json.loads(result["classification_json"])


class CiClassificationTests(unittest.TestCase):
    def assert_risk(self, expected: str, path: str) -> dict[str, str]:
        result = classify_paths([path])
        self.assertEqual(result["risk_level"], expected, path)
        self.assertEqual(json.loads(result["unknown_paths"]), [], path)
        self.assertTrue(json.loads(result["matched_rules"]), path)
        return result

    def assert_false(self, result: dict[str, str], *names: str) -> None:
        for name in names:
            self.assertEqual(result[name], "false", name)

    def assert_true(self, result: dict[str, str], *names: str) -> None:
        for name in names:
            self.assertEqual(result[name], "true", name)

    def test_install_portal_current_and_retired_paths_remain_critical(self):
        for path in (
            "sites/install-portal/index.html",
            "sites/install-portal/release-state.mjs",
            "sites/install-portal/_headers",
            "install.animemo.cc/index.html",
        ):
            with self.subTest(path=path):
                result = self.assert_risk("CRITICAL", path)
                document = parsed(result)
                matched = document["paths"][0]
                self.assertIn("install-portal-bootstrap", matched["rules"])
                self.assertEqual(result["execution_profile"], "TARGETED")
                self.assert_true(result, "release", "tooling", "critical_gate")

    def test_machine_readable_schema_v2_is_consistent(self):
        result = classify_paths(["backend/journal/services.py"])
        document = parsed(result)

        self.assertEqual(SCHEMA_VERSION, "animemo.ci-risk/v2")
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["risk"]["level"], result["risk_level"])
        self.assertEqual(str(document["risk"]["rank"]), result["risk_rank"])
        self.assertEqual(document["execution"]["profile"], "TARGETED")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assertIs(document["execution"]["force_full"], False)
        self.assertEqual(result["execution_force_full"], "false")
        self.assertEqual(document["risk"]["reasons"], json.loads(result["reasons"]))
        self.assertEqual(document["paths"], json.loads(result["matched_rules"]))
        self.assertEqual(document["unknown_paths"], json.loads(result["unknown_paths"]))
        self.assertIs(document["signals"]["backend"], True)
        self.assertIs(document["gates"]["run_backend"], True)

    def test_case_a_safe_docs_only(self):
        result = classify_paths(["docs/architecture.md", "README.md"])

        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["execution_profile"], "DOCS_ONLY")
        self.assert_true(result, "docs_only")
        self.assert_false(
            result,
            "run_contract_validation",
            *PRODUCT_GATES,
            *RELEASE_GATES,
            "full_gate",
            "critical_gate",
        )

    def test_root_legal_documents_are_safe_docs(self):
        paths = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES", "TRADEMARKS"]
        result = classify_paths(paths)

        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["execution_profile"], "DOCS_ONLY")
        self.assertEqual(json.loads(result["unknown_paths"]), [])
        matched = json.loads(result["matched_rules"])
        self.assertEqual({entry["path"] for entry in matched}, set(paths))
        self.assertTrue(
            all("safe-documentation" in entry["rules"] for entry in matched)
        )

    def test_case_b_sensitive_contract_docs_only(self):
        result = classify_paths(["docs/backup-contract-v1.md"])

        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(result["execution_profile"], "CONTRACT_VALIDATION_ONLY")
        self.assert_true(result, "run_contract_validation")
        self.assert_false(
            result, "docs_only", *PRODUCT_GATES, *RELEASE_GATES, "full_gate"
        )

    def test_case_c_pr_93_is_contract_validation_only(self):
        result = classify_paths(PR_93_PATHS)
        document = parsed(result)

        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(result["execution_profile"], "CONTRACT_VALIDATION_ONLY")
        self.assert_true(result, "run_contract_validation")
        self.assert_false(
            result,
            "recovery",
            "ci",
            "docs_only",
            *PRODUCT_GATES,
            *RELEASE_GATES,
            "full_gate",
            "critical_gate",
        )
        validation_record = next(
            record
            for record in document["paths"]
            if record["path"] == "scripts/tests/test_recovery_migration_contracts.py"
        )
        self.assertEqual(
            validation_record["rules"], ["audited-contract-validation-test"]
        )

    def test_contract_support_paths_without_primary_doc_are_not_contract_only(self):
        result = classify_paths(
            ["README.md", "scripts/tests/test_recovery_migration_contracts.py"]
        )

        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_false(result, "run_contract_validation", "recovery", "ci")

    def test_case_d_mixed_contract_and_runtime_exits_contract_profile(self):
        result = classify_paths(
            ["docs/backup-contract-v1.md", "durability/backup.py"]
        )

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result, "recovery", "run_plugins", "run_postgres", "run_release_dr"
        )
        self.assert_false(
            result,
            "run_contract_validation",
            "run_release_stateful",
            "run_release_docker",
            "full_gate",
        )

    def test_case_e_updater_is_critical_and_targeted(self):
        result = classify_paths(["updater/agent.py"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result,
            "updater",
            "run_bootstrap",
            "run_release_updater",
            "run_release_stateful",
            "critical_gate",
        )
        self.assert_false(
            result,
            "run_release_full",
            "run_release_docker",
            "run_release_dr",
            "full_gate",
        )

    def test_phase3c_paths_are_known_critical_and_select_required_gates(self):
        cases = {
            "installer/restore_production.py": (
                "deployment",
                "release",
                "updater",
                "recovery",
            ),
            "durability/managed_config.py": (
                "deployment",
                "updater",
                "recovery",
            ),
            "updater/configuration.py": (
                "deployment",
                "updater",
                "recovery",
            ),
            "scripts/platform_qualification.py": (
                "deployment",
                "release",
                "recovery",
            ),
            "release/materials.py": ("release",),
        }
        for path, signals in cases.items():
            with self.subTest(path=path):
                result = self.assert_risk("CRITICAL", path)
                self.assertEqual(result["execution_profile"], "TARGETED")
                self.assert_true(result, *signals)
                self.assert_false(result, "run_release_full", "full_gate")

    def test_case_f_database_migration_selects_postgres_and_stateful(self):
        result = classify_paths(["backend/journal/migrations/9999_gate_test.py"])

        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result,
            "backend",
            "database",
            "run_backend",
            "run_postgres",
            "run_release_stateful",
        )
        self.assert_false(
            result,
            "run_release_full",
            "run_release_docker",
            "run_release_dr",
            "full_gate",
            "critical_gate",
        )

    def test_auth_security_paths_remain_strongly_targeted(self):
        for path in (
            "backend/accounts/authentication.py",
            "backend/journal/security.py",
            "backend/journal/token_service.py",
        ):
            with self.subTest(path=path):
                result = classify_paths([path])
                self.assertEqual(result["risk_level"], "HIGH")
                self.assert_true(
                    result, "auth", "backend", "run_backend", "run_postgres"
                )
                self.assert_false(result, "full_gate")

    def test_case_g_recovery_runtime_selects_dr_not_stateful(self):
        result = classify_paths(["scripts/dr_recovery_paths.py"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result, "recovery", "run_plugins", "run_postgres", "run_release_dr"
        )
        self.assert_false(
            result, "run_release_stateful", "run_release_docker", "full_gate"
        )

    def test_production_backup_tests_select_recovery_dr_gate(self):
        result = classify_paths(["scripts/tests/test_production_backup_runtime.py"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assert_true(
            result, "recovery", "run_plugins", "run_postgres", "run_release_dr"
        )

    def test_case_h_deployment_runtime_selects_docker_and_stateful(self):
        result = classify_paths(["deploy/docker-compose.yml"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result,
            "deployment",
            "run_bootstrap",
            "run_release_docker",
            "run_release_stateful",
        )
        self.assert_false(result, "run_release_dr", "full_gate")

    def test_stateful_runtime_is_explicit_and_does_not_select_dr(self):
        result = classify_paths(["scripts/stateful-upgrade-gate.sh"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result, "deployment", "run_release_docker", "run_release_stateful"
        )
        self.assert_false(result, "recovery", "run_release_dr", "full_gate")

    def test_case_i_ci_authority_is_critical_targeted(self):
        result = classify_paths(["scripts/ci_gate_authority.py"])

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(result, "ci", "tooling", "run_bootstrap", "critical_gate")
        self.assert_false(
            result,
            "auth",
            "backend",
            "run_backend",
            "run_postgres",
            "run_release_full",
            "run_release_updater",
            "run_release_docker",
            "run_release_stateful",
            "run_release_dr",
            "full_gate",
        )

    def test_case_j_unknown_path_is_conservative_broad_targeted(self):
        path = "future-system/new-control-plane.bin"
        result = classify_paths([path])
        document = parsed(result)

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assertEqual(document["unknown_paths"], [path])
        self.assertEqual(document["paths"][0]["rules"], ["unknown-path-fail-closed"])
        self.assert_true(result, *PRODUCT_GATES, *RELEASE_GATES, "critical_gate")
        self.assert_false(result, "run_release_full", "full_gate")

    def test_empty_change_set_is_conservative_broad_targeted(self):
        result = classify_paths([])
        document = parsed(result)

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assertEqual(document["paths"], [])
        self.assertEqual(
            document["risk"]["reasons"][0]["rule"], "empty-change-set-fail-closed"
        )
        self.assert_true(result, *PRODUCT_GATES, *RELEASE_GATES, "critical_gate")
        self.assert_false(result, "run_release_full", "full_gate")

    def test_case_k_force_full_selects_every_full_gate_without_rewriting_risk(self):
        result = classify_paths(["README.md"], force_full=True)
        document = parsed(result)

        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["execution_profile"], "FULL_AUTHORITY")
        self.assertEqual(result["execution_force_full"], "true")
        self.assert_true(
            result,
            *PRODUCT_GATES,
            "run_release_full",
            *RELEASE_GATES,
            "full_gate",
        )
        self.assert_false(
            result, "docs_only", "run_contract_validation", "critical_gate"
        )
        self.assertEqual(
            document["execution"]["reasons"][0]["rule"], "authority-force-full"
        )

    def test_phase_3a_runtime_paths_remain_runtime_targeted(self):
        result = classify_paths(
            [
                "durability/backup.py",
                "durability/doctor.py",
                "durability/secret_envelope.py",
                "durability/compatibility.py",
            ]
        )

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assert_true(
            result, "recovery", "run_plugins", "run_postgres", "run_release_dr"
        )
        self.assert_false(
            result,
            "auth",
            "backend",
            "run_backend",
            "run_contract_validation",
            "full_gate",
        )

    def test_negative_filename_and_allowlist_spoof_cases(self):
        cases = (
            ("docs/not-a-real-contract.md", "HIGH", False, False),
            ("scripts/tests/test_recovery_notes.py", "HIGH", True, False),
            ("scripts/tests/test_future_contract.py", "HIGH", True, False),
            ("scripts/new_selector.py", "HIGH", True, False),
            ("docs/backup-contract-v1.md.copy", "HIGH", False, False),
            (
                "scripts/tests/test_recovery_migration_contracts.py.bak",
                "HIGH",
                True,
                False,
            ),
        )
        for path, risk, ci_signal, recovery_signal in cases:
            with self.subTest(path=path):
                result = classify_paths([path])
                self.assertEqual(result["risk_level"], risk)
                self.assertEqual(result["execution_profile"], "TARGETED")
                self.assertEqual(result["run_contract_validation"], "false")
                self.assertEqual(result["ci"], str(ci_signal).lower())
                self.assertEqual(result["recovery"], str(recovery_signal).lower())

    def test_non_phase2_sensitive_contracts_are_high_but_not_auto_full(self):
        for path in (
            "docs/api-v1-contract.md",
            "docs/auth-contract.md",
            "docs/plugin-sdk-v2.md",
            "docs/release-contract-v1.md",
            "docs/update-agent-v1.md",
            "docs/release-gates.md",
            "docs/first-run-bootstrap.md",
        ):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file(), path)
                result = self.assert_risk("HIGH", path)
                self.assertEqual(result["execution_profile"], "TARGETED")
                self.assert_false(result, "run_contract_validation", "full_gate")

    def test_dependency_inputs_select_owning_components(self):
        cases = {
            "package-lock.json": ("HIGH", ("frontend", "plugin")),
            "backend/pip-bootstrap.lock": ("HIGH", ("backend", "database")),
            "backend/container-requirements.lock": (
                "HIGH",
                ("backend", "database"),
            ),
            "backend/requirements.lock": ("HIGH", ("backend", "database")),
            "backend/requirements.txt": ("HIGH", ("backend", "database")),
            "bridges/astrbot_plugin_animemo_bridge/requirements.lock": (
                "HIGH",
                ("bridge", "integration"),
            ),
            "bridges/astrbot_plugin_animemo_bridge/requirements.txt": (
                "HIGH",
                ("bridge", "integration"),
            ),
            "durability/requirements.lock": ("CRITICAL", ("recovery",)),
            "durability/requirements.txt": ("CRITICAL", ("recovery",)),
            "release/requirements.lock": ("CRITICAL", ("release",)),
            "release/requirements.txt": ("CRITICAL", ("release",)),
            "scripts/requirements-tools.lock": ("HIGH", ("ci", "tooling")),
        }
        for path, (expected_risk, expected_signals) in cases.items():
            with self.subTest(path=path):
                result = self.assert_risk(expected_risk, path)
                for signal in expected_signals:
                    self.assertEqual(result[signal], "true", signal)
                self.assertEqual(result["execution_profile"], "TARGETED")
                self.assertEqual(result["full_gate"], "false")

    def test_highest_matching_path_wins_deterministically(self):
        paths = ["README.md", "backend/journal/services.py", "release/contract.py"]
        forward = classify_paths(paths)
        reverse = classify_paths(list(reversed(paths)) + ["README.md"])

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["risk_level"], "CRITICAL")
        self.assertEqual(forward["execution_profile"], "TARGETED")
        self.assertEqual(
            [entry["path"] for entry in json.loads(forward["matched_rules"])],
            sorted(set(paths)),
        )

    def test_windows_paths_are_normalized_before_matching(self):
        result = classify_paths([r"backend\journal\migrations\0006_add_index.py"])
        self.assertEqual(result["risk_level"], "HIGH")
        self.assertEqual(
            json.loads(result["matched_rules"])[0]["path"],
            "backend/journal/migrations/0006_add_index.py",
        )

    def test_sensitive_source_path_survives_real_git_rename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.name", "AniMemo CI Test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "ci-test@invalid.example"],
                cwd=repository,
                check=True,
            )
            source = repository / "updater" / "agent.py"
            source.parent.mkdir(parents=True)
            source.write_text("RELEASE_AUTHORITY = True\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "updater/agent.py"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "add updater"],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            destination = repository / "backend" / "journal" / "agent.py"
            destination.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "mv", "updater/agent.py", "backend/journal/agent.py"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "move updater"],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            previous_directory = Path.cwd()
            try:
                os.chdir(repository)
                paths = changed_paths(base, head)
            finally:
                os.chdir(previous_directory)

        self.assertEqual(paths, ["backend/journal/agent.py", "updater/agent.py"])
        result = classify_paths(paths)
        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["execution_profile"], "TARGETED")
        self.assertEqual(result["run_release_updater"], "true")
        self.assertEqual(result["full_gate"], "false")

    def test_changed_paths_rejects_untrusted_revision_inputs(self):
        valid_sha = "a" * 40
        cases = (
            ("--output=outside", valid_sha),
            ("abc123", valid_sha),
            (valid_sha, "--help"),
            (valid_sha, "f" * 39),
            (valid_sha, "0" * 40),
        )

        with mock.patch("scripts.ci_classify.subprocess.run") as run:
            for base, head in cases:
                with (
                    self.subTest(base=base, head=head),
                    self.assertRaisesRegex(ValueError, "40-character commit SHA"),
                ):
                    changed_paths(base, head)
            run.assert_not_called()

    def test_changed_paths_canonicalizes_shas_and_uses_git_argument_boundary(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"README.md\0")
        with mock.patch("scripts.ci_classify.subprocess.run", return_value=completed) as run:
            self.assertEqual(changed_paths("A" * 40, "b" * 40), ["README.md"])

        run.assert_called_once_with(
            [
                "git",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                f"{'a' * 40}...{'b' * 40}",
                "--",
            ],
            check=True,
            capture_output=True,
        )

    def test_changed_paths_accepts_zero_base_only_for_initial_push(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"package-lock.json\0")
        with mock.patch("scripts.ci_classify.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                changed_paths("0" * 40, "C" * 40),
                ["package-lock.json"],
            )

        run.assert_called_once_with(
            [
                "git",
                "diff-tree",
                "--no-commit-id",
                "--no-renames",
                "--name-only",
                "-r",
                "-z",
                "c" * 40,
                "--",
            ],
            check=True,
            capture_output=True,
        )

    def test_all_repository_tracked_paths_have_audited_rules(self):
        completed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        )
        tracked = [
            path.decode("utf-8") for path in completed.stdout.split(b"\0") if path
        ]
        document = parsed(classify_paths(tracked))
        matched = {entry["path"]: entry["rules"] for entry in document["paths"]}

        self.assertGreater(len(tracked), 600)
        self.assertEqual(document["unknown_paths"], [])
        self.assertEqual(set(matched), set(tracked))
        self.assertTrue(all(matched[path] for path in tracked))

    def test_phase2_primary_allowlist_matches_six_frozen_documents(self):
        self.assertEqual(
            AUDITED_CONTRACT_PRIMARY_DOCUMENTS,
            {
                "docs/backup-contract-v1.md",
                "docs/compatibility-matrix-v1.md",
                "docs/doctor-basic-contract-v1.md",
                "docs/migration-bundle-v1.md",
                "docs/migration-secret-envelope-v1.md",
                "docs/restore-contract-v1.md",
            },
        )

    def test_audited_contract_documents_retain_high_risk(self):
        for path in sorted(AUDITED_CONTRACT_PRIMARY_DOCUMENTS):
            with self.subTest(path=path):
                result = parsed(classify_paths([path]))
                self.assertEqual(result["risk"]["level"], "HIGH")

    def test_authority_force_is_explicit_or_merge_group_only(self):
        self.assertTrue(force_full_for_event("merge_group"))
        self.assertTrue(
            force_full_for_event("workflow_dispatch", explicitly_forced=True)
        )
        self.assertTrue(force_full_for_event("workflow_call", explicitly_forced=True))
        self.assertTrue(force_full_for_event("pull_request", explicitly_forced=True))
        self.assertFalse(force_full_for_event("workflow_dispatch"))
        self.assertFalse(force_full_for_event("workflow_call"))
        self.assertFalse(force_full_for_event("pull_request"))
        self.assertFalse(force_full_for_event("push"))


if __name__ == "__main__":
    unittest.main()
