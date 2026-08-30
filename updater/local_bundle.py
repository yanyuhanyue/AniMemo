"""Explicit, no-fallback transport Adapter for an offline release envelope.

This module copies the untrusted payload and its detached GitHub release
attestation into private staging exactly once.  Authority is established only
by :class:`updater.offline.OfflineReleaseVerifier`; transport receipts never
authorize a release.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from updater import __version__
from updater.authority import VerifiedReleaseMaterials
from updater.oci import VerifiedOCIImage, VerifiedOCIImageSet

MAX_LOCAL_PAYLOAD_BYTES = 32 * 1024 * 1024 * 1024
MAX_RELEASE_ATTESTATION_BYTES = 64 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_TRANSPORT_RECEIPT_NAME = "transport-receipt.json"
_MAX_TRANSPORT_RECEIPT_BYTES = 64 * 1024


class LocalBundleError(ValueError):
    """The local transport or its immutable authority proof is invalid."""


def _valid_release_execution_receipt(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
        "signedAt",
        "identity",
    }:
        return False
    if value.get("schema") != "animemo.release-execution-receipt/v1":
        return False
    for identity_field in (
        "publicationIdentity",
        "publicationExecutionReceiptIdentity",
        "signedClaimIdentity",
        "identity",
    ):
        item = value.get(identity_field)
        if (
            not isinstance(item, str)
            or len(item) != 71
            or not item.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in item[7:])
        ):
            return False
    signed_at = value.get("signedAt")
    if not isinstance(signed_at, str):
        return False
    try:
        parsed = datetime.strptime(signed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != signed_at:
        return False
    unsigned = dict(value)
    identity = unsigned.pop("identity")
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return identity == expected


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
        document[key] = value
    return document


def _policy_identity() -> str:
    value = {
        "authority": "github-release-attestation-sidecar",
        "fallback": "forbidden",
        "policyVersion": 1,
        "source": "local-bundle",
    }
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


LOCAL_BUNDLE_POLICY_IDENTITY = _policy_identity()


@dataclass(frozen=True)
class LocalBundleTransportPolicy:
    source: str = field(default="local-bundle", init=False)
    fallback_allowed: bool = field(default=False, init=False)
    identity: str = field(default=LOCAL_BUNDLE_POLICY_IDENTITY, init=False)


@dataclass(frozen=True)
class LocalBundleObjectReceipt:
    logical_name: str
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class LocalBundleReceipt:
    payload: LocalBundleObjectReceipt
    release_attestation: LocalBundleObjectReceipt
    identity: str


@dataclass(frozen=True)
class AcquiredLocalBundle:
    root: Path
    receipt: LocalBundleReceipt

    def material(self, logical_name: str) -> Path:
        if logical_name == "portable-payload":
            receipt = self.receipt.payload
        elif logical_name == "release-attestation":
            receipt = self.receipt.release_attestation
        else:
            raise LocalBundleError("LOCAL_BUNDLE_OBJECT_MISSING")
        target = self.root / receipt.relative_path
        size, digest = _regular_file_identity(target, max_bytes=receipt.size)
        if size != receipt.size or digest != receipt.sha256:
            raise LocalBundleError("LOCAL_BUNDLE_PRIVATE_COPY_CHANGED")
        return target


def _receipt_identity_document(
    payload: LocalBundleObjectReceipt,
    release_attestation: LocalBundleObjectReceipt,
) -> dict[str, object]:
    return {
        "objects": [
            {
                "logicalName": item.logical_name,
                "relativePath": item.relative_path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in (payload, release_attestation)
        ],
        "policyIdentity": LOCAL_BUNDLE_POLICY_IDENTITY,
        "receiptVersion": 1,
    }


def _receipt_identity(document: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


def _write_transport_receipt(root: Path, receipt: LocalBundleReceipt) -> None:
    document = {
        "schema": "animemo.local-bundle-transport-receipt/v1",
        "identity": receipt.identity,
        **_receipt_identity_document(receipt.payload, receipt.release_attestation),
    }
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(encoded) > _MAX_TRANSPORT_RECEIPT_BYTES:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    target = root / _TRANSPORT_RECEIPT_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_WRITE_FAILED")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_transport_receipt(
    root: Path,
    *,
    expected_identity: str,
) -> LocalBundleReceipt:
    root = _direct_private_directory(Path(root), create=False)
    expected_names = {
        "portable-payload.tar",
        "release-attestation.sigstore.json",
        _TRANSPORT_RECEIPT_NAME,
    }
    try:
        entries = list(root.iterdir())
    except OSError as error:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID") from error
    if {entry.name for entry in entries} != expected_names:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID") from error
        if (
            entry.is_symlink()
            or bool(getattr(entry, "is_junction", lambda: False)())
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    receipt_path = root / _TRANSPORT_RECEIPT_NAME
    descriptor, metadata = _open_regular_source(
        receipt_path,
        max_bytes=_MAX_TRANSPORT_RECEIPT_BYTES,
    )
    try:
        encoded = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            encoded.decode("ascii"),
            object_pairs_hook=_closed_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, LocalBundleError) as error:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID") from error
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema",
            "identity",
            "objects",
            "policyIdentity",
            "receiptVersion",
        }
        or document.get("schema")
        != "animemo.local-bundle-transport-receipt/v1"
        or document.get("identity") != expected_identity
    ):
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    identity_document = {
        "objects": document.get("objects"),
        "policyIdentity": document.get("policyIdentity"),
        "receiptVersion": document.get("receiptVersion"),
    }
    if _receipt_identity(identity_document) != expected_identity:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    objects = document.get("objects")
    if not isinstance(objects, list) or len(objects) != 2:
        raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
    expected_objects = (
        ("portable-payload", "portable-payload.tar", MAX_LOCAL_PAYLOAD_BYTES),
        (
            "release-attestation",
            "release-attestation.sigstore.json",
            MAX_RELEASE_ATTESTATION_BYTES,
        ),
    )
    receipts: list[LocalBundleObjectReceipt] = []
    for value, (logical_name, relative_path, maximum) in zip(
        objects, expected_objects, strict=True
    ):
        if (
            not isinstance(value, dict)
            or set(value) != {"logicalName", "relativePath", "sha256", "size"}
            or value.get("logicalName") != logical_name
            or value.get("relativePath") != relative_path
            or type(value.get("size")) is not int
            or not 0 < value["size"] <= maximum
            or type(value.get("sha256")) is not str
            or len(value["sha256"]) != 71
            or not value["sha256"].startswith("sha256:")
        ):
            raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
        receipt = LocalBundleObjectReceipt(
            logical_name=logical_name,
            relative_path=relative_path,
            sha256=value["sha256"],
            size=value["size"],
        )
        size, digest = _regular_file_identity(root / relative_path, max_bytes=maximum)
        if size != receipt.size or digest != receipt.sha256:
            raise LocalBundleError("LOCAL_BUNDLE_PRIVATE_COPY_CHANGED")
        receipts.append(receipt)
    return LocalBundleReceipt(
        payload=receipts[0],
        release_attestation=receipts[1],
        identity=expected_identity,
    )


def _direct_private_directory(path: Path, *, create: bool) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise LocalBundleError("LOCAL_BUNDLE_STAGING_UNSAFE")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LocalBundleError("LOCAL_BUNDLE_STAGING_UNSAFE") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or os.path.normcase(str(path.absolute())) != os.path.normcase(str(resolved))
    ):
        raise LocalBundleError("LOCAL_BUNDLE_STAGING_UNSAFE")
    if os.name != "nt" and metadata.st_mode & 0o077:
        raise LocalBundleError("LOCAL_BUNDLE_STAGING_UNSAFE")
    return resolved


def _open_regular_source(path: Path, *, max_bytes: int) -> tuple[int, os.stat_result]:
    path = Path(path)
    if not path.is_absolute():
        raise LocalBundleError("LOCAL_BUNDLE_PATH_UNSAFE")
    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise LocalBundleError("LOCAL_BUNDLE_PATH_UNSAFE") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_nlink != 1
    ):
        raise LocalBundleError("LOCAL_BUNDLE_PATH_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise LocalBundleError("LOCAL_BUNDLE_PATH_UNSAFE") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_dev != path_metadata.st_dev
        or metadata.st_ino != path_metadata.st_ino
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        os.close(descriptor)
        raise LocalBundleError("LOCAL_BUNDLE_PATH_UNSAFE")
    return descriptor, metadata


def _copy_regular_source(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
) -> LocalBundleObjectReceipt:
    descriptor, before = _open_regular_source(source, max_bytes=max_bytes)
    digest = hashlib.sha256()
    size = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        output = os.open(destination, flags, 0o600)
        try:
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise LocalBundleError("LOCAL_BUNDLE_RESOURCE_LIMIT")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output, view)
                    view = view[written:]
            os.fsync(output)
        finally:
            os.close(output)
        after = os.fstat(descriptor)
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    if (
        size != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or getattr(after, "st_ino", None) != getattr(before, "st_ino", None)
    ):
        destination.unlink(missing_ok=True)
        raise LocalBundleError("LOCAL_BUNDLE_SOURCE_CHANGED_DURING_COPY")
    return LocalBundleObjectReceipt(
        logical_name="",
        relative_path=destination.name,
        sha256="sha256:" + digest.hexdigest(),
        size=size,
    )


def _regular_file_identity(path: Path, *, max_bytes: int) -> tuple[int, str]:
    descriptor, metadata = _open_regular_source(path, max_bytes=max_bytes)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise LocalBundleError("LOCAL_BUNDLE_RESOURCE_LIMIT")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if size != metadata.st_size:
        raise LocalBundleError("LOCAL_BUNDLE_PRIVATE_COPY_CHANGED")
    return size, "sha256:" + digest.hexdigest()


class LocalBundleTransport:
    """Copy exactly two explicit files into private immutable staging."""

    policy = LocalBundleTransportPolicy()

    def acquire(
        self,
        *,
        payload: Path,
        release_attestation: Path,
        private_staging: Path,
    ) -> AcquiredLocalBundle:
        staging = _direct_private_directory(Path(private_staging), create=True)
        pending = Path(tempfile.mkdtemp(prefix=".local-bundle-", dir=staging))
        os.chmod(pending, 0o700)
        committed = False
        try:
            payload_receipt = _copy_regular_source(
                Path(payload),
                pending / "portable-payload.tar",
                max_bytes=MAX_LOCAL_PAYLOAD_BYTES,
            )
            sidecar_receipt = _copy_regular_source(
                Path(release_attestation),
                pending / "release-attestation.sigstore.json",
                max_bytes=MAX_RELEASE_ATTESTATION_BYTES,
            )
            payload_receipt = LocalBundleObjectReceipt(
                logical_name="portable-payload",
                relative_path=payload_receipt.relative_path,
                sha256=payload_receipt.sha256,
                size=payload_receipt.size,
            )
            sidecar_receipt = LocalBundleObjectReceipt(
                logical_name="release-attestation",
                relative_path=sidecar_receipt.relative_path,
                sha256=sidecar_receipt.sha256,
                size=sidecar_receipt.size,
            )
            identity_document = _receipt_identity_document(
                payload_receipt, sidecar_receipt
            )
            identity = _receipt_identity(identity_document)
            receipt = LocalBundleReceipt(
                payload=payload_receipt,
                release_attestation=sidecar_receipt,
                identity=identity,
            )
            _write_transport_receipt(pending, receipt)
            final = staging / f"acquired-{identity[:24]}"
            if final.exists() or final.is_symlink():
                raise LocalBundleError("LOCAL_BUNDLE_STAGING_COLLISION")
            os.replace(pending, final)
            committed = True
            return AcquiredLocalBundle(
                root=final,
                receipt=receipt,
            )
        finally:
            if not committed:
                shutil.rmtree(pending, ignore_errors=True)


def _offline_types() -> tuple[tuple[type, ...], type]:
    try:
        module = importlib.import_module("updater.offline")
        verifier_types = tuple(
            candidate
            for candidate in (
                getattr(module, "OfflineReleaseVerifier", None),
                getattr(module, "PersistentOfflineReleaseVerifier", None),
            )
            if isinstance(candidate, type)
        )
        release_type = module.VerifiedPortableRelease
    except (AttributeError, ImportError) as error:
        raise LocalBundleError("LOCAL_BUNDLE_OFFLINE_VERIFIER_UNAVAILABLE") from error
    if not verifier_types or not isinstance(release_type, type):
        raise LocalBundleError("LOCAL_BUNDLE_OFFLINE_VERIFIER_UNAVAILABLE")
    return verifier_types, release_type


def _validate_verified_release(value: Any, receipt: LocalBundleReceipt) -> Any:
    _, release_type = _offline_types()
    if not isinstance(value, release_type):
        raise LocalBundleError("LOCAL_BUNDLE_IMMUTABLE_PROOF_REQUIRED")
    materials = getattr(value, "materials", None)
    images = getattr(value, "images", None)
    if (
        not isinstance(materials, VerifiedReleaseMaterials)
        or not isinstance(images, VerifiedOCIImageSet)
        or getattr(value, "payload_sha256", None) != receipt.payload.sha256
        or getattr(value, "authority_evidence", None) is None
        or type(images.images) is not tuple
        or any(type(image) is not VerifiedOCIImage for image in images.images)
        or tuple(image.role for image in images.images)
        != ("api", "postgres", "redis", "web")
    ):
        raise LocalBundleError("LOCAL_BUNDLE_IMMUTABLE_PROOF_REQUIRED")
    try:
        for image in images.images:
            expected = materials.manifest["images"][image.role]
            if (
                image.repository != expected["repository"]
                or image.digest != expected["digest"]
                or image.platform != "linux/amd64"
            ):
                raise LocalBundleError("LOCAL_BUNDLE_OCI_AUTHORITY_MISMATCH")
    except (KeyError, TypeError) as error:
        raise LocalBundleError("LOCAL_BUNDLE_OCI_AUTHORITY_MISMATCH") from error
    return value


class LocalBundleReleaseSource:
    """One-release Resolver backed only by a privately staged offline envelope."""

    transport_policy = LocalBundleTransportPolicy()

    def __init__(
        self,
        *,
        acquired: AcquiredLocalBundle,
        verifier: Any,
        updater_version: str,
        verification_destination: Path,
        expected_rollback_version: str | None = None,
    ) -> None:
        verifier_types, _ = _offline_types()
        if not isinstance(verifier, verifier_types):
            raise LocalBundleError("LOCAL_BUNDLE_OFFLINE_VERIFIER_UNAVAILABLE")
        self._acquired = acquired
        self._verifier = verifier
        self._updater_version = updater_version
        self._verification_destination = verification_destination
        self._expected_rollback_version = expected_rollback_version
        self._verified = self._verify()

    @classmethod
    def from_media(
        cls,
        *,
        payload: Path,
        release_attestation: Path,
        cache_root: Path,
        verifier: Any,
        updater_version: str,
        expected_rollback_version: str | None = None,
    ) -> LocalBundleReleaseSource:
        cache = _direct_private_directory(Path(cache_root), create=True)
        acquired = LocalBundleTransport().acquire(
            payload=Path(payload),
            release_attestation=Path(release_attestation),
            private_staging=cache / "transport",
        )
        return cls(
            acquired=acquired,
            verifier=verifier,
            updater_version=updater_version,
            verification_destination=cache / "verified-materials",
            expected_rollback_version=expected_rollback_version,
        )

    @classmethod
    def from_staged(
        cls,
        *,
        cache_root: Path,
        transport_identity: str,
        verifier: Any,
        updater_version: str,
        expected_rollback_version: str | None = None,
    ) -> LocalBundleReleaseSource:
        if (
            type(transport_identity) is not str
            or len(transport_identity) != 71
            or not transport_identity.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in transport_identity[7:]
            )
        ):
            raise LocalBundleError("LOCAL_BUNDLE_RECEIPT_INVALID")
        cache = _direct_private_directory(Path(cache_root), create=False)
        identity = transport_identity[7:]
        root = cache / "transport" / f"acquired-{identity[:24]}"
        receipt = _read_transport_receipt(root, expected_identity=identity)
        return cls(
            acquired=AcquiredLocalBundle(root=root, receipt=receipt),
            verifier=verifier,
            updater_version=updater_version,
            verification_destination=cache / "verified-materials",
            expected_rollback_version=expected_rollback_version,
        )

    def _verify(self) -> Any:
        verified = self._verifier.verify(
            payload=self._acquired.material("portable-payload"),
            sidecar=self._acquired.material("release-attestation"),
            destination=self._verification_destination,
            updater_version=self._updater_version,
            expected_rollback_version=self._expected_rollback_version,
        )
        return _validate_verified_release(verified, self._acquired.receipt)

    @property
    def receipt(self) -> LocalBundleReceipt:
        return self._acquired.receipt

    def release_binding(self, version: str) -> dict[str, object]:
        materials = self.fetch_verified_materials(
            version,
            updater_version=self._updater_version,
        )
        manifest = materials.manifest
        release_attestation_identity = getattr(
            self._verified, "release_attestation_identity", None
        )
        trust_profile_version = getattr(
            self._verified, "trust_profile_version", None
        )
        trust_profile_identity = getattr(
            self._verified, "trust_profile_identity", None
        )
        payload_identity = getattr(self._verified, "payload_sha256", None)
        release_execution_receipt = getattr(
            self._verified, "release_execution_receipt", None
        )
        if (
            release_attestation_identity
            != self._acquired.receipt.release_attestation.sha256
            or payload_identity != self._acquired.receipt.payload.sha256
            or type(trust_profile_version) is not int
            or trust_profile_version < 1
            or type(trust_profile_identity) is not str
            or len(trust_profile_identity) != 71
            or not trust_profile_identity.startswith("sha256:")
            or not _valid_release_execution_receipt(release_execution_receipt)
        ):
            raise LocalBundleError("LOCAL_BUNDLE_AUTHORITY_BINDING_INVALID")
        manifest_identity = "sha256:" + hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            result = {
                "source": "local-bundle",
                "transportPolicyIdentity": self.transport_policy.identity,
                "verifiedReleaseIdentity": materials.identity_digest,
                "transportIdentity": "sha256:" + self._acquired.receipt.identity,
                "payloadIdentity": payload_identity,
                "releaseAttestationIdentity": release_attestation_identity,
                "releaseExecutionReceipt": dict(release_execution_receipt),
                "trustProfileVersion": trust_profile_version,
                "trustProfileIdentity": trust_profile_identity,
                "manifestIdentity": manifest_identity,
                "deploymentContractIdentity": manifest["deployment"][
                    "contractSha256"
                ],
                "apiDigest": manifest["images"]["api"]["digest"],
                "webDigest": manifest["images"]["web"]["digest"],
                "postgresDigest": manifest["images"]["postgres"]["digest"],
                "redisDigest": manifest["images"]["redis"]["digest"],
            }
            if self._expected_rollback_version is not None:
                result["expectedRollbackVersion"] = (
                    self._expected_rollback_version
                )
            return result
        except (KeyError, TypeError) as error:
            raise LocalBundleError("LOCAL_BUNDLE_AUTHORITY_BINDING_INVALID") from error

    def list_releases(
        self,
        channel: str,
        *,
        refresh: bool = False,
    ) -> list[dict[str, object]]:
        if refresh:
            self._verified = self._verify()
        manifest = self._verified.materials.manifest
        release = manifest["release"]
        if channel != release["channel"]:
            return []
        return [
            {
                "version": release["version"],
                "channel": release["channel"],
                "publishedAt": None,
            }
        ]

    def fetch_verified_materials(
        self,
        version: str,
        *,
        updater_version: str = __version__,
        refresh: bool = False,
    ) -> VerifiedReleaseMaterials:
        if updater_version != self._updater_version:
            raise LocalBundleError("LOCAL_BUNDLE_UPDATER_VERSION_CHANGED")
        if refresh:
            refreshed = self._verify()
            if (
                refreshed.materials.identity_digest
                != self._verified.materials.identity_digest
                or refreshed.materials.manifest != self._verified.materials.manifest
            ):
                raise LocalBundleError("LOCAL_BUNDLE_VERIFIED_RELEASE_CHANGED")
            self._verified = refreshed
        materials = self._verified.materials
        if version != materials.manifest["release"]["version"]:
            raise LocalBundleError("LOCAL_BUNDLE_RELEASE_VERSION_MISMATCH")
        return materials

    def fetch_verified(
        self,
        version: str,
        *,
        updater_version: str = __version__,
        refresh: bool = False,
    ) -> dict[str, object]:
        return self.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        ).manifest

    def verified_images(self, version: str) -> VerifiedOCIImageSet:
        materials = self.fetch_verified_materials(
            version,
            updater_version=self._updater_version,
        )
        del materials
        return self._verified.images

    def acquire_images(self, materials, image_acquirer):
        if materials is not self._verified.materials:
            raise LocalBundleError("LOCAL_BUNDLE_VERIFIED_RELEASE_CHANGED")
        acquire_local = getattr(image_acquirer, "acquire_local", None)
        if not callable(acquire_local):
            raise LocalBundleError("LOCAL_BUNDLE_OCI_IMPORT_UNAVAILABLE")
        return acquire_local(
            materials,
            self._verified.images,
            self.transport_policy,
        )
