from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.release_notes_snapshot import (
    SnapshotCollectionError,
    _query_population_with_graphql,
    collect_pull_metadata,
    collect_pull_metadata_double_readback,
)


class ReleaseNotesSnapshotCollectionTests(unittest.TestCase):
    def test_double_readback_requires_identical_population_digest(self):
        first = [
            {
                "number": 203,
                "title": "修复控制器",
                "source_identity": "c" * 40,
                "labels": ["release/deployment"],
                "observed_updated_at": "2026-08-31T10:00:00Z",
            }
        ]
        second = [
            {
                **first[0],
                "labels": ["release/deployment", "release/fix"],
                "observed_updated_at": "2026-08-31T10:01:00Z",
            }
        ]
        reads = iter((first, second))

        with self.assertRaisesRegex(
            SnapshotCollectionError,
            "release_notes_population_changed_between_readbacks",
        ):
            collect_pull_metadata_double_readback(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40],
                collect=lambda _repository, _commits: next(reads),
            )

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
                    "updated_at": "2026-08-31T10:00:00Z",
                }
            ],
            commits[1]: [
                {
                    "number": 131,
                    "title": "v1.1 分发收敛",
                    "merge_commit_sha": "c" * 40,
                    "head": {"sha": "d" * 40},
                    "labels": [{"name": "release/feature"}],
                    "updated_at": "2026-08-31T10:00:00Z",
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
                    "observed_updated_at": "2026-08-31T10:00:00Z",
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
                    "updated_at": "2026-08-31T10:00:00Z",
                }
            ]

        with self.assertRaises(SnapshotCollectionError):
            collect_pull_metadata(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40, "b" * 40],
                query=conflict,
            )

    def test_commit_with_multiple_associated_prs_is_rejected(self):
        def raw(number: int):
            return {
                "number": number,
                "title": f"change {number}",
                "merge_commit_sha": str(number) * 40,
                "head": {"sha": "d" * 40},
                "labels": [{"name": "release/fix"}],
                "updated_at": "2026-08-31T10:00:00Z",
            }

        with self.assertRaisesRegex(
            SnapshotCollectionError,
            "exactly one associated merged PR",
        ):
            collect_pull_metadata(
                repository="yanyuhanyue/AniMemo",
                commits=["a" * 40],
                query=lambda _repository, _commit: [raw(1), raw(2)],
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

    def test_graphql_partial_errors_and_malformed_associations_fail_closed(self):
        commit = "a" * 40
        cases = (
            {
                "data": {"repository": {}},
                "errors": [{"message": "partial failure"}],
            },
            {
                "data": {
                    "repository": {
                        "c0": {
                            "oid": commit,
                            "associatedPullRequests": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [None],
                            },
                        }
                    }
                }
            },
        )
        for response in cases:
            completed = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(response),
            )
            with self.subTest(response=response), patch(
                "scripts.release_notes_snapshot.subprocess.run",
                return_value=completed,
            ), self.assertRaises(SnapshotCollectionError):
                _query_population_with_graphql(
                    "yanyuhanyue/AniMemo",
                    [commit],
                )


if __name__ == "__main__":
    unittest.main()
