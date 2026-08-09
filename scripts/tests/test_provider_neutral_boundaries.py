import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (
    ROOT / "plugins",
    ROOT / "backend" / "plugin_host",
    ROOT / "backend" / "integrations",
)
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
FORBIDDEN_PROVIDER_APIS = (
    "astrbot/schema",
    "AstrBotGateway",
    "host.astrbot",
)


class ProviderNeutralBoundaryTests(unittest.TestCase):
    def test_core_and_official_plugins_have_no_astrbot_specific_api(self):
        findings = []
        for root in SCANNED_ROOTS:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                source = path.read_text(encoding="utf-8")
                for token in FORBIDDEN_PROVIDER_APIS:
                    if token in source:
                        findings.append(f"{path.relative_to(ROOT)}: {token}")
        self.assertEqual(findings, [], "Provider-specific API crossed the Integration boundary:\n" + "\n".join(findings))

    def test_watch_history_reference_plugin_keeps_generic_integration_actions(self):
        source = (ROOT / "plugins" / "watch-history-importer" / "backend" / "plugin.py").read_text(
            encoding="utf-8"
        )
        for action in ("history-get", "history-add", "entries-search", "import-preview", "import-commit"):
            self.assertIn(f'host.integrations.register_action("{action}"', source)


if __name__ == "__main__":
    unittest.main()
