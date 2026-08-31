from __future__ import annotations

import hashlib
import unittest

from release.notes import ReleaseNotesError, build_release_notes, render_release_notes
from release.release_notes_preflight import (
    ReleaseNotesPreflightError,
    build_preflight_manifest,
    canonical_json_bytes,
    verify_preflight_manifest,
)

HEAD_SHA = "a" * 40
HEAD_TREE = "b" * 40
BOUNDARY_SHA = "c" * 40


def note_input():
    return {
        "context": {
            "candidate_sha": HEAD_SHA,
            "comparison_base_sha": BOUNDARY_SHA,
            "previous_stable": "v1.0.0",
            "release_tag": "v1.1.0-rc.19",
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
        },
        "pulls": [
            {
                "number": 203,
                "title": "修复候选验证与发行控制器 stdin 转发",
                "source_identity": "d" * 40,
                "labels": ["release/deployment", "size/XL"],
                "observed_updated_at": "2026-08-31T10:00:00Z",
            }
        ],
    }


def files():
    source = note_input()
    notes = build_release_notes(context=source["context"], pulls=source["pulls"])
    population_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(source["pulls"])
    ).hexdigest()
    events = [
        {
            "labels": pull["labels"],
            "number": pull["number"],
            "observed_updated_at": pull["observed_updated_at"],
        }
        for pull in source["pulls"]
    ]
    event_digest = "sha256:" + hashlib.sha256(canonical_json_bytes(events)).hexdigest()
    return {
        "release-notes-input.json": canonical_json_bytes(source),
        "release-notes.json": canonical_json_bytes(notes),
        "release-notes.md": render_release_notes(notes).encode("utf-8"),
        "release-notes-readback.json": canonical_json_bytes(
            {
                "schema": "animemo.release-notes-readback/v1",
                "readback_count": 2,
                "population_digest": population_digest,
                "event_digest": event_digest,
            }
        ),
    }


def binding():
    return {
        "repository": "yanyuhanyue/AniMemo",
        "run_id": 12345,
        "run_attempt": 1,
        "head_sha": HEAD_SHA,
        "head_tree": HEAD_TREE,
        "comparison_base_sha": BOUNDARY_SHA,
        "previous_stable": "v1.0.0",
        "release_tag": "v1.1.0-rc.19",
        "target_version": "v1.1.0",
        "channel": "rc",
    }


class ReleaseNotesPreflightTests(unittest.TestCase):
    def test_full_population_conflict_fails_in_release_preflight(self):
        payloads = files()
        conflicted = note_input()
        conflicted["pulls"][0]["number"] = 198
        conflicted["pulls"][0]["labels"] = [
            "release/fix",
            "release/deployment",
            "release/ci",
        ]
        payloads["release-notes-input.json"] = canonical_json_bytes(conflicted)
        conflicted_events = [
            {
                "labels": pull["labels"],
                "number": pull["number"],
                "observed_updated_at": pull["observed_updated_at"],
            }
            for pull in conflicted["pulls"]
        ]
        payloads["release-notes-readback.json"] = canonical_json_bytes(
            {
                "schema": "animemo.release-notes-readback/v1",
                "readback_count": 2,
                "population_digest": "sha256:"
                + hashlib.sha256(canonical_json_bytes(conflicted["pulls"])).hexdigest(),
                "event_digest": "sha256:"
                + hashlib.sha256(canonical_json_bytes(conflicted_events)).hexdigest(),
            }
        )

        with self.assertRaisesRegex(
            ReleaseNotesError,
            "release_primary_category_conflict.*PR #198",
        ):
            build_preflight_manifest(binding=binding(), files=payloads)

    def test_run_head_and_tree_tamper_are_rejected(self):
        payloads = files()
        manifest = build_preflight_manifest(binding=binding(), files=payloads)

        verify_preflight_manifest(
            manifest,
            files=payloads,
            expected_binding=binding(),
        )
        for field, replacement in (
            ("run_id", 99999),
            ("head_sha", "e" * 40),
            ("head_tree", "f" * 40),
        ):
            changed = binding()
            changed[field] = replacement
            with self.subTest(field=field), self.assertRaises(
                ReleaseNotesPreflightError
            ):
                verify_preflight_manifest(
                    manifest,
                    files=payloads,
                    expected_binding=changed,
                )

    def test_previous_stable_boundary_tamper_is_rejected(self):
        payloads = files()
        manifest = build_preflight_manifest(binding=binding(), files=payloads)
        changed = binding()
        changed["previous_stable"] = "v0.9.0"
        changed["comparison_base_sha"] = "9" * 40

        with self.assertRaisesRegex(
            ReleaseNotesPreflightError,
            "comparison_base_sha,previous_stable",
        ):
            verify_preflight_manifest(
                manifest,
                files=payloads,
                expected_binding=changed,
            )


if __name__ == "__main__":
    unittest.main()
