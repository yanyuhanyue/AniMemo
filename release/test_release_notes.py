from __future__ import annotations

import copy
import unittest

from release.notes import (
    ReleaseNotesError,
    build_release_notes,
    configuration,
    promote_release_notes,
    render_release_notes,
    validate_release_notes,
)

COMMIT = "b" * 40
BASE = "a" * 40


def context(**changes):
    value = {
        "candidate_sha": COMMIT,
        "comparison_base_sha": BASE,
        "previous_stable": "v1.0.0",
        "release_tag": "v1.1.0-rc.TEST",
        "target_version": "v1.1.0",
        "channel": "rc",
        "minimum_updater_version": "1.0.0",
        "supported_os": ["Ubuntu 24.04 LTS"],
        "docker_requirement": "Docker Engine 27+ with Compose v2",
        "release_assets": [
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
            "checksums.txt",
        ],
    }
    value.update(changes)
    return value


def pull(number, label, title=None, **changes):
    value = {
        "number": number,
        "title": title or f"变更 {number}",
        "source_identity": f"sha256:{number:064x}",
        "labels": [label],
    }
    value.update(changes)
    return value


class ReleaseNotesTests(unittest.TestCase):
    def test_all_frozen_categories_and_exclusion_controls(self):
        labels = (
            "release/feature",
            "release/fix",
            "release/improvement",
            "release/performance",
            "release/security",
            "release/breaking",
            "release/docs",
            "release/ui",
            "release/refactor",
            "release/deployment",
            "release/ci",
            "release/dependencies",
        )
        pulls = [pull(index, label) for index, label in enumerate(labels, 1)]
        pulls.extend(
            (
                pull(20, "release/internal"),
                pull(21, "skip-changelog"),
            )
        )

        artifact = build_release_notes(context=context(), pulls=pulls)

        self.assertEqual(len(artifact["pulls"]), 14)
        self.assertEqual(artifact["pulls"][-2]["decision"], "EXCLUDED_INTERNAL")
        self.assertEqual(artifact["pulls"][-1]["decision"], "EXCLUDED_SKIP")
        self.assertEqual(artifact["configuration"]["label_namespace"], "release/")
        self.assertEqual(artifact["schema"], "animemo.release-notes/v1")
        validate_release_notes(artifact)

    def test_unclassified_conflicting_and_duplicate_pulls_fail_closed(self):
        cases = (
            [pull(1, "unrelated")],
            [pull(1, "release/feature", labels=["release/feature", "release/fix"])],
            [pull(1, "release/feature"), pull(1, "release/feature")],
            [pull(1, "release/internal", labels=["release/internal", "release/feature"])],
        )
        for pulls in cases:
            with self.subTest(pulls=pulls), self.assertRaises(ReleaseNotesError):
                build_release_notes(context=context(), pulls=pulls)

    def test_conflict_diagnostic_uses_stable_policy_code_and_exact_pr_context(self):
        merge_commit = "c" * 40
        conflicted = pull(
            198,
            "release/fix",
            source_identity=merge_commit,
            labels=["release/fix", "release/deployment", "release/ci"],
            observed_updated_at="2026-08-31T07:11:22Z",
        )

        with self.assertRaises(ReleaseNotesError) as raised:
            build_release_notes(context=context(), pulls=[conflicted])

        detail = str(raised.exception)
        self.assertIn("release_primary_category_conflict", detail)
        self.assertIn("PR #198", detail)
        self.assertIn(
            "primaryLabels=[release/ci,release/deployment,release/fix]",
            detail,
        )
        self.assertIn("exclusionLabels=[]", detail)
        self.assertIn(f"mergeCommit={merge_commit}", detail)
        self.assertIn("observedUpdatedAt=2026-08-31T07:11:22Z", detail)

    def test_pr_input_order_does_not_change_identity_or_markdown(self):
        pulls = [
            pull(9, "release/fix", "修复 [Markdown] *边界*"),
            pull(2, "release/feature", "新增中文功能"),
            pull(7, "release/docs", "Document `upgrade`"),
        ]
        first = build_release_notes(context=context(), pulls=pulls)
        second = build_release_notes(context=context(), pulls=list(reversed(pulls)))

        self.assertEqual(first, second)
        self.assertEqual(render_release_notes(first), render_release_notes(second))
        self.assertEqual([entry["number"] for entry in first["pulls"]], [2, 7, 9])
        markdown = render_release_notes(first)
        self.assertIn("新增中文功能 (#2)", markdown)
        self.assertIn(r"修复 \[Markdown\] \*边界\* (#9)", markdown)
        self.assertIn("## 📝 文档", markdown)

    def test_feature_only_rendering_omits_empty_categories_and_authority_context(self):
        artifact = build_release_notes(
            context=context(),
            pulls=[pull(1, "release/feature", "发行自动化")],
        )
        markdown = render_release_notes(artifact)
        self.assertTrue(markdown.startswith("# v1.1.0-rc.TEST\n"))
        self.assertNotIn("AniMemo", markdown.splitlines()[0])
        self.assertIn("## ✨ 新增功能", markdown)
        for heading in (
            "## 🐛 Bug 修复",
            "## 💡 功能与体验优化",
            "## 🚀 性能与工程改进",
            "## 🔒 安全与稳定性",
            "## 📝 文档",
            "## ⚠️ Breaking Changes",
            "## 📋 部署环境",
            "## 📦 Release Assets",
        ):
            self.assertNotIn(heading, markdown)
        for placeholder in ("- 无", "\n无\n", "暂无", "No changes", "None"):
            self.assertNotIn(placeholder, markdown)
        for asset in artifact["context"]["release_assets"]:
            self.assertNotIn(f"`{asset}`", markdown)
        self.assertEqual(artifact["context"]["supported_os"], ["Ubuntu 24.04 LTS"])
        self.assertEqual(
            artifact["context"]["docker_requirement"],
            "Docker Engine 27+ with Compose v2",
        )
        self.assertEqual(
            artifact["context"]["release_assets"],
            [
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
                "checksums.txt",
            ],
        )
        for heading in (
            "## 🔄 升级 (Upgrade)",
            "## 📦 安装 (Installation)",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("从 v1.0.0 升级", markdown)
        self.assertIn("install.animemo.cc", markdown)

    def test_security_and_breaking_sections_render_only_for_actual_changes(self):
        feature_security = build_release_notes(
            context=context(),
            pulls=[
                pull(1, "release/feature"),
                pull(2, "release/security", "修复令牌边界"),
            ],
        )
        feature_security_markdown = render_release_notes(feature_security)
        self.assertIn("## ✨ 新增功能", feature_security_markdown)
        self.assertIn("## 🔒 安全与稳定性", feature_security_markdown)
        self.assertIn("修复令牌边界 (#2)", feature_security_markdown)
        self.assertNotIn("## ⚠️ Breaking Changes", feature_security_markdown)

        breaking_fix = build_release_notes(
            context=context(),
            pulls=[
                pull(1, "release/breaking", "更新部署根目录"),
                pull(2, "release/fix"),
            ],
        )
        breaking_fix_markdown = render_release_notes(breaking_fix)
        self.assertIn("## 🐛 Bug 修复", breaking_fix_markdown)
        self.assertIn("## ⚠️ Breaking Changes", breaking_fix_markdown)
        self.assertIn("更新部署根目录 (#1)", breaking_fix_markdown)
        self.assertNotIn("## 🔒 安全与稳定性", breaking_fix_markdown)
        self.assertNotIn("## 📝 文档", breaking_fix_markdown)

    def test_renderer_v2_changes_configuration_identity_without_changing_snapshot_schema(self):
        current = configuration()
        self.assertEqual(current["renderer"], "animemo.release-notes.renderer/v2")
        self.assertEqual(current["schema"], "animemo.release-notes.configuration/v1")
        artifact = build_release_notes(
            context=context(), pulls=[pull(1, "release/feature")]
        )
        self.assertEqual(artifact["schema"], "animemo.release-notes/v1")
        self.assertEqual(artifact["configuration"], current)

    def test_stable_rendering_uses_version_only_heading_and_keeps_static_guidance(self):
        stable = build_release_notes(
            context=context(release_tag="v1.1.0", channel="stable", previous_stable=""),
            pulls=[pull(1, "release/feature")],
        )
        stable_markdown = render_release_notes(stable)
        self.assertEqual(stable_markdown.splitlines()[0], "# v1.1.0")
        self.assertIn("首个 Stable 发行基线", stable_markdown)
        self.assertIn("## 📦 安装 (Installation)", stable_markdown)

    def test_snapshot_tamper_and_unknown_fields_are_rejected(self):
        artifact = build_release_notes(
            context=context(), pulls=[pull(1, "release/feature")]
        )
        for mutation in (
            lambda value: value.update(identity="sha256:" + "0" * 64),
            lambda value: value["pulls"][0].update(title="被篡改"),
            lambda value: value.update(unexpected=True),
        ):
            candidate = copy.deepcopy(artifact)
            mutation(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ReleaseNotesError):
                validate_release_notes(candidate)

    def test_noncanonical_authority_context_is_rejected_before_rendering(self):
        invalid_contexts = (
            context(supported_os=["Unknown OS"]),
            context(release_assets=["release-manifest.json"]),
        )
        for invalid in invalid_contexts:
            with self.subTest(context=invalid), self.assertRaises(ReleaseNotesError):
                build_release_notes(
                    context=invalid, pulls=[pull(1, "release/feature")]
                )

    def test_stable_notes_are_derived_from_the_same_frozen_pr_population(self):
        rc = build_release_notes(
            context=context(),
            pulls=[pull(2, "release/feature"), pull(1, "release/fix")],
        )
        stable = promote_release_notes(rc, stable_tag="v1.1.0")
        self.assertEqual(
            [
                {key: item[key] for key in ("number", "title", "source_identity", "labels")}
                for item in stable["pulls"]
            ],
            [
                {key: item[key] for key in ("number", "title", "source_identity", "labels")}
                for item in rc["pulls"]
            ],
        )
        self.assertEqual(stable["context"]["candidate_sha"], rc["context"]["candidate_sha"])
        self.assertEqual(stable["context"]["channel"], "stable")
        self.assertIn("Stable 版本", render_release_notes(stable))


if __name__ == "__main__":
    unittest.main()
