from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "b" * 40
API_DIGEST = "sha256:" + "3" * 64
WEB_DIGEST = "sha256:" + "4" * 64


class ReleaseCliTests(unittest.TestCase):
    def run_cli(self, *arguments, expected=0):
        completed = subprocess.run(
            [sys.executable, "-m", "release.cli", *map(str, arguments)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr or completed.stdout)
        return completed

    def test_resolve_version_emits_json_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tags = root / "tags.txt"
            outputs = root / "github-output.txt"
            tags.write_text("v1.0.0\nv1.0.1-beta.1\n", encoding="utf-8")
            completed = self.run_cli(
                "resolve-version",
                "--tags-file", tags,
                "--bump", "patch",
                "--channel", "beta",
                "--github-output", outputs,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["releaseTag"], "v1.0.1-beta.2")
            self.assertIn("release_tag=v1.0.1-beta.2", outputs.read_text(encoding="utf-8"))

    def test_generate_validate_and_checksum_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "release-manifest.json"
            deployment_contract = root / "deployment-contract.json"
            installer_materials = root / "installer-materials.tar"
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            checksums = root / "checksums.txt"
            self.run_cli(
                "build-installer-materials",
                "--root", ROOT,
                "--wheelhouse", wheelhouse,
                "--output", installer_materials,
            )
            self.run_cli(
                "generate-deployment-contract",
                "--root", ROOT,
                "--installer-materials", installer_materials,
                "--output", deployment_contract,
            )
            self.run_cli(
                "generate-manifest",
                "--version", "v1.0.0-rc.1",
                "--channel", "rc",
                "--commit", COMMIT,
                "--created-at", "2026-08-12T10:00:00Z",
                "--api-digest", API_DIGEST,
                "--web-digest", WEB_DIGEST,
                "--compatibility-file", ROOT / "release" / "compatibility.json",
                "--deployment-contract-file", deployment_contract,
                "--deployment-root", ROOT,
                "--installer-materials", installer_materials,
                "--output", target,
            )
            self.run_cli("validate-manifest", "--manifest", target, "--updater-version", "1.0.0")
            self.run_cli(
                "write-checksums", "--output", checksums,
                target, deployment_contract, installer_materials
            )
            expected_manifest = hashlib.sha256(target.read_bytes()).hexdigest()
            expected_deployment = hashlib.sha256(deployment_contract.read_bytes()).hexdigest()
            expected_materials = hashlib.sha256(installer_materials.read_bytes()).hexdigest()
            self.assertEqual(
                checksums.read_text(encoding="utf-8"),
                f"{expected_manifest}  release-manifest.json\n"
                f"{expected_deployment}  deployment-contract.json\n"
                f"{expected_materials}  installer-materials.tar\n",
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["deployment"]["contractSha256"],
                "sha256:" + expected_deployment,
            )
            self.assertEqual(payload["schemaVersion"], 2)
            self.assertEqual(
                payload["deployment"]["installerMaterials"]["sha256"],
                "sha256:" + expected_materials,
            )

    def test_generate_manifest_rejects_deployment_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            (source_root / "deploy").mkdir(parents=True)
            (source_root / "updater").mkdir()
            compose = source_root / "deploy" / "docker-compose.yml"
            overlay = source_root / "updater" / "docker-compose.runtime.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            overlay.write_text("services: {}\n", encoding="utf-8")
            contract = root / "deployment-contract.json"
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "qualified_dependency-1.0-py3-none-any.whl").write_bytes(
                b"qualified wheel bytes"
            )
            installer_materials = root / "installer-materials.tar"
            self.run_cli(
                "build-installer-materials", "--root", ROOT,
                "--wheelhouse", wheelhouse, "--output", installer_materials,
            )
            self.run_cli(
                "generate-deployment-contract", "--root", source_root,
                "--installer-materials", installer_materials,
                "--output", contract,
            )
            compose.write_text("services:\n  changed: {}\n", encoding="utf-8")

            completed = self.run_cli(
                "generate-manifest",
                "--version", "v1.0.0-rc.1",
                "--channel", "rc",
                "--commit", COMMIT,
                "--created-at", "2026-08-12T10:00:00Z",
                "--api-digest", API_DIGEST,
                "--web-digest", WEB_DIGEST,
                "--compatibility-file", ROOT / "release" / "compatibility.json",
                "--deployment-contract-file", contract,
                "--deployment-root", source_root,
                "--installer-materials", installer_materials,
                "--output", root / "release-manifest.json",
                expected=2,
            )
            self.assertIn("checksum differs", completed.stderr)

    def test_generate_provenance_plan_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provenance-plan.json"
            completed = self.run_cli(
                "generate-provenance-plan",
                "--version", "v1.0.0-rc.1",
                "--commit", COMMIT,
                "--created-at", "2026-08-12T10:00:00Z",
                "--api-digest", API_DIGEST,
                "--web-digest", WEB_DIGEST,
                "--output", target,
            )
            self.assertEqual(json.loads(completed.stdout)["predicateType"], "https://slsa.dev/provenance/v1")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["subject"][0]["digest"]["sha256"], "3" * 64)

    def test_previous_stable_outputs_empty_for_bootstrap_and_latest_prior_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tags = root / "tags.txt"
            outputs = root / "outputs.txt"
            tags.write_text("v1.0.0\nv1.1.0-beta.1\nv1.1.0\nv1.2.0-rc.1\n", encoding="utf-8")
            completed = self.run_cli(
                "previous-stable",
                "--tags-file", tags,
                "--target", "v1.2.0",
                "--github-output", outputs,
            )
            self.assertEqual(json.loads(completed.stdout), {"previousStable": "v1.1.0"})
            self.assertIn("previous_stable=v1.1.0", outputs.read_text(encoding="utf-8"))

    def test_cli_errors_are_machine_readable_and_nonzero(self):
        completed = self.run_cli(
            "resolve-version",
            "--tags-file", ROOT / "release" / "missing-tags.txt",
            "--bump", "patch",
            "--channel", "stable",
            expected=2,
        )
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["code"], "release_contract_invalid")
        self.assertIn("detail", payload)

    def test_generate_release_notes_and_publication_plan_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_input = root / "notes-input.json"
            notes_json = root / "release-notes.json"
            notes_markdown = root / "release-notes.md"
            notes_input.write_text(
                json.dumps(
                    {
                        "context": {
                            "candidate_sha": COMMIT,
                            "comparison_base_sha": "a" * 40,
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
                        },
                        "pulls": [
                            {
                                "number": 131,
                                "title": "v1.1 分发收敛",
                                "source_identity": COMMIT,
                                "labels": ["release/feature"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            first = self.run_cli(
                "generate-release-notes",
                "--input", notes_input,
                "--output-json", notes_json,
                "--output-markdown", notes_markdown,
            )
            first_json = notes_json.read_bytes()
            first_markdown = notes_markdown.read_bytes()
            second = self.run_cli(
                "generate-release-notes",
                "--input", notes_input,
                "--output-json", notes_json,
                "--output-markdown", notes_markdown,
            )
            self.assertEqual(first_json, notes_json.read_bytes())
            self.assertEqual(first_markdown, notes_markdown.read_bytes())
            self.assertEqual(json.loads(first.stdout)["identity"], json.loads(second.stdout)["identity"])

            assets = {}
            for name, content in {
                "release-manifest.json": b"manifest",
                "deployment-contract.json": b"deployment",
                "installer-materials.tar": b"materials",
                "checksums.txt": b"checksums",
            }.items():
                assets[name] = {
                    "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            plan_input = root / "plan-input.json"
            plan_output = root / "publication-plan.json"
            plan_input.write_text(
                json.dumps(
                    {
                        "repository": "yanyuhanyue/AniMemo",
                        "channel": "rc",
                        "tag": "v1.1.0-rc.TEST",
                        "commit": COMMIT,
                        "qualification_identity": "sha256:" + "6" * 64,
                        "release_notes_identity": json.loads(notes_json.read_text(encoding="utf-8"))["identity"],
                        "release_notes_markdown_sha256": "sha256:" + hashlib.sha256(first_markdown).hexdigest(),
                        "assets": assets,
                        "api_digest": API_DIGEST,
                        "web_digest": WEB_DIGEST,
                    }
                ),
                encoding="utf-8",
            )
            completed = self.run_cli(
                "plan-publication", "--input", plan_input, "--output", plan_output
            )
            plan = json.loads(completed.stdout)
            self.assertEqual(plan, json.loads(plan_output.read_text(encoding="utf-8")))
            self.assertEqual(plan["external_mutation_mode"], "PLAN_ONLY")
            self.assertNotIn("--generate-notes", plan["commands"]["create_draft"])


if __name__ == "__main__":
    unittest.main()
