from __future__ import annotations

import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from release.candidate import (
    LoadedVerifiedCandidate,
    canonical_json_bytes,
    sha256_bytes,
)
from release.publication_input import (
    PublicationInputError,
    build_publish_candidate_plan,
)
from release.test_candidate import (
    aggregate_receipt,
    candidate_input,
    verified_candidate_identity,
)


def _loaded() -> LoadedVerifiedCandidate:
    candidate = candidate_input()
    verified = verified_candidate_identity()
    root = Path("candidate-root")
    images = SimpleNamespace(
        images=tuple(
            SimpleNamespace(
                role=item["role"],
                repository=item["repository"],
                digest=item["digest"],
                platform=item["platform"],
                layout=root / "candidate-runtime" / "oci" / item["role"],
                config_digest=item["config_digest"],
                layer_digests=tuple(item["layer_digests"]),
            )
            for item in verified["oci_verification"]
        )
    )
    return LoadedVerifiedCandidate(
        root=root,
        verified_digest=sha256_bytes(canonical_json_bytes(verified)),
        verified=verified,
        candidate_input=candidate,
        manifest={},
        deployment_contract={},
        materials=SimpleNamespace(),
        images=images,
    )


def _receipt(loaded: LoadedVerifiedCandidate) -> dict[str, object]:
    receipt = aggregate_receipt()
    receipt["candidate_input_digest"] = sha256_bytes(
        canonical_json_bytes(loaded.candidate_input)
    )
    receipt["verified_candidate_digest"] = loaded.verified_digest
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return receipt


def _resign_receipt(receipt: dict[str, object]) -> dict[str, object]:
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return receipt


def _replace_loaded(
    loaded: LoadedVerifiedCandidate, **changes: object
) -> LoadedVerifiedCandidate:
    values = {
        "root": loaded.root,
        "verified_digest": loaded.verified_digest,
        "verified": loaded.verified,
        "candidate_input": loaded.candidate_input,
        "manifest": loaded.manifest,
        "deployment_contract": loaded.deployment_contract,
        "materials": loaded.materials,
        "images": loaded.images,
    }
    values.update(changes)
    return LoadedVerifiedCandidate(**values)


class PublishCandidateInputTests(unittest.TestCase):
    def test_plan_closes_the_exact_candidate_accepted_byte_tuple(self):
        loaded = _loaded()
        plan = build_publish_candidate_plan(loaded, _receipt(loaded))

        self.assertEqual(plan["schema"], "animemo.publish-candidate-plan/v1")
        self.assertEqual(
            plan["candidate_input_digest"], loaded.verified["candidate_input_sha256"]
        )
        self.assertEqual(plan["verified_candidate_digest"], loaded.verified_digest)
        self.assertEqual(
            plan["release_manifest_digest"],
            loaded.candidate_input["release_manifest_sha256"],
        )
        self.assertEqual(
            plan["candidate_runtime_inventory_digest"],
            sha256_bytes(
                canonical_json_bytes(
                    loaded.candidate_input["candidate_runtime_file_inventory"]
                )
            ),
        )
        self.assertEqual(
            plan["images"]["api"]["layout_path"], "candidate-runtime/oci/api"
        )
        self.assertEqual(
            plan["images"]["web"]["layout_path"], "candidate-runtime/oci/web"
        )
        self.assertEqual(plan["publish_rebuild_count"], 0)
        self.assertEqual(plan["manifest_generation_count"], 0)
        self.assertFalse(plan["mutation_authorized"])
        self.assertRegex(plan["plan_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_any_identity_or_acceptance_substitution_fails_closed(self):
        loaded = _loaded()
        cases = {}

        wrong_receipt = _receipt(loaded)
        wrong_receipt["verified_candidate_digest"] = "sha256:" + "f" * 64
        cases["acceptance digest"] = (loaded, _resign_receipt(wrong_receipt))

        for name, field, replacement in (
            ("run", "qualification_run_id", 99999999999),
            ("SHA", "source_sha", "f" * 40),
            ("tree", "source_tree", "e" * 40),
            ("version", "candidate_version", "v1.1.0-rc.999"),
        ):
            receipt = _receipt(loaded)
            receipt[field] = replacement
            cases[name] = (loaded, _resign_receipt(receipt))

        wrong_verified = copy.deepcopy(loaded.verified)
        wrong_verified["api_oci_digest"] = "sha256:" + "f" * 64
        cases["verified"] = (
            _replace_loaded(loaded, verified=wrong_verified),
            _receipt(loaded),
        )

        wrong_manifest = copy.deepcopy(loaded.candidate_input)
        wrong_manifest["release_manifest_sha256"] = "sha256:" + "f" * 64
        cases["release manifest"] = (
            _replace_loaded(loaded, candidate_input=wrong_manifest),
            _receipt(loaded),
        )

        wrong_inventory = copy.deepcopy(loaded.candidate_input)
        wrong_inventory["candidate_runtime_file_inventory"][0]["sha256"] = (
            "sha256:" + "f" * 64
        )
        cases["runtime inventory"] = (
            _replace_loaded(loaded, candidate_input=wrong_inventory),
            _receipt(loaded),
        )

        wrong_api = copy.deepcopy(loaded.images)
        next(item for item in wrong_api.images if item.role == "api").digest = (
            "sha256:" + "f" * 64
        )
        cases["api digest"] = (
            _replace_loaded(loaded, images=wrong_api),
            _receipt(loaded),
        )

        wrong_web = copy.deepcopy(loaded.images)
        next(item for item in wrong_web.images if item.role == "web").layout = (
            loaded.root / "candidate-runtime" / "oci" / "web-substitution"
        )
        cases["web layout"] = (
            _replace_loaded(loaded, images=wrong_web),
            _receipt(loaded),
        )

        for name, (candidate, receipt) in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    PublicationInputError, "PUBLISH_CANDIDATE_BYTE_MISMATCH"
                ),
            ):
                build_publish_candidate_plan(candidate, receipt)


if __name__ == "__main__":
    unittest.main()
