from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from updater.source import GitHubReleaseSource
from updater.tests.test_source import (
    FAKE_DEPLOYMENT_CONTRACT,
    FAKE_MATERIAL_ARCHIVE,
    FakePublicRest,
    FakeRunner,
    stable_manifest,
)
from updater.transport import (
    AcquiredTransportSet,
    ExplicitTransportPolicy,
    RELEASE_BUNDLE_OBJECTS,
    TransportError,
    TransportObjectReceipt,
    TransportReceipt,
    TransportSourceId,
)


def fixture_assets() -> dict[str, bytes]:
    manifest = stable_manifest()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    deployment_bytes = (
        json.dumps(
            FAKE_DEPLOYMENT_CONTRACT,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    checksums = (
        f"{hashlib.sha256(manifest_bytes).hexdigest()}  release-manifest.json\n"
        f"{hashlib.sha256(deployment_bytes).hexdigest()}  deployment-contract.json\n"
        f"{hashlib.sha256(FAKE_MATERIAL_ARCHIVE).hexdigest()}  installer-materials.tar\n"
    ).encode()
    return {
        "checksums.txt": checksums,
        "deployment-contract.json": deployment_bytes,
        "installer-materials.tar": FAKE_MATERIAL_ARCHIVE,
        "release-manifest.json": manifest_bytes,
    }


class FixtureTransport:
    def __init__(
        self,
        transport_id: TransportSourceId,
        assets: dict[str, bytes],
        *,
        fail: bool = False,
    ) -> None:
        self.transport_id = transport_id
        self.assets = assets
        self.fail = fail
        self.calls = 0

    def acquire(self, request, private_staging: Path) -> AcquiredTransportSet:
        self.calls += 1
        if self.fail:
            raise TransportError(
                "TRANSPORT_UNAVAILABLE",
                "fixture transport unavailable",
                transport_id=self.transport_id,
            )
        root = private_staging / f"fixture-{self.transport_id.value}"
        root.mkdir(mode=0o700)
        objects = []
        for name in RELEASE_BUNDLE_OBJECTS:
            payload = self.assets[name]
            (root / name).write_bytes(payload)
            objects.append(
                TransportObjectReceipt(
                    logical_name=name,
                    relative_path=name,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                )
            )
        receipts = tuple(objects)
        receipt = TransportReceipt(
            transport_id=self.transport_id,
            request_identity=request.identity,
            objects=receipts,
            identity="f" * 64,
        )
        return AcquiredTransportSet(root, receipts, receipt)


class ReleaseResolverTransportTests(unittest.TestCase):
    def test_same_bytes_via_github_and_mirror_have_the_same_verified_identity(self):
        assets = fixture_assets()
        github = FixtureTransport(TransportSourceId.GITHUB, assets)
        mirror = FixtureTransport(TransportSourceId.OFFICIAL_MIRROR, assets)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            github_source = GitHubReleaseSource(
                root / "github",
                runner=FakeRunner(stable_manifest()),
                rest=FakePublicRest(stable_manifest()),
                policy=ExplicitTransportPolicy.github(),
                transports={TransportSourceId.GITHUB: github},
            )
            mirror_source = GitHubReleaseSource(
                root / "mirror",
                runner=FakeRunner(stable_manifest()),
                rest=FakePublicRest(stable_manifest()),
                policy=ExplicitTransportPolicy.official_mirror(),
                transports={TransportSourceId.OFFICIAL_MIRROR: mirror},
            )

            github_verified = github_source.fetch_verified_materials("v1.0.0")
            mirror_verified = mirror_source.fetch_verified_materials("v1.0.0")

            self.assertEqual(github_verified.identity_digest, mirror_verified.identity_digest)
            self.assertEqual(github.calls, 1)
            self.assertEqual(mirror.calls, 1)
            self.assertEqual(mirror_source.transport_policy.source, TransportSourceId.OFFICIAL_MIRROR)

    def test_explicit_github_failure_never_invokes_mirror(self):
        assets = fixture_assets()
        github = FixtureTransport(TransportSourceId.GITHUB, assets, fail=True)
        mirror = FixtureTransport(TransportSourceId.OFFICIAL_MIRROR, assets)
        with tempfile.TemporaryDirectory() as temporary:
            source = GitHubReleaseSource(
                Path(temporary),
                runner=FakeRunner(stable_manifest()),
                rest=FakePublicRest(stable_manifest()),
                policy=ExplicitTransportPolicy.github(),
                transports={
                    TransportSourceId.GITHUB: github,
                    TransportSourceId.OFFICIAL_MIRROR: mirror,
                },
            )
            with self.assertRaises(TransportError) as raised:
                source.fetch_verified_materials("v1.0.0")

            self.assertEqual(raised.exception.code, "TRANSPORT_UNAVAILABLE")
            self.assertEqual(github.calls, 1)
            self.assertEqual(mirror.calls, 0)


if __name__ == "__main__":
    unittest.main()
