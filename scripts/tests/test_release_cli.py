from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release import cli
from release.acceptance import (
    build_rc_live_acceptance,
    verify_stable_promotion_acceptance,
)
from release.cli import _validate_stable_publication_authority_inputs
from release.contract import build_manifest, promote_manifest
from release.materials import MaterialContractError
from release.publication import PublicationError
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit

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

    def test_normalize_oci_layout_cli_exposes_only_exact_identity_inputs(self):
        completed = self.run_cli("normalize-oci-layout", "--help")
        for argument in (
            "--source-root",
            "--layout",
            "--role",
            "--repository",
            "--expected-digest",
            "--expected-platform",
        ):
            self.assertIn(argument, completed.stdout)
        for forbidden in (
            "--tag",
            "--source-index",
            "--allow-unknown",
            "--rewrite-digest",
        ):
            self.assertNotIn(forbidden, completed.stdout)

    def test_metadata_freshness_cli_does_not_accept_operator_identity_overrides(self):
        completed = self.run_cli("collect-metadata-freshness", "--help")
        for argument in (
            "--repository-root",
            "--qualification-directory",
            "--output-directory",
            "--workflow-run-id",
            "--workflow-attempt",
            "--workflow-sha",
            "--candidate-sha",
            "--candidate-tree",
            "--qualification-run-id",
            "--qualification-artifact-id",
        ):
            self.assertIn(argument, completed.stdout)
        for forbidden in (
            "--repository ",
            "--api-url",
            "--workflow-path",
            "--release-tag",
            "--release-notes-identity",
            "--markdown-sha",
            "--freshness-passed",
        ):
            self.assertNotIn(forbidden, completed.stdout)

        verification = self.run_cli("verify-metadata-freshness", "--help")
        self.assertIn("--expected-workflow-run-id", verification.stdout)
        self.assertIn("--expected-qualification-artifact-id", verification.stdout)
        self.assertNotIn("--allow-expired", verification.stdout)
        self.assertNotIn("--override", verification.stdout)

    def test_resolve_version_emits_json_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tags = root / "tags.txt"
            reservations = root / "publication-reservations.json"
            outputs = root / "github-output.txt"
            tags.write_text("v1.0.0\nv1.0.1-beta.1\n", encoding="utf-8")
            reservations.write_text(
                '{"schemaVersion":1,"reservations":[]}\n', encoding="utf-8"
            )
            completed = self.run_cli(
                "resolve-version",
                "--tags-file", tags,
                "--publication-reservations-file", reservations,
                "--bump", "patch",
                "--channel", "beta",
                "--github-output", outputs,
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["releaseTag"], "v1.0.1-beta.2")
            self.assertIn("release_tag=v1.0.1-beta.2", outputs.read_text(encoding="utf-8"))

    def test_stable_planner_rebinds_the_receipt_and_exact_rc_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            deployment = b"accepted deployment contract\n"
            materials = b"accepted installer materials"
            deployment_identity = "sha256:" + hashlib.sha256(deployment).hexdigest()
            materials_identity = "sha256:" + hashlib.sha256(materials).hexdigest()
            rc_manifest = build_manifest(
                version="v1.1.0-rc.1",
                channel="rc",
                commit=COMMIT,
                created_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
                api_digest=API_DIGEST,
                web_digest=WEB_DIGEST,
                deployment_contract_sha256=deployment_identity,
                deployment_files=[
                    {
                        "path": "deploy/docker-compose.yml",
                        "sha256": "sha256:" + "d" * 64,
                    },
                    {
                        "path": "updater/docker-compose.runtime.yml",
                        "sha256": "sha256:" + "e" * 64,
                    },
                ],
                installer_materials_sha256=materials_identity,
                minimum_updater_version="1.0.0",
                database_contract="animemo-db-v1",
                database_accepts=["animemo-db-v1"],
                migration_required=False,
                migration_policy="none",
                application_rollback="safe",
                configuration_contract="animemo-config-v1",
                configuration_accepts=["animemo-config-v1"],
                plugin_sdk_apis=[2],
            )
            rc_manifest_path = root / "rc-manifest.json"
            rc_manifest_path.write_text(
                json.dumps(rc_manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            stable_manifest = promote_manifest(
                rc_manifest,
                existing_tags=[],
                provenance_source_commit="c" * 40,
                created_at="2026-08-20T00:00:00Z",
            )
            (assets / "release-manifest.json").write_text(
                json.dumps(stable_manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            (assets / "deployment-contract.json").write_bytes(deployment)
            (assets / "installer-materials.tar").write_bytes(materials)
            acceptance = build_rc_live_acceptance(
                rc_tag="v1.1.0-rc.1",
                rc_commit=COMMIT,
                release_manifest_identity=(
                    "sha256:" + hashlib.sha256(rc_manifest_path.read_bytes()).hexdigest()
                ),
                deployment_contract_identity=deployment_identity,
                installer_materials_identity=materials_identity,
                api_digest=API_DIGEST,
                web_digest=WEB_DIGEST,
                fresh_base_identity="sha256:" + "6" * 64,
                docker_base_identity="sha256:" + "7" * 64,
                runtime_base_identity="sha256:" + "8" * 64,
                install_path="github",
                doctor_result="PASS",
                upgrade_result="PASS",
                accepted_at="2026-08-20T00:01:00Z",
                operator_identity="github:maintainer-review/v1",
                tool_identity="sha256:" + "9" * 64,
            )
            receipt = verify_stable_promotion_acceptance(
                acceptance,
                expected={
                    key: acceptance[key]
                    for key in (
                        "rc_tag",
                        "rc_commit",
                        "release_manifest_identity",
                        "deployment_contract_identity",
                        "installer_materials_identity",
                        "api_digest",
                        "web_digest",
                    )
                },
                stable_commit=COMMIT,
                stable_api_digest=API_DIGEST,
                stable_web_digest=WEB_DIGEST,
            )
            acceptance_path = root / "acceptance.json"
            receipt_path = root / "promotion-acceptance.json"
            for path, payload in ((acceptance_path, acceptance), (receipt_path, receipt)):
                path.write_text(json.dumps(payload), encoding="utf-8")
            arguments = SimpleNamespace(
                acceptance=acceptance_path,
                promotion_acceptance=receipt_path,
                rc_manifest=rc_manifest_path,
                asset_directory=assets,
                tag="v1.1.0",
            )

            accepted, promotion = _validate_stable_publication_authority_inputs(
                arguments
            )
            self.assertEqual(accepted["identity"], acceptance["identity"])
            self.assertEqual(promotion["identity"], receipt["identity"])

            rc_manifest_path.write_bytes(rc_manifest_path.read_bytes() + b" ")
            with self.assertRaisesRegex(PublicationError, "RC manifest"):
                _validate_stable_publication_authority_inputs(arguments)
            rc_manifest_path.write_bytes(rc_manifest_path.read_bytes()[:-1])
            (assets / "installer-materials.tar").write_bytes(b"tampered")
            with self.assertRaisesRegex(PublicationError, "immutable materials"):
                _validate_stable_publication_authority_inputs(arguments)

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
            trust_kit = create_test_initial_trust_kit(root)
            self.run_cli(
                "build-installer-materials",
                "--root", ROOT,
                "--wheelhouse", wheelhouse,
                "--output", installer_materials,
                "--initial-trust-kit", trust_kit,
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
            trust_kit = create_test_initial_trust_kit(root)
            self.run_cli(
                "build-installer-materials", "--root", ROOT,
                "--wheelhouse", wheelhouse, "--output", installer_materials,
                "--initial-trust-kit", trust_kit,
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

    def test_resolve_version_consumes_the_canonical_incident_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            tags = Path(directory) / "tags.txt"
            tags.write_text("v1.0.0\n", encoding="utf-8")
            completed = self.run_cli(
                "resolve-version",
                "--tags-file", tags,
                "--publication-reservations-file",
                ROOT / "release" / "publication-reservations.json",
                "--bump", "minor",
                "--channel", "rc",
            )
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "releaseTag": "v1.1.0-rc.6",
                    "sequence": 6,
                    "targetVersion": "v1.1.0",
                },
            )

    def test_resolve_version_rejects_duplicate_ledger_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tags = root / "tags.txt"
            reservations = root / "publication-reservations.json"
            tags.write_text("v1.0.0\n", encoding="utf-8")
            reservations.write_text(
                '{"schemaVersion":1,"schemaVersion":1,"reservations":[]}\n',
                encoding="utf-8",
            )
            completed = self.run_cli(
                "resolve-version",
                "--tags-file", tags,
                "--publication-reservations-file", reservations,
                "--bump", "minor",
                "--channel", "rc",
                expected=2,
            )
            self.assertIn("Duplicate JSON field", completed.stderr)

    def test_cli_errors_are_machine_readable_and_nonzero(self):
        completed = self.run_cli(
            "resolve-version",
            "--tags-file", ROOT / "release" / "missing-tags.txt",
            "--publication-reservations-file",
            ROOT / "release" / "publication-reservations.json",
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

    def test_authority_snapshot_reads_each_path_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "authority.json"
            original = b'{"value":"original"}\n'
            path.write_bytes(original)
            snapshot = cli.PublicationInputSnapshot()

            self.assertEqual(
                snapshot.read(path, subject="test authority"),
                original,
            )
            path.write_bytes(b'{"value":"replacement"}\n')

            self.assertEqual(
                snapshot.read(path, subject="test authority"),
                original,
            )

    def test_json_and_checksum_authority_reads_do_not_use_path_read_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.json"
            output = root / "checksums.txt"
            authority.write_text('{"value":1}\n', encoding="utf-8")
            args = SimpleNamespace(files=[authority], output=output)

            with (
                mock.patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("Path.read_bytes bypass"),
                ),
                mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError("Path.read_text bypass"),
                ),
            ):
                self.assertEqual(cli._read_json(authority), {"value": 1})
                result = cli._write_checksums(args)

            self.assertEqual(result["files"], 1)
            self.assertTrue(output.is_file())

    def test_authority_snapshot_rejects_symlink_and_hardlink_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.json"
            original.write_text(json.dumps({"value": 1}), encoding="utf-8")
            aliases = []
            hardlink = root / "hardlink.json"
            os.link(original, hardlink)
            aliases.append(hardlink)
            symlink = root / "symlink.json"
            try:
                symlink.symlink_to(original)
            except (OSError, NotImplementedError):
                pass
            else:
                aliases.append(symlink)

            for alias in aliases:
                with self.subTest(alias=alias.name), self.assertRaises(
                    MaterialContractError
                ):
                    cli.PublicationInputSnapshot().read(
                        alias,
                        subject="test authority",
                    )


if __name__ == "__main__":
    unittest.main()
