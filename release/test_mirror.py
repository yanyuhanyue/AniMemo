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
    _gh_environment,
    build_mirror_receipt,
    load_mirror_receipt_bytes,
    mirror_release_assets,
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
        return 206, {"Accept-Ranges": "bytes"}, content[: 1024 * 1024]


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
