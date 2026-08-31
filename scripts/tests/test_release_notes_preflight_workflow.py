from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def job(source: str, name: str) -> str:
    marker = f"  {name}:\n"
    start = source.index(marker)
    next_job = source.find("\n  ", start + len(marker))
    while next_job != -1:
        candidate = source[next_job + 3 :].split(":", 1)[0]
        if candidate.replace("-", "").isalnum():
            break
        next_job = source.find("\n  ", next_job + 1)
    return source[start:] if next_job == -1 else source[start:next_job]


class ReleaseNotesPreflightWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")

    def test_release_preflight_is_the_only_live_metadata_authority(self):
        preflight = job(self.source, "preflight")
        phase_a = job(self.source, "dry-run")

        self.assertEqual(self.source.count("python scripts/release_notes_snapshot.py"), 1)
        self.assertIn("python scripts/release_notes_snapshot.py", preflight)
        self.assertIn("python scripts/release_notes_preflight.py create", preflight)
        self.assertIn("release-notes-preflight-${{ github.run_id }}-${{ github.run_attempt }}", preflight)
        self.assertNotIn("release_notes_snapshot.py", phase_a)
        self.assertNotIn("GH_TOKEN:", phase_a)
        self.assertNotIn("pull-requests: read", phase_a)

    def test_phase_a_downloads_and_verifies_the_exact_frozen_artifact(self):
        phase_a = job(self.source, "dry-run")

        self.assertIn("actions/download-artifact@", phase_a)
        self.assertIn("python scripts/release_notes_preflight.py verify", phase_a)
        self.assertIn("release-notes-preflight-${{ github.run_id }}-${{ github.run_attempt }}", phase_a)
        self.assertIn("release-notes-preflight.json", phase_a)
        for name in (
            "release-notes-input.json",
            "release-notes-readback.json",
            "release-notes-preflight.json",
        ):
            self.assertIn(
                f"release-output/{name}",
                phase_a,
            )
            self.assertIn(
                f"release-qualification/{name}",
                phase_a,
            )
        self.assertIn('wc -l)" = "14"', phase_a)

    def test_publish_authority_verifies_an_isolated_closed_preflight_root(self):
        release_authority = job(self.source, "release-authority")

        self.assertIn(
            'notes_preflight_root="$RUNNER_TEMP/qualification-release-notes-preflight"',
            release_authority,
        )
        self.assertIn('--root "$notes_preflight_root"', release_authority)
        self.assertNotIn(
            '--root "$RUNNER_TEMP/qualification"', release_authority
        )
        for name in (
            "release-notes-input.json",
            "release-notes.json",
            "release-notes.md",
            "release-notes-readback.json",
            "release-notes-preflight.json",
        ):
            self.assertIn(name, release_authority)
        self.assertIn(
            '"$RUNNER_TEMP/qualification/$name"', release_authority
        )


if __name__ == "__main__":
    unittest.main()
