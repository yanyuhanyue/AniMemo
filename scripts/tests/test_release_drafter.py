from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / ".github" / "release-drafter.yml"
NATIVE_CONFIG_PATH = ROOT / ".github" / "release.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-drafter.yml"


class ReleaseDrafterConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.native_config = yaml.safe_load(
            NATIVE_CONFIG_PATH.read_text(encoding="utf-8")
        )
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_required_changelog_categories_are_exclusive_and_ordered(self) -> None:
        categories = self.config["categories"]
        self.assertEqual(categories[0]["type"], "pre-exclude")
        self.assertEqual(categories[0]["when"], {"label": "skip-changelog"})
        self.assertEqual(categories[1]["type"], "pre-exclude")
        self.assertEqual(categories[1]["when"], {"label": "release/internal"})

        expected = [
            ("⚠️ Breaking Changes", {"release/breaking"}),
            ("🔒 安全与稳定性", {"release/security"}),
            ("✨ 新增功能", {"release/feature"}),
            ("🐛 Bug 修复", {"release/fix"}),
            ("🎨 界面与交互", {"release/ui"}),
            ("💡 功能与体验优化", {"release/improvement"}),
            ("🚀 性能与代码改进", {"release/performance", "release/refactor"}),
            ("🧱 部署与运维", {"release/deployment"}),
            ("🧰 CI / 工程化", {"release/ci"}),
            ("⬆️ 依赖更新", {"release/dependencies"}),
            ("📝 文档", {"release/docs"}),
        ]
        actual = []
        for category in categories[2:-1]:
            when = category["when"]
            labels = set(when.get("labels", [when.get("label")]))
            actual.append((category["title"], labels))
            self.assertIs(category["exclusive"], True)

        self.assertEqual(actual, expected)
        self.assertEqual(categories[-1], {"title": "📦 其他变更"})

    def test_native_release_notes_fallback_has_the_same_category_contract(self) -> None:
        changelog = self.native_config["changelog"]
        self.assertIn("skip-changelog", changelog["exclude"]["labels"])
        self.assertIn("release/internal", changelog["exclude"]["labels"])

        drafter_categories = [
            category
            for category in self.config["categories"]
            if "title" in category
        ]
        native_categories = changelog["categories"]
        self.assertEqual(
            [category["title"] for category in native_categories],
            [category["title"] for category in drafter_categories],
        )
        for native, drafter in zip(native_categories[:-1], drafter_categories[:-1]):
            when = drafter["when"]
            expected_labels = when.get("labels", [when.get("label")])
            self.assertEqual(native["labels"], expected_labels)
        self.assertEqual(native_categories[-1]["labels"], ["*"])

    def test_additive_primary_autolabeler_surface_does_not_exist(self) -> None:
        self.assertNotIn("autolabeler", self.config)
        self.assertNotIn("auto_label:", self.workflow)
        self.assertNotIn("release-drafter/release-drafter/autolabeler@", self.workflow)

    def test_workflow_is_draft_only_and_has_minimal_permissions(self) -> None:
        approved_release_drafter_commit = "34d80673e067bdc0c24568d3af899c216adcfaa9"
        self.assertIn(
            f"release-drafter/release-drafter@{approved_release_drafter_commit}",
            self.workflow,
        )
        self.assertNotIn("release-drafter/release-drafter@v7", self.workflow)
        self.assertNotIn("actions/checkout", self.workflow)
        self.assertNotIn("release create", self.workflow)
        self.assertNotIn("gh release", self.workflow)
        self.assertNotIn("workflow_dispatch", self.workflow)
        self.assertIn("contents: read", self.workflow)
        self.assertIn("contents: write", self.workflow)
        self.assertNotIn("pull-requests: write", self.workflow)


if __name__ == "__main__":
    unittest.main()
