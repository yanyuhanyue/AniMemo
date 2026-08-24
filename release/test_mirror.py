from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from release.mirror import (
    CACHE_CONTROL,
    MIRROR_ORIGIN,
    MIRROR_PATH_PREFIX,
    MirrorError,
    MirrorObjectConflict,
    OfficialMirrorPublicReader,
    OfficialReleaseMirrorPublisher,
    R2S3ObjectStore,
    _collect_response_headers,
    _gh_environment,
    _is_immutable_cache_control,
    _verify_github_release_authority,
    build_mirror_receipt,
    load_mirror_receipt_bytes,
    mirror_release_assets,
    publish_release_mirror,
    validate_mirror_receipt,
)

TAG = "v1.1.0-rc.10"
COMMIT = "0" * 40


def identity(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, tuple[str, str]] = {}
        self.writes: list[str] = []

    def read_to(self, key: str, destination: Path) -> bool:
        if key not in self.objects:
            return False
        destination.write_bytes(self.objects[key])
        return True

    def put_file_if_absent(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
        cache_control: str,
    ) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = source.read_bytes()
        self.metadata[key] = (content_type, cache_control)
        self.writes.append(key)
        return True


class MemoryPublicReader:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.reads: list[str] = []
        self.ranges: list[str] = []

    def read_to(self, url: str, destination: Path) -> tuple[int, dict[str, str]]:
        self.reads.append(url)
        prefix = MIRROR_ORIGIN + "/"
        if not url.startswith(prefix):
            raise AssertionError(url)
        key = url.removeprefix(prefix)
        if key not in self.store.objects:
            return 404, {}
        content = self.store.objects[key]
        destination.write_bytes(content)
        content_type, cache_control = self.store.metadata[key]
        return 200, {
            "Content-Length": str(len(content)),
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            "Cache-Control": cache_control,
        }

    def first_mib(self, url: str) -> tuple[int, dict[str, str], bytes]:
        self.ranges.append(url)
        key = url.removeprefix(MIRROR_ORIGIN + "/")
        content = self.store.objects[key]
        body = content[: 1024 * 1024]
        return 206, {
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(content)}",
        }, body


class CloudflareMemoryPublicReader(MemoryPublicReader):
    def read_to(self, url: str, destination: Path) -> tuple[int, dict[str, str]]:
        status, headers = super().read_to(url, destination)
        if status == 200:
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return status, headers


class CloudflareRangeMemoryPublicReader(CloudflareMemoryPublicReader):
    def first_mib(self, url: str) -> tuple[int, dict[str, str], bytes]:
        self.ranges.append(url)
        key = url.removeprefix(MIRROR_ORIGIN + "/")
        content = self.store.objects[key]
        body = content[: 1024 * 1024]
        return 206, {
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(content)}",
        }, body


class InvalidRangeMemoryPublicReader(MemoryPublicReader):
    def __init__(self, store: MemoryStore, range_headers: dict[str, str]) -> None:
        super().__init__(store)
        self.range_headers = range_headers

    def first_mib(self, url: str) -> tuple[int, dict[str, str], bytes]:
        self.ranges.append(url)
        key = url.removeprefix(MIRROR_ORIGIN + "/")
        content = self.store.objects[key]
        return 206, self.range_headers, content[: 1024 * 1024]


class CacheControlValidationTests(unittest.TestCase):
    def test_equivalent_closed_directive_lists_are_accepted(self) -> None:
        for value in (
            "public,max-age=31536000,immutable",
            "public, max-age=31536000, immutable",
            "IMMUTABLE, PUBLIC, MAX-AGE=31536000",
            "\tpublic,\tmax-age=31536000,\timmutable\t",
        ):
            with self.subTest(value=value):
                self.assertTrue(_is_immutable_cache_control(value))

    def test_missing_duplicate_or_additional_directives_are_rejected(self) -> None:
        for value in (
            None,
            "public,max-age=31536000",
            "public,max-age=31536000,immutable,no-store",
            "public,public,max-age=31536000,immutable",
            "private,max-age=31536000,immutable",
            "\vpublic,max-age=31536000,immutable",
            "\fpublic,max-age=31536000,immutable",
            "\r\n public,max-age=31536000,immutable",
            "\u00a0public,max-age=31536000,immutable",
        ):
            with self.subTest(value=value):
                self.assertFalse(_is_immutable_cache_control(value))

    def test_duplicate_response_fields_are_merged_before_validation(self) -> None:
        class DuplicateHeaders(dict[str, str]):
            def items(self):
                return [
                    ("Cache-Control", "public,max-age=31536000,immutable"),
                    ("cache-control", "no-store"),
                ]

        collected = _collect_response_headers(DuplicateHeaders())

        self.assertEqual(
            collected["cache-control"],
            "public,max-age=31536000,immutable,no-store",
        )
        self.assertFalse(_is_immutable_cache_control(collected["cache-control"]))


class MirrorWorkflowIdentityTests(unittest.TestCase):
    def test_publisher_rejects_workflow_sha_that_differs_from_main_checkout(self):
        environment = {
            "GITHUB_REPOSITORY": "yanyuhanyue/AniMemo",
            "GITHUB_WORKFLOW_REF": (
                "yanyuhanyue/AniMemo/.github/workflows/"
                "release-mirror.yml@refs/heads/main"
            ),
            "GITHUB_WORKFLOW_SHA": "1" * 40,
        }
        checkout = mock.Mock(returncode=0, stdout=("2" * 40) + "\n")
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch("release.mirror.subprocess.run", return_value=checkout),
            self.assertRaisesRegex(MirrorError, "workflow identity"),
        ):
            publish_release_mirror(TAG)


class MirrorReleaseAuthorityTests(unittest.TestCase):
    def test_stable_file_attestations_use_promotion_provenance(self) -> None:
        tag = "v1.1.0"
        application_commit = "1" * 40
        provenance_commit = "2" * 40
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            names = mirror_release_assets(tag)
            inventory = []
            for name in names:
                content = ("stable:" + name).encode()
                (directory / name).write_bytes(content)
                inventory.append(
                    {
                        "name": name,
                        "state": "uploaded",
                        "size": len(content),
                        "digest": identity(content),
                    }
                )
            metadata = {
                "tag_name": tag,
                "name": tag,
                "draft": False,
                "immutable": True,
                "id": 123,
                "assets": inventory,
            }
            manifest = {
                "release": {"version": tag, "commit": application_commit},
                "images": {
                    "api": {"digest": "sha256:" + "a" * 64},
                    "web": {"digest": "sha256:" + "b" * 64},
                },
                "deployment": {"contractSha256": "sha256:" + "c" * 64},
                "provenance": {
                    "workflow": ".github/workflows/promote-release.yml",
                    "sourceCommit": provenance_commit,
                },
            }
            deployment = {}
            portable = directory / names[-1]
            portable_digest = identity(portable.read_bytes())
            calls: list[tuple[str, ...]] = []

            def github_json(arguments: tuple[str, ...]):
                endpoint = arguments[-1]
                if endpoint.endswith(f"releases/tags/{tag}"):
                    return metadata
                if endpoint.endswith(f"git/ref/tags/{tag}"):
                    return {"object": {"type": "tag", "sha": "3" * 40}}
                if endpoint.endswith("git/tags/" + "3" * 40):
                    return {
                        "tag": tag,
                        "message": tag + "\n",
                        "object": {"type": "commit", "sha": application_commit},
                    }
                raise AssertionError(arguments)

            def strict_json(path: Path, *, label: str):
                return manifest if path.name == "release-manifest.json" else deployment

            inspection = mock.Mock(
                archive_sha256=portable_digest,
                archive_size=portable.stat().st_size,
            )
            with (
                mock.patch("release.mirror._gh_json", side_effect=github_json),
                mock.patch("release.mirror._run_gh", side_effect=calls.append),
                mock.patch("release.mirror._verify_checksums"),
                mock.patch("release.mirror._strict_json_file", side_effect=strict_json),
                mock.patch("release.contract.validate_manifest"),
                mock.patch("release.contract.validate_deployment_contract"),
                mock.patch(
                    "release.contract.deployment_contract_digest",
                    return_value=manifest["deployment"]["contractSha256"],
                ),
                mock.patch("release.portable.inspect_portable_archive", return_value=inspection),
            ):
                _verify_github_release_authority(tag, directory)

        attestation_calls = [call for call in calls if call[:2] == ("attestation", "verify")]
        self.assertEqual(len(attestation_calls), 5)
        for call in attestation_calls[:2]:
            self.assertIn("yanyuhanyue/AniMemo/.github/workflows/release.yml", call)
            self.assertEqual(call[call.index("--source-digest") + 1], application_commit)
        for call in attestation_calls[2:]:
            self.assertIn(
                "yanyuhanyue/AniMemo/.github/workflows/promote-release.yml", call
            )
            self.assertEqual(call[call.index("--source-digest") + 1], provenance_commit)


class MirrorReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contents = {
            name: ("fixture:" + name).encode("utf-8")
            for name in mirror_release_assets(TAG)
        }
        self.assets = [
            {"name": name, "size": len(self.contents[name]), "sha256": identity(self.contents[name])}
            for name in mirror_release_assets(TAG)
        ]

    def receipt(self) -> dict[str, object]:
        return build_mirror_receipt(
            release_tag=TAG,
            release_id=375630845,
            release_commit=COMMIT,
            assets=self.assets,
            publisher_run_id=123456,
            published_at="2026-08-24T15:00:00Z",
        )

    def test_receipt_is_closed_ordered_and_self_digesting(self) -> None:
        receipt = self.receipt()

        self.assertEqual(validate_mirror_receipt(receipt), receipt)
        self.assertEqual(receipt["schemaVersion"], 1)
        self.assertEqual(receipt["repository"], "yanyuhanyue/AniMemo")
        self.assertEqual(receipt["releaseTag"], TAG)
        self.assertTrue(receipt["releaseImmutable"])
        self.assertEqual(receipt["assetCount"], 5)
        self.assertEqual(
            [item["name"] for item in receipt["assets"]],
            list(mirror_release_assets(TAG)),
        )
        self.assertEqual(receipt["mirrorOrigin"], MIRROR_ORIGIN)
        self.assertEqual(receipt["mirrorPrefix"], MIRROR_PATH_PREFIX)
        self.assertRegex(receipt["receiptDigest"], r"^sha256:[0-9a-f]{64}$")

    def test_duplicate_unknown_reordered_and_tampered_receipts_fail(self) -> None:
        receipt = self.receipt()
        encoded = json.dumps(receipt, separators=(",", ":"))
        duplicate = encoded.replace('"repository":', '"repository":"attacker/repo","repository":', 1)
        with self.assertRaises(MirrorError):
            load_mirror_receipt_bytes(duplicate.encode("utf-8"))

        cases = []
        unknown = dict(receipt)
        unknown["fallback"] = "github"
        cases.append(unknown)
        reordered = dict(receipt)
        reordered["assets"] = list(reversed(receipt["assets"]))
        cases.append(reordered)
        tampered = dict(receipt)
        tampered["publisherRunId"] = 999
        cases.append(tampered)
        for value in cases:
            with self.subTest(keys=list(value)), self.assertRaises(MirrorError):
                validate_mirror_receipt(value)


class MirrorPublisherTests(MirrorReceiptTests):
    def _write_assets(self, root: Path) -> None:
        for name, content in self.contents.items():
            (root / name).write_bytes(content)

    def test_publisher_writes_exact_assets_then_marker_and_reconciles_equal_bytes(self) -> None:
        store = MemoryStore()
        reader = MemoryPublicReader(store)
        publisher = OfficialReleaseMirrorPublisher(store=store, public_reader=reader)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            first = publisher.publish(receipt=self.receipt(), asset_directory=root)
            second = publisher.publish(receipt=self.receipt(), asset_directory=root)

        prefix = f"{MIRROR_PATH_PREFIX}/{TAG}/"
        self.assertEqual(store.writes[-1], prefix + "mirror-receipt.json")
        self.assertEqual(store.writes[:-1], [prefix + name for name in mirror_release_assets(TAG)])
        self.assertEqual(first["uploadedObjectCount"], 6)
        self.assertEqual(second["uploadedObjectCount"], 0)
        self.assertEqual(second["existingEqualObjectCount"], 6)
        self.assertTrue(all(value[1] == CACHE_CONTROL for value in store.metadata.values()))
        asset_urls = {
            f"{MIRROR_ORIGIN}/{MIRROR_PATH_PREFIX}/{TAG}/{name}"
            for name in mirror_release_assets(TAG)
        }
        self.assertTrue(all(reader.reads.count(url) == 4 for url in asset_urls))
        self.assertTrue(all(reader.ranges.count(url) == 2 for url in asset_urls))

    def test_publisher_accepts_cloudflare_cache_control_serialization(self) -> None:
        store = MemoryStore()
        reader = CloudflareMemoryPublicReader(store)
        publisher = OfficialReleaseMirrorPublisher(store=store, public_reader=reader)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            result = publisher.publish(receipt=self.receipt(), asset_directory=root)

        self.assertEqual(result["uploadedObjectCount"], 6)
        self.assertEqual(result["publicReadback"], "PASS")

    def test_publisher_accepts_bound_206_without_optional_accept_ranges(self) -> None:
        store = MemoryStore()
        reader = CloudflareRangeMemoryPublicReader(store)
        publisher = OfficialReleaseMirrorPublisher(store=store, public_reader=reader)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            result = publisher.publish(receipt=self.receipt(), asset_directory=root)

        self.assertEqual(result["uploadedObjectCount"], 6)
        self.assertEqual(result["rangeStatus"], "PASS")

    def test_publisher_rejects_unbound_or_inconsistent_206_response(self) -> None:
        cases = (
            {"Content-Length": "1"},
            {"Content-Length": "5", "Content-Range": "bytes 1-5/5"},
            {
                "Content-Length": "5",
                "Content-Range": "bytes 0-4/5",
                "Accept-Ranges": "none",
            },
        )
        for headers in cases:
            with self.subTest(headers=headers):
                store = MemoryStore()
                reader = InvalidRangeMemoryPublicReader(store, headers)
                publisher = OfficialReleaseMirrorPublisher(
                    store=store, public_reader=reader
                )
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._write_assets(root)
                    with self.assertRaisesRegex(MirrorError, "Range readback"):
                        publisher.publish(
                            receipt=self.receipt(), asset_directory=root
                        )

    def test_existing_different_object_freezes_without_overwrite(self) -> None:
        store = MemoryStore()
        reader = MemoryPublicReader(store)
        publisher = OfficialReleaseMirrorPublisher(store=store, public_reader=reader)
        prefix = f"{MIRROR_PATH_PREFIX}/{TAG}/"
        store.objects[prefix + "checksums.txt"] = b"different"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_assets(root)
            with self.assertRaises(MirrorObjectConflict):
                publisher.publish(receipt=self.receipt(), asset_directory=root)
        self.assertEqual(store.objects[prefix + "checksums.txt"], b"different")
        self.assertEqual(store.writes, [])


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def read(self, _size: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    def getheader(self, _name: str) -> str | None:
        return None


class FakeConnection:
    response_status = 201
    instances: ClassVar[list[FakeConnection]] = []

    def __init__(self, host: str, *, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.request: tuple[str, str, bool, bool] | None = None
        self.headers: dict[str, str] = {}
        self.sent = bytearray()
        self.closed = False
        self.instances.append(self)

    def putrequest(
        self, method: str, path: str, *, skip_host: bool, skip_accept_encoding: bool
    ) -> None:
        self.request = (method, path, skip_host, skip_accept_encoding)

    def putheader(self, name: str, value: str) -> None:
        self.headers[name.lower()] = value

    def endheaders(self) -> None:
        return None

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self.response_status)

    def close(self) -> None:
        self.closed = True


class R2AdapterBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeConnection.instances.clear()
        FakeConnection.response_status = 201
        self.store = R2S3ObjectStore(
            account_id="a" * 32,
            access_key_id="k" * 16,
            secret_access_key="s" * 32,
        )
        self.key = f"{MIRROR_PATH_PREFIX}/{TAG}/checksums.txt"

    def test_fixed_bucket_key_and_conditional_put_are_signed_without_secret_headers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "checksums.txt"
            source.write_bytes(b"verified")
            with mock.patch(
                "release.mirror.http.client.HTTPSConnection", FakeConnection
            ):
                created = self.store.put_file_if_absent(
                    self.key,
                    source,
                    content_type="text/plain; charset=utf-8",
                    cache_control=CACHE_CONTROL,
                )

        self.assertTrue(created)
        connection = FakeConnection.instances[-1]
        self.assertEqual(connection.host, f"{'a' * 32}.r2.cloudflarestorage.com")
        self.assertEqual(
            connection.request,
            (
                "PUT",
                f"/animemo-release-mirror/{self.key}",
                True,
                True,
            ),
        )
        self.assertEqual(connection.headers["if-none-match"], "*")
        self.assertEqual(connection.headers["cache-control"], CACHE_CONTROL)
        self.assertNotIn("s" * 32, json.dumps(connection.headers))
        self.assertEqual(bytes(connection.sent), b"verified")
        self.assertTrue(connection.closed)

    def test_precondition_conflict_never_becomes_overwrite(self) -> None:
        FakeConnection.response_status = 412
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "checksums.txt"
            source.write_bytes(b"verified")
            with mock.patch(
                "release.mirror.http.client.HTTPSConnection", FakeConnection
            ):
                created = self.store.put_file_if_absent(
                    self.key,
                    source,
                    content_type="text/plain; charset=utf-8",
                    cache_control=CACHE_CONTROL,
                )
        self.assertFalse(created)
        self.assertEqual(FakeConnection.instances[-1].request[0], "PUT")

    def test_arbitrary_origin_query_bucket_and_key_are_unreachable(self) -> None:
        invalid_keys = (
            f"other-prefix/{TAG}/checksums.txt",
            f"{MIRROR_PATH_PREFIX}/{TAG}/unknown",
            f"{MIRROR_PATH_PREFIX}/{TAG}/nested/checksums.txt",
            f"{MIRROR_PATH_PREFIX}/latest/checksums.txt",
        )
        for key in invalid_keys:
            with self.subTest(key=key), self.assertRaises(MirrorError):
                self.store._canonical_path(key)
        for url in (
            f"https://attacker.invalid/{self.key}",
            f"{MIRROR_ORIGIN}/{self.key}?fallback=1",
            f"{MIRROR_ORIGIN}/{MIRROR_PATH_PREFIX}/{TAG}/unknown",
        ):
            with self.subTest(url=url), self.assertRaises(MirrorError):
                OfficialMirrorPublicReader._request(url)

    def test_r2_secrets_never_enter_github_subprocess_environment(self) -> None:
        secrets = {
            "ANIMEMO_RELEASE_MIRROR_ACCOUNT_ID": "a" * 32,
            "ANIMEMO_RELEASE_MIRROR_ACCESS_KEY_ID": "k" * 16,
            "ANIMEMO_RELEASE_MIRROR_SECRET_ACCESS_KEY": "s" * 32,
        }
        with mock.patch.dict(os.environ, secrets, clear=False):
            environment = _gh_environment()
        self.assertTrue(set(secrets).isdisjoint(environment))
        self.assertTrue(set(secrets.values()).isdisjoint(environment.values()))


if __name__ == "__main__":
    unittest.main()
