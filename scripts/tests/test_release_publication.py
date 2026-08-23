from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release.publication import build_publication_plan, verify_asset_readback

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
NOTES = "# v1.1.0-rc.TEST\n"
CONTENTS = {
    "release-manifest.json": b"manifest\n",
    "deployment-contract.json": b"deployment\n",
    "installer-materials.tar": b"materials",
    "checksums.txt": b"checksums\n",
}
PORTABLE_NAME = "animemo-v1.1.0-rc.TEST-portable.tar"
PORTABLE_BYTES = b"deterministic portable payload"


class ReleasePublicationCliTests(unittest.TestCase):
    def _fixture(self, root: Path):
        assets = root / "assets"
        assets.mkdir()
        identities = {}
        for name, content in CONTENTS.items():
            (assets / name).write_bytes(content)
            identities[name] = {
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        (assets / PORTABLE_NAME).write_bytes(PORTABLE_BYTES)
        notes = root / "release-notes.md"
        notes.write_text(NOTES, encoding="utf-8", newline="\n")
        plan = build_publication_plan(
            repository="yanyuhanyue/AniMemo",
            channel="rc",
            tag="v1.1.0-rc.TEST",
            commit=COMMIT,
            qualification_identity="sha256:" + "1" * 64,
            release_notes_identity="sha256:" + "2" * 64,
            release_notes_markdown_sha256="sha256:" + hashlib.sha256(notes.read_bytes()).hexdigest(),
            assets=identities,
            transport_assets={
                PORTABLE_NAME: {
                    "role": "PORTABLE_RELEASE_BUNDLE",
                    "sha256": "sha256:"
                    + hashlib.sha256(PORTABLE_BYTES).hexdigest(),
                    "size": len(PORTABLE_BYTES),
                }
            },
            api_digest="sha256:" + "3" * 64,
            web_digest="sha256:" + "4" * 64,
        )
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        metadata = {
            "id": 1,
            "tag": "v1.1.0-rc.TEST",
            "target": COMMIT,
            "draft": True,
            "prerelease": True,
            "body": NOTES,
            "assets": [
                {"name": name, "size": value["size"], "digest": value["sha256"]}
                for name, value in identities.items()
            ]
            + [
                {
                    "name": PORTABLE_NAME,
                    "size": len(PORTABLE_BYTES),
                    "digest": "sha256:"
                    + hashlib.sha256(PORTABLE_BYTES).hexdigest(),
                }
            ],
        }
        metadata_path = root / "metadata.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return plan_path, metadata_path, assets, notes

    def test_publication_plan_preserves_four_authority_assets_and_declares_one_portable_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path, _, _, _ = self._fixture(Path(directory))
            plan = json.loads(plan_path.read_text(encoding="utf-8"))

            self.assertEqual(set(plan["assets"]), set(CONTENTS))
            self.assertEqual(
                plan["transport_assets"],
                {
                    PORTABLE_NAME: {
                        "role": "PORTABLE_RELEASE_BUNDLE",
                        "sha256": "sha256:"
                        + hashlib.sha256(PORTABLE_BYTES).hexdigest(),
                        "size": len(PORTABLE_BYTES),
                    }
                },
            )

    def test_readback_streams_files_without_loading_the_portable_asset_into_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path, _, assets, _ = self._fixture(Path(directory))
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            expected = {
                name: {
                    "sha256": item["sha256"],
                    "size": item["size"],
                }
                for name, item in {**plan["assets"], **plan["transport_assets"]}.items()
            }

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("readback must be streamed"),
            ):
                verify_asset_readback(
                    plan,
                    remote_assets=expected,
                    downloaded_assets={name: assets / name for name in expected},
                )
            self.assertEqual(
                plan["commands"]["upload_assets"][-3:],
                [
                    f"release-output/{PORTABLE_NAME}",
                    "--repo",
                    "yanyuhanyue/AniMemo",
                ],
            )

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-B", "scripts/release_publication.py", *map(str, args)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_draft_cli_verifies_closed_metadata_body_and_exact_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, metadata, assets, notes = self._fixture(Path(directory))
            completed = self._run(
                "verify-draft",
                "--plan", plan,
                "--metadata", metadata,
                "--download-directory", assets,
                "--notes-file", notes,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["state"], "DRAFT_VERIFIED")

    def test_draft_cli_rejects_changed_body_and_asset_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, metadata, assets, notes = self._fixture(root)
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["body"] = "changed"
            metadata.write_text(json.dumps(payload), encoding="utf-8")
            completed = self._run(
                "verify-draft",
                "--plan", plan,
                "--metadata", metadata,
                "--download-directory", assets,
                "--notes-file", notes,
            )
            self.assertEqual(completed.returncode, 2)
            (assets / "checksums.txt").write_bytes(b"tampered")
            payload["body"] = NOTES
            metadata.write_text(json.dumps(payload), encoding="utf-8")
            completed = self._run(
                "verify-draft",
                "--plan", plan,
                "--metadata", metadata,
                "--download-directory", assets,
                "--notes-file", notes,
            )
            self.assertEqual(completed.returncode, 2)

    def test_draft_cli_rejects_undeclared_transport_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, metadata, assets, notes = self._fixture(root)
            (assets / "unexpected.bin").write_bytes(b"not declared")
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["assets"].append(
                {
                    "name": "unexpected.bin",
                    "size": 12,
                    "digest": "sha256:" + hashlib.sha256(b"not declared").hexdigest(),
                }
            )
            metadata.write_text(json.dumps(payload), encoding="utf-8")

            completed = self._run(
                "verify-draft",
                "--plan", plan,
                "--metadata", metadata,
                "--download-directory", assets,
                "--notes-file", notes,
            )

            self.assertEqual(completed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
