from __future__ import annotations

import copy
import unittest

from release.notes import (
    ReleaseNotesError,
    build_release_notes,
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
        self.assertIn("## 📝 文档 (Documentation)", markdown)

    def test_rc_and_stable_rendering_bind_upgrade_install_security_and_assets(self):
        artifact = build_release_notes(
            context=context(),
            pulls=[pull(1, "release/feature", "发行自动化")],
        )
        markdown = render_release_notes(artifact)
        self.assertTrue(markdown.startswith("# AniMemo v1.1.0-rc.TEST\n"))
        for heading in (
            "## ✨ 新增功能 (Features)",
            "## 🐛 Bug 修复 (Bug Fixes)",
            "## 💡 功能与体验优化 (Improvements)",
            "## 🚀 性能与工程改进 (Performance & Engineering)",
            "## 🔄 升级 (Upgrade)",
            "## 📦 安装 (Installation)",
            "## 🛡️ 安全 (Security)",
            "## ⚠️ Breaking Changes",
            "## 📋 部署环境",
            "## 📦 Release Assets",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("从 v1.0.0 升级", markdown)
        self.assertIn("install.animemo.cc", markdown)
        self.assertIn("不声明新的 Heavy Security 认证", markdown)
        self.assertIn("`release-manifest.json`", markdown)

        stable = build_release_notes(
            context=context(release_tag="v1.1.0", channel="stable", previous_stable=""),
            pulls=[pull(1, "release/feature")],
        )
        stable_markdown = render_release_notes(stable)
        self.assertIn("首个 Stable 发行基线", stable_markdown)
        self.assertNotEqual(stable["identity"], artifact["identity"])

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
