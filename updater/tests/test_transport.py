from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from updater.transport import (
    ExplicitTransportPolicy,
    GitHubTransportSource,
    OfficialMirrorTransportSource,
    RELEASE_BUNDLE_OBJECTS,
    TransportError,
    TransportRequest,
    TransportSourceId,
)


class FakeResponse:
    def __init__(self, body: bytes, url: str, *, declared_length: int | None = None):
        self._stream = io.BytesIO(body)
        self._url = url
        self.headers = {
            "Content-Length": str(
                len(body) if declared_length is None else declared_length
            )
        }

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stream.close()


class FakeOpener:
    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        declared_lengths: dict[str, int] | None = None,
        redirected_urls: dict[str, str] | None = None,
        errors: dict[str, BaseException] | None = None,
    ):
        self.objects = objects
        self.declared_lengths = declared_lengths or {}
        self.redirected_urls = redirected_urls or {}
        self.errors = errors or {}
        self.urls: list[str] = []

    def open(self, request, timeout: int):
        del timeout
        url = request.full_url
        self.urls.append(url)
        name = Path(urlsplit(url).path).name
        if name in self.errors:
            raise self.errors[name]
        return FakeResponse(
            self.objects[name],
            self.redirected_urls.get(name, url),
            declared_length=self.declared_lengths.get(name),
        )


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


class ExplicitTransportPolicyTests(unittest.TestCase):
    def test_policy_accepts_only_explicit_closed_transport_sources(self):
        github = ExplicitTransportPolicy.github()
        mirror = ExplicitTransportPolicy.official_mirror()

        self.assertEqual(github.source, TransportSourceId.GITHUB)
        self.assertEqual(mirror.source, TransportSourceId.OFFICIAL_MIRROR)
        self.assertFalse(github.fallback_allowed)
        self.assertFalse(mirror.fallback_allowed)
        self.assertRegex(github.identity, r"^[0-9a-f]{64}$")
        self.assertNotEqual(github.identity, mirror.identity)

        with self.assertRaises(TransportError) as raised:
            ExplicitTransportPolicy(source="auto")
        self.assertEqual(raised.exception.code, "TRANSPORT_POLICY_INVALID")


class TransportRequestTests(unittest.TestCase):
    def test_release_request_has_a_fixed_object_set_and_no_url_input(self):
        request = TransportRequest.release_bundle("1.1.0-rc.2")

        self.assertEqual(request.exact_version, "1.1.0-rc.2")
        self.assertEqual(request.objects, RELEASE_BUNDLE_OBJECTS)
        self.assertRegex(request.identity, r"^[0-9a-f]{64}$")

        for invalid in ("v1.1.0", "../1.1.0", "1.1", "1.1.0/asset"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TransportError) as raised:
                    TransportRequest.release_bundle(invalid)
                self.assertEqual(
                    raised.exception.code,
                    "TRANSPORT_REQUEST_INVALID",
                )

        with self.assertRaises(TypeError):
            TransportRequest.release_bundle(
                "1.1.0",
                url="https://attacker.invalid/release",
            )


class TransportSourceTests(unittest.TestCase):
    def test_github_and_mirror_acquire_the_same_fixed_transport_objects(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle("1.1.0")
        github_opener = FakeOpener(bodies)
        mirror_opener = FakeOpener(bodies)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            github_staging = root / "github"
            mirror_staging = root / "mirror"
            github_staging.mkdir(mode=0o700)
            mirror_staging.mkdir(mode=0o700)

            github = GitHubTransportSource(opener=github_opener).acquire(
                request,
                github_staging,
            )
            mirror = OfficialMirrorTransportSource(opener=mirror_opener).acquire(
                request,
                mirror_staging,
            )

            self.assertEqual(github.receipt.transport_id, TransportSourceId.GITHUB)
            self.assertEqual(
                mirror.receipt.transport_id,
                TransportSourceId.OFFICIAL_MIRROR,
            )
            self.assertEqual(
                [(item.logical_name, item.sha256, item.size) for item in github.objects],
                [(item.logical_name, item.sha256, item.size) for item in mirror.objects],
            )
            for name, body in bodies.items():
                self.assertEqual(github.material(name).read_bytes(), body)
                self.assertEqual(mirror.material(name).read_bytes(), body)
            self.assertRegex(github.receipt.identity, r"^[0-9a-f]{64}$")
            self.assertRegex(mirror.receipt.identity, r"^[0-9a-f]{64}$")
            self.assertFalse(hasattr(github, "manifest"))
            self.assertFalse(hasattr(mirror, "manifest"))

        self.assertEqual(
            {urlsplit(url).netloc for url in github_opener.urls},
            {"github.com"},
        )
        self.assertEqual(
            {urlsplit(url).netloc for url in mirror_opener.urls},
            {"download.animemo.app"},
        )

    def test_oversized_object_fails_closed_and_removes_partial_state(self):
        bodies = {
            name: (b"x" * 4)
            for name in RELEASE_BUNDLE_OBJECTS
        }
        bodies["installer-materials.tar"] = b"x" * 9
        request = TransportRequest.release_bundle(
            "1.1.0",
            max_object_bytes=8,
            max_total_bytes=32,
        )

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(opener=FakeOpener(bodies)).acquire(
                    request,
                    staging,
                )

            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_RESPONSE_TOO_LARGE",
            )
            self.assertEqual(list(staging.iterdir()), [])

    def test_aggregate_limit_can_be_stricter_than_the_per_object_limit(self):
        bodies = {name: b"xx" for name in RELEASE_BUNDLE_OBJECTS}
        request = TransportRequest.release_bundle(
            "1.1.0",
            max_object_bytes=8,
            max_total_bytes=6,
        )

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(opener=FakeOpener(bodies)).acquire(
                    request,
                    staging,
                )
            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_RESPONSE_TOO_LARGE",
            )
            self.assertEqual(list(staging.iterdir()), [])

    def test_declared_length_mismatch_is_rejected_and_not_committed(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            declared_lengths={"checksums.txt": len(bodies["checksums.txt"]) + 1},
        )
        request = TransportRequest.release_bundle("1.1.0")

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(opener=opener).acquire(request, staging)

            self.assertEqual(raised.exception.code, "TRANSPORT_RECEIPT_INVALID")
            self.assertEqual(list(staging.iterdir()), [])

    def test_redirect_and_missing_object_have_stable_fail_closed_errors(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle("1.1.0")
        cases = (
            (
                FakeOpener(
                    bodies,
                    redirected_urls={
                        "checksums.txt": "https://attacker.invalid/checksums.txt"
                    },
                ),
                "TRANSPORT_REDIRECT_REJECTED",
            ),
            (
                FakeOpener(
                    bodies,
                    errors={
                        "checksums.txt": HTTPError(
                            "https://github.com/fixed",
                            404,
                            "missing",
                            None,
                            None,
                        )
                    },
                ),
                "TRANSPORT_OBJECT_MISSING",
            ),
        )

        for opener, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temporary:
                    staging = Path(temporary).resolve() / "private"
                    staging.mkdir(mode=0o700)
                    with self.assertRaises(TransportError) as raised:
                        GitHubTransportSource(opener=opener).acquire(
                            request,
                            staging,
                        )
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(list(staging.iterdir()), [])

    def test_interrupted_transaction_removes_every_partial_object(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            errors={"installer-materials.tar": URLError("fixture unavailable")},
        )

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                OfficialMirrorTransportSource(opener=opener).acquire(
                    TransportRequest.release_bundle("1.1.0"),
                    staging,
                )

            self.assertEqual(raised.exception.code, "TRANSPORT_UNAVAILABLE")
            self.assertTrue(raised.exception.retriable)
            self.assertEqual(list(staging.iterdir()), [])

    def test_timeout_has_a_stable_retriable_classification(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            errors={"checksums.txt": TimeoutError("fixture timeout")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(opener=opener).acquire(
                    TransportRequest.release_bundle("1.1.0"),
                    staging,
                )
            self.assertEqual(raised.exception.code, "TRANSPORT_TIMEOUT")
            self.assertTrue(raised.exception.retriable)
            self.assertEqual(list(staging.iterdir()), [])

    def test_official_mirror_endpoint_and_policy_cannot_be_user_urls_or_auto(self):
        with self.assertRaises(TransportError) as raised:
            OfficialMirrorTransportSource(
                endpoint_id="geo-auto",
                opener=FakeOpener({}),
            )
        self.assertEqual(
            raised.exception.code,
            "TRANSPORT_SOURCE_UNSUPPORTED",
        )

        with self.assertRaises(TypeError):
            OfficialMirrorTransportSource(
                opener=FakeOpener({}),
                origin="https://attacker.invalid",
            )
        with self.assertRaises(TypeError):
            ExplicitTransportPolicy(
                source=TransportSourceId.GITHUB,
                fallback_allowed=True,
            )

    def test_acquired_material_rejects_hardlink_replacement(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            staging = base / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(opener=FakeOpener(bodies)).acquire(
                TransportRequest.release_bundle("1.1.0"),
                staging,
            )
            material = acquired.material("checksums.txt")
            external = base / "external-copy"
            external.write_bytes(material.read_bytes())
            material.unlink()
            try:
                material.hardlink_to(external)
            except OSError as error:
                self.skipTest(f"hardlinks are unavailable: {error}")

            with self.assertRaises(TransportError) as raised:
                acquired.material("checksums.txt")
            self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

    def test_acquired_material_rejects_byte_tampering_against_its_receipt(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = OfficialMirrorTransportSource(
                opener=FakeOpener(bodies)
            ).acquire(
                TransportRequest.release_bundle("1.1.0"),
                staging,
            )
            material = acquired.material("release-manifest.json")
            material.write_bytes(b"tampered transport bytes")

            with self.assertRaises(TransportError) as raised:
                acquired.material("release-manifest.json")
            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_RECEIPT_INVALID",
            )

    def test_staging_must_be_an_absolute_direct_private_directory(self):
        source = GitHubTransportSource(opener=FakeOpener({}))
        with self.assertRaises(TransportError) as raised:
            source.acquire(
                TransportRequest.release_bundle("1.1.0"),
                Path("relative-staging"),
            )
        self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            direct = base / "direct"
            link = base / "link"
            direct.mkdir(mode=0o700)
            link_directory(link, direct)
            with self.assertRaises(TransportError) as raised:
                source.acquire(
                    TransportRequest.release_bundle("1.1.0"),
                    link,
                )
            self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

    def test_transport_receipt_identity_excludes_random_staging_location(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii")
            for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle("1.1.0")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            first_staging = base / "first"
            second_staging = base / "second"
            first_staging.mkdir(mode=0o700)
            second_staging.mkdir(mode=0o700)
            first = GitHubTransportSource(opener=FakeOpener(bodies)).acquire(
                request,
                first_staging,
            )
            second = GitHubTransportSource(opener=FakeOpener(bodies)).acquire(
                request,
                second_staging,
            )

            self.assertNotEqual(first.root, second.root)
            self.assertEqual(first.receipt.identity, second.receipt.identity)


if __name__ == "__main__":
    unittest.main()
