from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from updater.authority import VerifiedReleaseMaterials
from updater.oci import OCIContractError, ImageAcquirer
from updater.transport import ExplicitTransportPolicy


def materials() -> VerifiedReleaseMaterials:
    images = {}
    repositories = {
        "api": "ghcr.io/yanyuhanyue/animemo-api",
        "postgres": "docker.io/library/postgres",
        "redis": "docker.io/library/redis",
        "web": "ghcr.io/yanyuhanyue/animemo-web",
    }
    for index, role in enumerate(sorted(repositories), start=1):
        images[role] = {
            "repository": repositories[role],
            "digest": "sha256:" + str(index) * 64,
        }
    return VerifiedReleaseMaterials(
        manifest={"images": images},
        deployment_contract={},
        verified=None,  # Image acquisition must not inspect installer files.
        identity_digest="sha256:" + "a" * 64,
    )


class FakeRunner:
    def __init__(self, *, substitute: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.substitute = substitute

    def run(self, argv, **options):
        del options
        self.calls.append(list(argv))
        if argv[1:3] == ["image", "inspect"]:
            reference = argv[-1]
            if self.substitute:
                reference = reference[:-1] + "f"
            return SimpleNamespace(stdout=json.dumps([reference]))
        return SimpleNamespace(stdout="")


class ImageAcquirerTests(unittest.TestCase):
    def test_exact_canonical_digests_are_pulled_and_read_back_under_bound_policy(self):
        runner = FakeRunner()
        policy = ExplicitTransportPolicy.official_mirror()

        receipt = ImageAcquirer(runner=runner).acquire(materials(), policy)

        self.assertEqual(receipt.transport_policy_identity, policy.identity)
        self.assertEqual(receipt.verified_release_identity, "sha256:" + "a" * 64)
        self.assertEqual(len(receipt.images), 4)
        self.assertTrue(all("@sha256:" in image.canonical_reference for image in receipt.images))
        self.assertEqual(sum(call[1] == "pull" for call in runner.calls), 4)
        self.assertEqual(sum(call[1:3] == ["image", "inspect"] for call in runner.calls), 4)

    def test_readback_digest_substitution_fails_closed(self):
        with self.assertRaisesRegex(OCIContractError, "OCI_RUNTIME_DIGEST_MISMATCH"):
            ImageAcquirer(runner=FakeRunner(substitute=True)).acquire(
                materials(), ExplicitTransportPolicy.github()
            )

    def test_arbitrary_policy_shape_is_rejected_before_docker(self):
        runner = FakeRunner()
        with self.assertRaisesRegex(OCIContractError, "OCI_TRANSPORT_POLICY_INVALID"):
            ImageAcquirer(runner=runner).acquire(materials(), object())
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
