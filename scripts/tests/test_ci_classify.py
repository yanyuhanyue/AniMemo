import unittest

from scripts.ci_classify import classify_paths


class CiClassificationTests(unittest.TestCase):
    def test_docs_only_skips_product_gates(self):
        result = classify_paths(["docs/architecture.md", "README.md"])
        self.assertEqual(result["docs_only"], "true")
        for name in ("run_frontend", "run_backend", "run_plugins", "run_bridge", "run_postgres", "full_gate"):
            self.assertEqual(result[name], "false", name)

    def test_frontend_change_only_runs_frontend(self):
        result = classify_paths(["src/pages/Journal.jsx"])
        self.assertEqual(result["run_frontend"], "true")
        self.assertEqual(result["run_backend"], "false")
        self.assertEqual(result["run_postgres"], "false")

    def test_backend_change_runs_backend_without_full_gate(self):
        result = classify_paths(["backend/journal/services.py"])
        self.assertEqual(result["run_backend"], "true")
        self.assertEqual(result["run_postgres"], "false")
        self.assertEqual(result["full_gate"], "false")

    def test_high_risk_contract_changes_run_full_gate(self):
        for path in (
            "backend/accounts/authentication.py",
            "backend/journal/serializers_entries.py",
            "backend/journal/migrations/0002_add.py",
            ".github/workflows/ci.yml",
            "backend/requirements.in",
        ):
            with self.subTest(path=path):
                self.assertEqual(classify_paths([path])["full_gate"], "true")

    def test_plugin_bridge_and_integration_risk_is_visible(self):
        result = classify_paths([
            "plugins/watch-history-importer/manifest.json",
            "bridges/astrbot_plugin_animemo_bridge/plugin.py",
            "backend/integrations/services.py",
        ])
        self.assertEqual(result["plugin"], "true")
        self.assertEqual(result["bridge"], "true")
        self.assertEqual(result["integration"], "true")
        self.assertEqual(result["mixed"], "true")
        self.assertEqual(result["full_gate"], "true")

    def test_merge_group_force_full(self):
        result = classify_paths(["src/App.jsx"], force_full=True)
        self.assertEqual(result["full_gate"], "true")
        for name in ("run_backend", "run_plugins", "run_bridge", "run_postgres", "run_bootstrap"):
            self.assertEqual(result[name], "true", name)


if __name__ == "__main__":
    unittest.main()
