from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from jsonschema import validate as validate_schema

import release.formal_vm_controller as formal_provenance
from release.candidate import canonical_json_bytes
from release.formal_vm_controller import (
    FormalProvenanceInput,
    FormalProvenancePlan,
    OfflineActionsProvenancePreflight,
    ProvenancePreflightError,
)


class FormalVmControllerTests(unittest.TestCase):
    def test_wave_a_component_exposes_no_clone_or_publication_authority(self):
        self.assertFalse(hasattr(formal_provenance, "FormalVmController"))
        self.assertFalse(
            hasattr(formal_provenance, "ProvenanceAuthorizedCloneCapability")
        )
        self.assertFalse(hasattr(formal_provenance, "execute_production_formal_vm"))

    def test_offline_preflight_closes_all_five_production_verifier_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / ("verifier.exe" if os.name == "nt" else "verifier")
            verifier.write_bytes(b"production verifier fixture")
            verifier.chmod(0o700)
            subject_names = {
                "api-image": "ghcr.io/yanyuhanyue/animemo-api",
                "web-image": "ghcr.io/yanyuhanyue/animemo-web",
                "release-manifest": "release-manifest.json",
                "deployment-contract": "deployment-contract.json",
                "installer-materials": "installer-materials.tar",
            }
            claims = {
                name: {
                    "schemaVersion": 1,
                    "subject": {
                        "name": subject_names[name],
                        "sha256": "sha256:" + character * 64,
                    },
                    "repository": {
                        "name": "yanyuhanyue/AniMemo",
                        "repositoryId": "1327429673",
                        "ownerId": "111261350",
                    },
                    "workflow": ".github/workflows/release.yml",
                    "source": {
                        "commit": character * 40,
                        "ref": "refs/heads/main",
                    },
                    "signerDigest": character * 40,
                }
                for name, character in zip(
                    (
                        "api-image",
                        "web-image",
                        "release-manifest",
                        "deployment-contract",
                        "installer-materials",
                    ),
                    "12345",
                    strict=True,
                )
            }
            inputs = []
            originals = {}
            initial_bytes = {}
            for name, character in reversed(tuple(zip(claims, "12345", strict=True))):
                bundle = root / f"{name}.bundle.json"
                trusted_root = root / f"{name}.root.json"
                request = root / f"{name}.request.json"
                bundle.write_bytes(
                    canonical_json_bytes({"name": name, "kind": "bundle"})
                )
                trusted_root.write_bytes(
                    canonical_json_bytes({"name": name, "kind": "root"})
                )
                request.write_bytes(
                    canonical_json_bytes(
                        {
                            "schemaVersion": 1,
                            "mode": "actions-provenance",
                            "evidenceName": name,
                            "subject": {
                                "name": subject_names[name],
                                "sha256": "sha256:" + character * 64,
                                "size": 0,
                            },
                            "workflow": ".github/workflows/release.yml",
                            "sourceCommit": character * 40,
                        }
                    )
                )
                inputs.append(
                    FormalProvenanceInput(name, bundle, trusted_root, request)
                )
                originals[name] = {
                    "bundle": bundle,
                    "trusted-root": trusted_root,
                    "request": request,
                }
                initial_bytes[name] = {
                    key: path.read_bytes() for key, path in originals[name].items()
                }
            calls: list[tuple[str, ...]] = []
            originals_replaced = False

            def runner(command: tuple[str, ...]) -> bytes:
                nonlocal originals_replaced
                calls.append(command)
                request = Path(command[command.index("--request") + 1])
                name = request.name.removesuffix(".request.json")
                for argument, key in (
                    ("--bundle", "bundle"),
                    ("--trusted-root", "trusted-root"),
                    ("--request", "request"),
                ):
                    snapshot = Path(command[command.index(argument) + 1])
                    self.assertNotEqual(snapshot, originals[name][key])
                    self.assertEqual(snapshot.read_bytes(), initial_bytes[name][key])
                self.assertNotEqual(Path(command[0]), verifier)
                self.assertEqual(
                    Path(command[0]).read_bytes(), b"production verifier fixture"
                )
                if not originals_replaced:
                    verifier.write_bytes(b"replaced verifier")
                    for paths in originals.values():
                        for path in paths.values():
                            path.write_bytes(b"replaced original input")
                    originals_replaced = True
                return canonical_json_bytes(claims[name])

            receipt = OfflineActionsProvenancePreflight(
                FormalProvenancePlan(verifier=verifier, inputs=tuple(inputs)),
                runner=runner,
            ).verify()

            self.assertEqual(len(calls), 5)
            schema = json.loads(
                Path(
                    "release/formal-provenance-preflight-receipt.schema.json"
                ).read_text(encoding="utf-8")
            )
            validate_schema(receipt, schema)
            self.assertFalse(receipt["clone_authorized"])
            self.assertFalse(receipt["release_authority_granted"])
            self.assertFalse(receipt["publish_authorized"])
            self.assertEqual(
                [item["evidence_name"] for item in receipt["claims"]],
                sorted(claims),
            )
            self.assertRegex(receipt["preflight_digest"], r"^sha256:[0-9a-f]{64}$")
            for command in calls:
                self.assertIn("--bundle", command)
                self.assertIn("--trusted-root", command)
                self.assertIn("--request", command)
            self.assertEqual(
                {item["request_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["request"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                {item["bundle_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["bundle"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                {item["trusted_root_digest"] for item in receipt["claims"]},
                {
                    "sha256:" + hashlib.sha256(values["trusted-root"]).hexdigest()
                    for values in initial_bytes.values()
                },
            )
            self.assertEqual(
                receipt["verifier_digest"],
                "sha256:" + hashlib.sha256(b"production verifier fixture").hexdigest(),
            )

    def test_same_api_provenance_cannot_impersonate_all_required_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier = root / ("verifier.exe" if os.name == "nt" else "verifier")
            verifier.write_bytes(b"production verifier fixture")
            verifier.chmod(0o700)
            bundle = root / "api.bundle.json"
            trusted_root = root / "trusted-root.json"
            request = root / "api.request.json"
            bundle.write_bytes(b"api provenance bundle")
            trusted_root.write_bytes(b"trusted root")
            request.write_bytes(
                canonical_json_bytes(
                    {
                        "schemaVersion": 1,
                        "mode": "actions-provenance",
                        "evidenceName": "api-image",
                        "subject": {
                            "name": "ghcr.io/yanyuhanyue/animemo-api",
                            "sha256": "sha256:" + "1" * 64,
                            "size": 0,
                        },
                        "workflow": ".github/workflows/release.yml",
                        "sourceCommit": "1" * 40,
                    }
                )
            )
            inputs = tuple(
                FormalProvenanceInput(name, bundle, trusted_root, request)
                for name in (
                    "api-image",
                    "web-image",
                    "release-manifest",
                    "deployment-contract",
                    "installer-materials",
                )
            )
            api_claim = canonical_json_bytes(
                {
                    "schemaVersion": 1,
                    "subject": {
                        "name": "ghcr.io/yanyuhanyue/animemo-api",
                        "sha256": "sha256:" + "1" * 64,
                    },
                }
            )

            with self.assertRaisesRegex(
                ProvenancePreflightError,
                "FORMAL_PROVENANCE_EVIDENCE_BINDING_INVALID",
            ):
                OfflineActionsProvenancePreflight(
                    FormalProvenancePlan(verifier=verifier, inputs=inputs),
                    runner=lambda _command: api_claim,
                ).verify()


if __name__ == "__main__":
    unittest.main()
