from __future__ import annotations

import unittest

from release.primary_category import (
    PrimaryCategoryError,
    validate_primary_category,
)


class PrimaryCategoryPolicyTests(unittest.TestCase):
    def test_breaking_and_security_are_explicit_primary_authorities(self):
        for label, category in (
            ("release/breaking", "breaking"),
            ("release/security", "security"),
        ):
            with self.subTest(label=label):
                decision = validate_primary_category(
                    number=207,
                    labels=[label],
                    merge_commit="1" * 40,
                    observed_updated_at="2026-08-31T09:05:00Z",
                )
                self.assertEqual(decision.category, category)
                self.assertEqual(decision.primary_labels, (label,))

    def test_included_pull_has_exactly_one_primary_category(self):
        decision = validate_primary_category(
            number=203,
            labels=["release/deployment", "size/XL"],
            merge_commit="b" * 40,
            observed_updated_at="2026-08-31T09:00:00Z",
        )

        self.assertEqual(decision.category, "deployment")
        self.assertEqual(decision.decision, "INCLUDED")
        self.assertEqual(decision.primary_labels, ("release/deployment",))
        self.assertEqual(decision.exclusion_labels, ())

    def test_excluded_pull_has_zero_primary_categories(self):
        decision = validate_primary_category(
            number=204,
            labels=["skip-changelog", "size/S"],
            merge_commit="c" * 40,
            observed_updated_at="2026-08-31T09:01:00Z",
        )

        self.assertEqual(decision.category, "skip")
        self.assertEqual(decision.decision, "EXCLUDED_SKIP")
        self.assertEqual(decision.primary_labels, ())
        self.assertEqual(decision.exclusion_labels, ("skip-changelog",))

    def test_included_pull_without_primary_category_is_rejected(self):
        with self.assertRaises(PrimaryCategoryError) as raised:
            validate_primary_category(
                number=201,
                labels=["size/M"],
                merge_commit="d" * 40,
                observed_updated_at="2026-08-31T09:02:00Z",
            )

        self.assertEqual(
            raised.exception.code,
            "release_primary_category_unclassified",
        )
        self.assertEqual(raised.exception.primary_labels, ())
        self.assertEqual(raised.exception.exclusion_labels, ())

    def test_exclusion_and_primary_category_conflict_is_rejected(self):
        with self.assertRaises(PrimaryCategoryError) as raised:
            validate_primary_category(
                number=205,
                labels=["release/internal", "release/security"],
                merge_commit="e" * 40,
                observed_updated_at="2026-08-31T09:03:00Z",
            )

        self.assertEqual(
            raised.exception.code,
            "release_primary_category_exclusion_conflict",
        )
        self.assertEqual(raised.exception.primary_labels, ("release/security",))
        self.assertEqual(
            raised.exception.exclusion_labels,
            ("release/internal",),
        )

    def test_two_exclusion_authorities_conflict(self):
        with self.assertRaises(PrimaryCategoryError) as raised:
            validate_primary_category(
                number=206,
                labels=["release/internal", "skip-changelog"],
                merge_commit="f" * 40,
                observed_updated_at="2026-08-31T09:04:00Z",
            )

        self.assertEqual(
            raised.exception.code,
            "release_primary_category_exclusion_conflict",
        )
        self.assertEqual(raised.exception.primary_labels, ())
        self.assertEqual(
            raised.exception.exclusion_labels,
            ("release/internal", "skip-changelog"),
        )

    def test_conflicting_primary_labels_report_exact_pull_request_context(self):
        merge_commit = "a" * 40

        with self.assertRaises(PrimaryCategoryError) as raised:
            validate_primary_category(
                number=198,
                labels=["release/fix", "release/deployment", "release/ci"],
                merge_commit=merge_commit,
                observed_updated_at="2026-08-31T07:11:22Z",
            )

        error = raised.exception
        self.assertEqual(error.code, "release_primary_category_conflict")
        self.assertEqual(
            error.primary_labels,
            ("release/ci", "release/deployment", "release/fix"),
        )
        self.assertEqual(error.exclusion_labels, ())
        self.assertIn("PR #198", str(error))
        self.assertIn(
            "primaryLabels=[release/ci,release/deployment,release/fix]",
            str(error),
        )
        self.assertIn("exclusionLabels=[]", str(error))
        self.assertIn(f"mergeCommit={merge_commit}", str(error))


if __name__ == "__main__":
    unittest.main()
