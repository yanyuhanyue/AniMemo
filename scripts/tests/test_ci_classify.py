from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.ci_classify import (
    SCHEMA_VERSION,
    changed_paths,
    classify_paths,
    force_full_for_event,
)

ROOT = Path(__file__).resolve().parents[2]


def parsed(result: dict[str, str]) -> dict[str, object]:
    return json.loads(result["classification_json"])


class CiClassificationTests(unittest.TestCase):
    def assert_risk(self, expected: str, path: str) -> dict[str, str]:
        result = classify_paths([path])
        self.assertEqual(result["risk_level"], expected, path)
        self.assertEqual(json.loads(result["unknown_paths"]), [], path)
        self.assertTrue(json.loads(result["matched_rules"]), path)
        return result

    def test_machine_readable_schema_is_versioned_and_consistent(self):
        result = classify_paths(["backend/journal/services.py"])
        document = parsed(result)

        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["risk"]["level"], result["risk_level"])
        self.assertEqual(str(document["risk"]["rank"]), result["risk_rank"])
        self.assertIs(document["execution"]["force_full"], False)
        self.assertEqual(result["execution_force_full"], "false")
        self.assertEqual(document["risk"]["reasons"], json.loads(result["reasons"]))
        self.assertEqual(document["paths"], json.loads(result["matched_rules"]))
        self.assertEqual(document["unknown_paths"], json.loads(result["unknown_paths"]))
        self.assertIs(document["signals"]["backend"], True)
        self.assertIs(document["gates"]["run_backend"], True)

    def test_safe_docs_only_remains_low_and_skips_product_gates(self):
        result = classify_paths(["docs/architecture.md", "README.md"])

        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["docs_only"], "true")
        for name in (
            "run_frontend",
            "run_backend",
            "run_plugins",
            "run_bridge",
            "run_postgres",
            "full_gate",
        ):
            self.assertEqual(result[name], "false", name)

    def test_root_legal_documents_are_classified_as_safe_documentation(self):
        paths = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES", "TRADEMARKS"]
        result = classify_paths(paths)

        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["docs_only"], "true")
        self.assertEqual(json.loads(result["unknown_paths"]), [])
        matched = json.loads(result["matched_rules"])
        self.assertEqual({entry["path"] for entry in matched}, set(paths))
        self.assertTrue(
            all("safe-documentation" in entry["rules"] for entry in matched)
        )

    def test_real_frozen_contract_docs_are_elevated(self):
        for path in (
            "docs/api-v1-contract.md",
            "docs/auth-contract.md",
            "docs/plugin-sdk-v2.md",
            "docs/plugin-sdk-contract.md",
            "docs/external-media-identity.md",
            "docs/integration-protocol-v1.md",
            "docs/release-contract-v1.md",
            "docs/update-agent-v1.md",
            "docs/release-gates.md",
            "docs/first-run-bootstrap.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    (ROOT / path).is_file(),
                    f"expected tracked contract fixture: {path}",
                )
                tracked = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", "--", path],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(tracked.stdout.strip(), path)
                result = self.assert_risk("HIGH", path)
                self.assertEqual(result["docs_only"], "false")
                self.assertEqual(result["full_gate"], "true")

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
        self.assertEqual(result["full_gate"], "true")

    def test_standard_product_changes_use_targeted_gates(self):
        cases = {
            "src/pages/Journal.jsx": "run_frontend",
            "backend/journal/services.py": "run_backend",
            "plugins/watch-history-importer/src/index.js": "run_plugins",
            "tests/navigation.test.mjs": "run_frontend",
        }
        for path, gate in cases.items():
            with self.subTest(path=path):
                result = self.assert_risk("STANDARD", path)
                self.assertEqual(result[gate], "true")
                self.assertEqual(result["full_gate"], "false")

    def test_high_risk_db_settings_security_and_contract_paths(self):
        paths = (
            "backend/journal/migrations/0006_add_index.py",
            "backend/journal/models.py",
            "backend/config/settings.py",
            "backend/accounts/authentication.py",
            "backend/journal/serializers_entries.py",
            "backend/plugin_host/sdk/types.py",
            "backend/integrations/services.py",
            "backend/site_config/media_storage/storage.py",
            "backend/journal/domain_services.py",
            "package-lock.json",
            "scripts/perf/regression_gate.py",
            "scripts/record-webm.mjs",
        )
        for path in paths:
            with self.subTest(path=path):
                result = self.assert_risk("HIGH", path)
                self.assertEqual(result["full_gate"], "true")

    def test_critical_release_updater_deployment_recovery_ci_and_first_run_paths(self):
        paths = (
            "release/contract.py",
            "scripts/tests/test_release_contract.py",
            "updater/agent.py",
            "updater/tests/test_executor.py",
            "deploy/docker-compose.yml",
            "durability/backup.py",
            "scripts/stateful-upgrade-gate.sh",
            ".github/workflows/ci.yml",
            "scripts/ci_classify.py",
            "scripts/ci_gate_authority.py",
            "scripts/tests/test_ci_classify.py",
            "backend/site_config/first_run.py",
            "backend/site_config/management/commands/bootstrap_animemo.py",
            "src/pages/SetupPage.jsx",
            "tests/first-run-setup.test.mjs",
        )
        for path in paths:
            with self.subTest(path=path):
                result = self.assert_risk("CRITICAL", path)
                self.assertEqual(result["full_gate"], "true")
                self.assertEqual(result["run_release_full"], "true")

    def test_high_and_critical_categories_expose_expected_signals(self):
        result = classify_paths(
            [
                "backend/journal/migrations/0006_add_index.py",
                "backend/accounts/authentication.py",
                "backend/journal/serializers_entries.py",
                "backend/plugin_host/sdk/types.py",
                "backend/integrations/services.py",
                "backend/site_config/media_storage/storage.py",
                "updater/agent.py",
            ]
        )
        for signal in (
            "backend",
            "auth",
            "api_contract",
            "plugin",
            "integration",
            "migration",
            "deployment",
            "media_storage",
        ):
            self.assertEqual(result[signal], "true", signal)
        self.assertEqual(result["mixed"], "true")
        self.assertEqual(result["risk_level"], "CRITICAL")

    def test_highest_matching_path_or_file_wins_deterministically(self):
        paths = ["README.md", "backend/journal/services.py", "release/contract.py"]
        forward = classify_paths(paths)
        reverse = classify_paths(list(reversed(paths)) + ["README.md"])

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["risk_level"], "CRITICAL")
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

    def test_unknown_path_fails_closed_with_explicit_evidence(self):
        path = "future-system/new-control-plane.bin"
        result = classify_paths([path])
        document = parsed(result)

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["full_gate"], "true")
        self.assertEqual(document["unknown_paths"], [path])
        self.assertEqual(document["paths"][0]["rules"], ["unknown-path-fail-closed"])
        self.assertIn("fail-closed", document["risk"]["reasons"][0]["reason"])

    def test_empty_change_set_fails_closed(self):
        result = classify_paths([])
        document = parsed(result)

        self.assertEqual(result["risk_level"], "CRITICAL")
        self.assertEqual(result["docs_only"], "false")
        self.assertEqual(result["full_gate"], "true")
        self.assertEqual(document["paths"], [])
        self.assertEqual(
            document["risk"]["reasons"][0]["rule"], "empty-change-set-fail-closed"
        )

    def test_all_repository_tracked_paths_have_audited_rules(self):
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
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

    def test_authority_events_force_execution_without_rewriting_change_risk(self):
        self.assertTrue(force_full_for_event("merge_group"))
        self.assertTrue(force_full_for_event("workflow_dispatch"))
        self.assertTrue(force_full_for_event("workflow_call"))
        self.assertTrue(force_full_for_event("pull_request", explicitly_forced=True))
        self.assertFalse(force_full_for_event("pull_request"))
        self.assertFalse(force_full_for_event("push"))

        result = classify_paths(["README.md"], force_full=True)
        document = parsed(result)
        self.assertEqual(result["risk_level"], "LOW")
        self.assertEqual(result["execution_force_full"], "true")
        self.assertEqual(result["docs_only"], "false")
        self.assertEqual(result["full_gate"], "true")
        self.assertEqual(result["critical_gate"], "true")
        self.assertIn(
            "authority-event-force-full",
            [reason["rule"] for reason in document["execution"]["reasons"]],
        )
        self.assertNotIn(
            "authority-event-force-full",
            [reason["rule"] for reason in document["risk"]["reasons"]],
        )

    def test_high_and_critical_release_subsets_are_distinct(self):
        high = classify_paths(["backend/journal/migrations/0006_add_index.py"])
        critical = classify_paths(["updater/agent.py"])

        self.assertEqual(high["risk_level"], "HIGH")
        self.assertEqual(high["run_release_docker"], "true")
        self.assertEqual(high["run_release_stateful"], "true")
        self.assertEqual(high["run_release_updater"], "false")
        self.assertEqual(high["critical_gate"], "false")

        self.assertEqual(critical["risk_level"], "CRITICAL")
        self.assertEqual(critical["run_release_docker"], "true")
        self.assertEqual(critical["run_release_stateful"], "true")
        self.assertEqual(critical["run_release_updater"], "true")
        self.assertEqual(critical["critical_gate"], "true")


if __name__ == "__main__":
    unittest.main()
