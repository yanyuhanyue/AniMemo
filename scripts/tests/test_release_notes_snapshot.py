from __future__ import annotations

import unittest

from scripts.release_notes_snapshot import (
    SnapshotCollectionError,
    collect_pull_metadata,
)


class ReleaseNotesSnapshotCollectionTests(unittest.TestCase):
    def test_associated_pull_requests_are_deduplicated_and_closed(self):
        commits = ["a" * 40, "b" * 40]
        responses = {
            commits[0]: [
                {
                    "number": 131,
                    "title": "v1.1 分发收敛",
                    "merge_commit_sha": "c" * 40,
                    "head": {"sha": "d" * 40},
                    "labels": [{"name": "release/feature"}],
                }
            ],
            commits[1]: [
                {
                    "number": 131,
                    "title": "v1.1 分发收敛",
                    "merge_commit_sha": "c" * 40,
                    "head": {"sha": "d" * 40},
                    "labels": [{"name": "release/feature"}],
                }
            ],
        }

        pulls = collect_pull_metadata(
            repository="yanyuhanyue/AniMemo",
            commits=commits,
            query=lambda _repository, commit: responses[commit],
        )

        self.assertEqual(
            pulls,
            [
                {
                    "number": 131,
                    "title": "v1.1 分发收敛",
                    "source_identity": "c" * 40,
                    "labels": ["release/feature"],
                }
            ],
        )

    def test_commit_without_pr_and_conflicting_duplicate_pr_fail_closed(self):
        with self.assertRaises(SnapshotCollectionError):
            collect_pull_metadata(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40],
                query=lambda _repository, _commit: [],
            )

        count = 0

        def conflict(_repository, _commit):
            nonlocal count
            count += 1
            return [
                {
                    "number": 1,
                    "title": "one" if count == 1 else "changed",
                    "merge_commit_sha": "c" * 40,
                    "head": {"sha": "d" * 40},
                    "labels": [{"name": "release/fix"}],
                }
            ]

        with self.assertRaises(SnapshotCollectionError):
            collect_pull_metadata(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40, "b" * 40],
                query=conflict,
            )

    def test_repository_and_pr_shape_are_strict(self):
        with self.assertRaises(SnapshotCollectionError):
            collect_pull_metadata(
                repository="attacker/Other",
                commits=["a" * 40],
                query=lambda _repository, _commit: [],
            )
        with self.assertRaises(SnapshotCollectionError):
            collect_pull_metadata(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40],
                query=lambda _repository, _commit: [
                    {"number": True, "title": "bad", "labels": []}
                ],
            )


if __name__ == "__main__":
    unittest.main()
