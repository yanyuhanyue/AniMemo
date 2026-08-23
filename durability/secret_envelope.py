"""In-memory Migration Secret Envelope v1 producer and consumer.

The reviewed profile uses the repository-pinned ``cryptography==50.0.0`` APIs:
Argon2id with the RFC 9106 64-MiB profile and standard AES-256-GCM. The suite ID
pins every parameter, so consumers never honor attacker-selected KDF costs.
Secret acquisition and protected-config publication deliberately remain outside
this module; callers pass sensitive material through redacting value objects.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Final, Literal, TypeAlias, cast
from uuid import UUID

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from durability.canonical import canonical_json_bytes, sha256_identity

ENVELOPE_FORMAT: Final = "animemo.migration-secret-envelope"
ENVELOPE_SCHEMA_VERSION: Final = 1
ENVELOPE_IDENTITY: Final = f"{ENVELOPE_FORMAT}/v{ENVELOPE_SCHEMA_VERSION}"
ENVELOPE_PATH: Final = "secrets/secret-envelope.json"
SUITE_ID: Final = "argon2id-m65536-t3-p4-aes-256-gcm-v1"

ARGON2_MEMORY_KIB: Final = 65_536
ARGON2_ITERATIONS: Final = 3
ARGON2_PARALLELISM: Final = 4
SALT_BYTES: Final = 16
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
TAG_BYTES: Final = 16

MAX_PASSPHRASE_BYTES: Final = 1_024
MIN_PASSPHRASE_BYTES: Final = 12
MAX_ENVELOPE_BYTES: Final = 4 * 1024 * 1024
MAX_BINDING_RECORD_BYTES: Final = 2 * 1024 * 1024
MAX_SECRET_BYTES: Final = 1024 * 1024
MAX_TOTAL_SECRET_BYTES: Final = 2 * 1024 * 1024
MAX_SECRET_ENTRIES: Final = 32
MAX_IDENTIFIER_BYTES: Final = 128

ArtifactType: TypeAlias = Literal["backup", "migration-bundle"]
Classification: TypeAlias = Literal["PRESERVE", "PRESERVE_OR_EXPLICIT_RECONFIGURE"]
Handling: TypeAlias = Literal["PRESERVE", "RECONFIGURE"]

_ARTIFACT_TYPES = frozenset(("backup", "migration-bundle"))
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]*\Z")

_SECRET_POLICY: Final[dict[str, Classification]] = {
    "CREDENTIAL_ENCRYPTION_KEY": "PRESERVE",
    "POSTGRES_PASSWORD": "PRESERVE",
    "DJANGO_SECRET_KEY": "PRESERVE_OR_EXPLICIT_RECONFIGURE",
    "REDIS_URL": "PRESERVE",
    "BANGUMI_OAUTH_CLIENT_SECRET": "PRESERVE_OR_EXPLICIT_RECONFIGURE",
    "RESEND_API_KEY": "PRESERVE_OR_EXPLICIT_RECONFIGURE",
    "TURNSTILE_SECRET": "PRESERVE_OR_EXPLICIT_RECONFIGURE",
}
_FORBIDDEN_BINDING_FIELDS = frozenset(
    (
        "ciphertext",
        "completedAt",
        "envelopeChecksum",
        "finalizedAt",
        "finalManifestChecksum",
        "manifestChecksum",
        "secretEnvelopeChecksum",
    )
)


class SecretEnvelopeError(ValueError):
    """Base error with only stable, non-secret machine fields."""

    compatibility_outcome: Literal["UNSUPPORTED", "CORRUPT"] | None = None

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SecretEnvelopeInputError(SecretEnvelopeError):
    """Producer input or caller acquisition boundary is invalid."""


class SecretEnvelopeOperationalError(SecretEnvelopeError):
    """The qualified crypto backend cannot currently evaluate the Envelope."""


class SecretEnvelopeUnsupportedError(SecretEnvelopeError):
    """A structurally recognizable envelope needs another implementation."""

    compatibility_outcome = "UNSUPPORTED"


class SecretEnvelopeCorruptError(SecretEnvelopeError):
    """A claimed-v1 envelope is malformed or fails authentication."""

    compatibility_outcome = "CORRUPT"


class _SensitiveMaterial:
    __slots__ = ("_material",)

    def __init__(self, material: bytes):
        self._material = material

    def __repr__(self) -> str:
        return f"<{type(self).__name__} redacted>"

    __str__ = __repr__


class Passphrase(_SensitiveMaterial):
    """Bounded passphrase acquired outside argv/environment handling."""

    mode: Final = "passphrase"

    @classmethod
    def from_text(cls, value: str) -> Passphrase:
        if not isinstance(value, str):
            raise SecretEnvelopeInputError("EXTERNAL_SECRET_INVALID")
        try:
            material = value.encode("utf-8")
        except UnicodeError:
            raise SecretEnvelopeInputError("EXTERNAL_SECRET_INVALID") from None
        return cls.from_bytes(material)

    @classmethod
    def from_bytes(cls, value: bytes) -> Passphrase:
        material = _copy_bytes(value, "EXTERNAL_SECRET_INVALID")
        if not MIN_PASSPHRASE_BYTES <= len(material) <= MAX_PASSPHRASE_BYTES:
            raise SecretEnvelopeInputError("EXTERNAL_SECRET_INVALID")
        return cls(material)


class OneTimeKey(_SensitiveMaterial):
    """Independent 256-bit key delivered outside the Envelope artifact."""

    mode: Final = "one-time-key"

    @classmethod
    def generate(cls) -> OneTimeKey:
        return cls(os.urandom(KEY_BYTES))

    @classmethod
    def from_bytes(cls, value: bytes) -> OneTimeKey:
        material = _copy_bytes(value, "EXTERNAL_SECRET_INVALID")
        if len(material) != KEY_BYTES:
            raise SecretEnvelopeInputError("EXTERNAL_SECRET_INVALID")
        return cls(material)

    def export(self) -> bytes:
        """Explicitly reveal a copy for the caller's protected delivery channel."""

        return bytes(self._material)


class SecretEntry(_SensitiveMaterial):
    """One allowlisted inner entry whose representation is always redacted."""

    __slots__ = ("classification", "handling", "name")

    def __init__(
        self,
        name: str,
        classification: Classification,
        handling: Handling,
        material: bytes,
    ):
        super().__init__(material)
        self.name = name
        self.classification = classification
        self.handling = handling

    @classmethod
    def preserve(cls, name: str, value: bytes) -> SecretEntry:
        material = _copy_bytes(value, "SECRET_ENTRY_INVALID")
        classification = _secret_classification(name)
        return cls(name, classification, "PRESERVE", material)

    @classmethod
    def reconfigure(cls, name: str) -> SecretEntry:
        classification = _secret_classification(name)
        if classification != "PRESERVE_OR_EXPLICIT_RECONFIGURE":
            raise SecretEnvelopeInputError("SECRET_DISPOSITION_INVALID")
        return cls(name, classification, "RECONFIGURE", b"")

    def reveal(self) -> bytes:
        """Explicitly reveal one preserved value to protected-config staging."""

        if self.handling != "PRESERVE":
            raise SecretEnvelopeInputError("SECRET_NOT_AVAILABLE")
        return bytes(self._material)


class SecretEnvelope:
    """Canonical encrypted artifact bytes with a non-disclosing representation."""

    __slots__ = ("_encoded",)

    def __init__(self, encoded: bytes):
        self._encoded = encoded

    @classmethod
    def from_bytes(cls, value: bytes) -> SecretEnvelope:
        encoded = _copy_bytes(value, "ENVELOPE_STRUCTURE_CORRUPT")
        if not encoded or len(encoded) > MAX_ENVELOPE_BYTES:
            raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
        return cls(encoded)

    def to_bytes(self) -> bytes:
        return bytes(self._encoded)

    def __repr__(self) -> str:
        return "<SecretEnvelope redacted>"

    __str__ = __repr__


class OpenedSecretPayload:
    """Authenticated payload. Secret entry representations remain redacted."""

    __slots__ = (
        "artifact_binding_digest",
        "artifact_id",
        "artifact_type",
        "credential_encryption_key_required",
        "entries",
        "source_instance_id",
    )

    def __init__(
        self,
        *,
        artifact_type: ArtifactType,
        artifact_id: str,
        artifact_binding_digest: str,
        source_instance_id: str,
        credential_encryption_key_required: bool,
        entries: tuple[SecretEntry, ...],
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.artifact_binding_digest = artifact_binding_digest
        self.source_instance_id = source_instance_id
        self.credential_encryption_key_required = credential_encryption_key_required
        self.entries = entries

    def get_secret(self, name: str) -> SecretEntry:
        for entry in self.entries:
            if entry.name == name and entry.handling == "PRESERVE":
                return entry
        raise SecretEnvelopeInputError("SECRET_NOT_AVAILABLE")

    def __repr__(self) -> str:
        return "<OpenedSecretPayload redacted>"

    __str__ = __repr__


def create_secret_envelope(
    *,
    external_secret: Passphrase | OneTimeKey,
    artifact_type: ArtifactType,
    artifact_id: str,
    artifact_binding_record: Mapping[str, object],
    source_instance_id: str,
    secret_entries: Sequence[SecretEntry],
    credential_encryption_key_required: bool = True,
) -> SecretEnvelope:
    """Create an Envelope bound to the supplied canonical pre-envelope record.

    The digest is always computed here; callers cannot inject a naked digest.
    """

    _validate_create_inputs(
        external_secret=external_secret,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_instance_id=source_instance_id,
        credential_encryption_key_required=credential_encryption_key_required,
    )
    artifact_binding_digest = _artifact_binding_digest(
        artifact_binding_record,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_instance_id=source_instance_id,
    )
    entries = _validate_entries(
        secret_entries,
        credential_encryption_key_required=credential_encryption_key_required,
    )
    _reject_credential_key_reuse(external_secret, entries, producer=True)

    salt = os.urandom(SALT_BYTES) if isinstance(external_secret, Passphrase) else None
    nonce = os.urandom(NONCE_BYTES)
    header = _build_header(
        mode=external_secret.mode,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        artifact_binding_digest=artifact_binding_digest,
        salt=salt,
        nonce=nonce,
    )
    payload = _build_payload(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        artifact_binding_digest=artifact_binding_digest,
        source_instance_id=source_instance_id,
        credential_encryption_key_required=credential_encryption_key_required,
        entries=entries,
    )
    plaintext = canonical_json_bytes(payload)
    aad = _canonical_aad(header)
    try:
        key = _derive_key(external_secret, salt)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        verified = AESGCM(key).decrypt(nonce, ciphertext, aad)
        if not hmac.compare_digest(verified, plaintext):
            raise SecretEnvelopeInputError("ENVELOPE_SELF_VERIFICATION_FAILED")
    except SecretEnvelopeError:
        raise
    except (MemoryError, RuntimeError, UnsupportedAlgorithm):
        raise SecretEnvelopeOperationalError("ENVELOPE_CRYPTO_UNAVAILABLE") from None
    except (InvalidTag, OverflowError, TypeError, ValueError):
        raise SecretEnvelopeInputError("ENVELOPE_SELF_VERIFICATION_FAILED") from None
    finally:
        key = b""
        plaintext = b""

    envelope_object = dict(header)
    envelope_object["ciphertext"] = _b64url_encode(ciphertext)
    encoded = canonical_json_bytes(cast(Mapping[str, object], envelope_object))
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise SecretEnvelopeInputError("SECRET_PROFILE_INVALID")
    return SecretEnvelope(encoded)


def open_secret_envelope(
    envelope: SecretEnvelope | bytes,
    *,
    external_secret: Passphrase | OneTimeKey,
    expected_artifact_type: ArtifactType,
    expected_artifact_id: str,
    expected_artifact_binding_record: Mapping[str, object],
    expected_source_instance_id: str,
) -> OpenedSecretPayload:
    """Recompute binding, authenticate, and parse without target mutation."""

    encoded = (
        envelope.to_bytes()
        if isinstance(envelope, SecretEnvelope)
        else _copy_bytes(envelope, "ENVELOPE_STRUCTURE_CORRUPT")
    )
    root = _parse_envelope_object(encoded)
    _evaluate_version_and_suite(root)
    header, ciphertext_text = _validate_v1_outer_structure(root)
    expected_artifact_binding_digest = _artifact_binding_digest(
        expected_artifact_binding_record,
        artifact_type=expected_artifact_type,
        artifact_id=expected_artifact_id,
        source_instance_id=expected_source_instance_id,
    )

    if not isinstance(external_secret, (Passphrase, OneTimeKey)):
        _authentication_failed()
    if (
        header["mode"] != external_secret.mode
        or header["binding"]["artifactType"] != expected_artifact_type
        or header["binding"]["artifactId"] != expected_artifact_id
        or header["binding"]["artifactBindingDigest"]
        != expected_artifact_binding_digest
    ):
        _authentication_failed()

    try:
        salt, nonce = _validated_crypto_metadata(header)
        ciphertext = _b64url_decode(ciphertext_text, maximum=MAX_ENVELOPE_BYTES)
        if len(ciphertext) < TAG_BYTES:
            _authentication_failed()
        key = _derive_key(external_secret, salt)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _canonical_aad(header))
    except SecretEnvelopeCorruptError as error:
        if error.code == "ENVELOPE_AUTHENTICATION_FAILED":
            raise
        _authentication_failed()
    except SecretEnvelopeError:
        raise
    except (MemoryError, OverflowError, RuntimeError, UnsupportedAlgorithm):
        raise SecretEnvelopeOperationalError("ENVELOPE_CRYPTO_UNAVAILABLE") from None
    except (InvalidTag, ValueError, TypeError, binascii.Error):
        _authentication_failed()
    finally:
        key = b""

    try:
        payload_object = _parse_inner_payload(plaintext)
        opened = _validate_inner_payload(payload_object, header)
        if opened.source_instance_id != expected_source_instance_id:
            _authentication_failed()
        _reject_credential_key_reuse(external_secret, opened.entries, producer=False)
        return opened
    finally:
        plaintext = b""


def _copy_bytes(value: object, error_code: str) -> bytes:
    if not isinstance(value, bytes):
        if error_code.startswith("ENVELOPE_"):
            raise SecretEnvelopeCorruptError(error_code)
        raise SecretEnvelopeInputError(error_code)
    return bytes(value)


def _secret_classification(name: object) -> Classification:
    if not isinstance(name, str) or name not in _SECRET_POLICY:
        raise SecretEnvelopeInputError("UNCLASSIFIED_SECRET")
    return _SECRET_POLICY[name]


def _validate_create_inputs(
    *,
    external_secret: object,
    artifact_type: object,
    artifact_id: object,
    source_instance_id: object,
    credential_encryption_key_required: object,
) -> None:
    if not isinstance(external_secret, (Passphrase, OneTimeKey)):
        raise SecretEnvelopeInputError("EXTERNAL_SECRET_INVALID")
    if artifact_type not in _ARTIFACT_TYPES:
        raise SecretEnvelopeInputError("ARTIFACT_IDENTITY_INVALID")
    if not _is_canonical_uuid(artifact_id) or not _is_canonical_uuid(
        source_instance_id
    ):
        raise SecretEnvelopeInputError("ARTIFACT_IDENTITY_INVALID")
    if not isinstance(credential_encryption_key_required, bool):
        raise SecretEnvelopeInputError("SECRET_PROFILE_INVALID")


def _artifact_binding_digest(
    value: Mapping[str, object],
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    source_instance_id: str,
) -> str:
    if (
        artifact_type not in _ARTIFACT_TYPES
        or not _is_canonical_uuid(artifact_id)
        or not _is_canonical_uuid(source_instance_id)
        or not isinstance(value, Mapping)
    ):
        raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")
    try:
        encoded = canonical_json_bytes(value)
        if not encoded or len(encoded) > MAX_BINDING_RECORD_BYTES:
            raise ValueError
        record = json.loads(encoded)
    except (RecursionError, TypeError, ValueError):
        raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID") from None
    if not isinstance(record, dict):
        raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")

    expected_format = {
        "backup": "animemo-instance-backup",
        "migration-bundle": "animemo-migration-bundle",
    }[artifact_type]
    id_field = {"backup": "backupId", "migration-bundle": "bundleId"}[artifact_type]
    version_field = {
        "backup": "schemaVersion",
        "migration-bundle": "formatVersion",
    }[artifact_type]
    record_source_id = record.get("sourceInstanceId", record.get("instanceId"))
    if record_source_id is None and isinstance(record.get("source"), dict):
        record_source_id = record["source"].get("instanceId")
    secret_profile = record.get("secretProfileIdentity")
    if secret_profile is None and isinstance(record.get("secretDisposition"), dict):
        secret_profile = record["secretDisposition"].get("profileIdentity")
    if (
        record.get("format") != expected_format
        or type(record.get(version_field)) is not int
        or record[version_field] != 1
        or record.get(id_field) != artifact_id
        or record_source_id != source_instance_id
        or secret_profile != ENVELOPE_IDENTITY
    ):
        raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")

    stack: list[object] = [record]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > 100_000:
            raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")
        if isinstance(item, dict):
            for name, child in item.items():
                if name in _FORBIDDEN_BINDING_FIELDS:
                    raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, float):
            raise SecretEnvelopeInputError("ARTIFACT_BINDING_INVALID")
    return sha256_identity(encoded)


def _validate_entries(
    values: Sequence[SecretEntry], *, credential_encryption_key_required: bool
) -> tuple[SecretEntry, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
    if not 0 <= len(values) <= MAX_SECRET_ENTRIES:
        raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
    entries: list[SecretEntry] = []
    seen: set[str] = set()
    total_secret_bytes = 0
    for entry in values:
        if not isinstance(entry, SecretEntry):
            raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
        if entry.name not in _SECRET_POLICY:
            raise SecretEnvelopeInputError("UNCLASSIFIED_SECRET")
        if entry.classification != _SECRET_POLICY[entry.name]:
            raise SecretEnvelopeInputError("SECRET_DISPOSITION_INVALID")
        allowed_handling = (
            frozenset(("PRESERVE",))
            if entry.classification == "PRESERVE"
            else frozenset(("PRESERVE", "RECONFIGURE"))
        )
        if entry.handling not in allowed_handling:
            raise SecretEnvelopeInputError("SECRET_DISPOSITION_INVALID")
        if entry.name in seen:
            raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
        if (
            entry.handling == "PRESERVE"
            and not 0 < len(entry._material) <= MAX_SECRET_BYTES
        ):
            raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
        if entry.handling == "PRESERVE":
            total_secret_bytes += len(entry._material)
            if total_secret_bytes > MAX_TOTAL_SECRET_BYTES:
                raise SecretEnvelopeInputError("SECRET_PROFILE_INVALID")
        if entry.handling == "RECONFIGURE" and entry._material:
            raise SecretEnvelopeInputError("SECRET_ENTRY_INVALID")
        seen.add(entry.name)
        entries.append(entry)
    if credential_encryption_key_required and "CREDENTIAL_ENCRYPTION_KEY" not in seen:
        raise SecretEnvelopeInputError("CREDENTIAL_CONTINUITY_REQUIRED")
    return tuple(entries)


def _reject_credential_key_reuse(
    external_secret: Passphrase | OneTimeKey,
    entries: Sequence[SecretEntry],
    *,
    producer: bool,
) -> None:
    for entry in entries:
        if entry.name == "CREDENTIAL_ENCRYPTION_KEY" and hmac.compare_digest(
            entry._material, external_secret._material
        ):
            if producer:
                raise SecretEnvelopeInputError("EXTERNAL_SECRET_REUSE_FORBIDDEN")
            raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")


def _build_header(
    *,
    mode: str,
    artifact_type: str,
    artifact_id: str,
    artifact_binding_digest: str,
    salt: bytes | None,
    nonce: bytes,
) -> dict[str, object]:
    if mode == "passphrase":
        assert salt is not None
        kdf: dict[str, object] = {
            "algorithm": "Argon2id",
            "iterations": ARGON2_ITERATIONS,
            "memoryKiB": ARGON2_MEMORY_KIB,
            "outputBytes": KEY_BYTES,
            "parallelism": ARGON2_PARALLELISM,
            "salt": _b64url_encode(salt),
            "saltEncoding": "base64url",
        }
    else:
        kdf = {"algorithm": "none"}
    return {
        "aead": {
            "algorithm": "AES-256-GCM",
            "keyBytes": KEY_BYTES,
            "nonce": _b64url_encode(nonce),
            "nonceBytes": NONCE_BYTES,
            "nonceEncoding": "base64url",
            "tagBytes": TAG_BYTES,
        },
        "binding": {
            "artifactBindingDigest": artifact_binding_digest,
            "artifactId": artifact_id,
            "artifactType": artifact_type,
        },
        "ciphertextEncoding": "base64url",
        "format": ENVELOPE_FORMAT,
        "kdf": kdf,
        "mode": mode,
        "schemaVersion": ENVELOPE_SCHEMA_VERSION,
        "suiteId": SUITE_ID,
    }


def _build_payload(
    *,
    artifact_type: str,
    artifact_id: str,
    artifact_binding_digest: str,
    source_instance_id: str,
    credential_encryption_key_required: bool,
    entries: Sequence[SecretEntry],
) -> dict[str, object]:
    encoded_entries: list[dict[str, object]] = []
    for entry in entries:
        encoded: dict[str, object] = {
            "classification": entry.classification,
            "handling": entry.handling,
            "name": entry.name,
        }
        if entry.handling == "PRESERVE":
            encoded["value"] = _b64url_encode(entry._material)
            encoded["valueEncoding"] = "base64url"
        encoded_entries.append(encoded)
    return {
        "artifactBinding": {
            "artifactBindingDigest": artifact_binding_digest,
            "artifactId": artifact_id,
            "artifactType": artifact_type,
        },
        "credentialEncryptionKeyRequired": credential_encryption_key_required,
        "payloadSchemaVersion": 1,
        "secretEntries": encoded_entries,
        "sourceInstanceId": source_instance_id,
    }


def _derive_key(external_secret: Passphrase | OneTimeKey, salt: bytes | None) -> bytes:
    if isinstance(external_secret, OneTimeKey):
        return bytes(external_secret._material)
    if salt is None or len(salt) != SALT_BYTES:
        _authentication_failed()
    try:
        return Argon2id(
            salt=salt,
            length=KEY_BYTES,
            iterations=ARGON2_ITERATIONS,
            lanes=ARGON2_PARALLELISM,
            memory_cost=ARGON2_MEMORY_KIB,
        ).derive(external_secret._material)
    except (MemoryError, RuntimeError, UnsupportedAlgorithm):
        raise SecretEnvelopeOperationalError("ENVELOPE_CRYPTO_UNAVAILABLE") from None


def _canonical_aad(header: Mapping[str, object]) -> bytes:
    aad = dict(header)
    aad["canonicalPath"] = ENVELOPE_PATH
    aad["envelopeIdentity"] = ENVELOPE_IDENTITY
    return canonical_json_bytes(cast(Mapping[str, object], aad))


def _parse_envelope_object(encoded: bytes) -> Mapping[str, object]:
    if not encoded or len(encoded) > MAX_ENVELOPE_BYTES:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    parsed = _strict_json_loads(encoded, "ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(parsed, Mapping):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    return cast(Mapping[str, object], parsed)


def _evaluate_version_and_suite(root: Mapping[str, object]) -> None:
    format_value = root.get("format")
    schema_version = root.get("schemaVersion")
    suite_id = root.get("suiteId")
    if format_value != ENVELOPE_FORMAT:
        if isinstance(format_value, str) and format_value:
            raise SecretEnvelopeUnsupportedError("ENVELOPE_VERSION_UNSUPPORTED")
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if schema_version != ENVELOPE_SCHEMA_VERSION:
        if schema_version > 0:
            raise SecretEnvelopeUnsupportedError("ENVELOPE_VERSION_UNSUPPORTED")
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(suite_id, str) or not suite_id:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if suite_id != SUITE_ID:
        raise SecretEnvelopeUnsupportedError("ENVELOPE_SUITE_UNSUPPORTED")


def _validate_v1_outer_structure(
    root: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    required = {
        "aead",
        "binding",
        "ciphertext",
        "ciphertextEncoding",
        "format",
        "kdf",
        "mode",
        "schemaVersion",
        "suiteId",
    }
    if set(root) != required:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    ciphertext = root["ciphertext"]
    if not isinstance(ciphertext, str):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    header = {key: root[key] for key in root if key != "ciphertext"}
    mode = header["mode"]
    if mode not in ("passphrase", "one-time-key"):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if header["ciphertextEncoding"] != "base64url":
        _authentication_failed()
    binding = header["binding"]
    if not isinstance(binding, Mapping) or set(binding) != {
        "artifactBindingDigest",
        "artifactId",
        "artifactType",
    }:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if (
        binding.get("artifactType") not in _ARTIFACT_TYPES
        or not _is_canonical_uuid(binding.get("artifactId"))
        or not isinstance(binding.get("artifactBindingDigest"), str)
        or not _DIGEST_PATTERN.fullmatch(cast(str, binding["artifactBindingDigest"]))
    ):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    header["binding"] = dict(binding)
    return header, ciphertext


def _validated_crypto_metadata(
    header: Mapping[str, object],
) -> tuple[bytes | None, bytes]:
    aead = header.get("aead")
    kdf = header.get("kdf")
    if not isinstance(aead, Mapping) or not isinstance(kdf, Mapping):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    expected_aead_keys = {
        "algorithm",
        "keyBytes",
        "nonce",
        "nonceBytes",
        "nonceEncoding",
        "tagBytes",
    }
    if set(aead) != expected_aead_keys:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(aead.get("nonce"), str):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if any(
        not isinstance(aead.get(name), int) or isinstance(aead.get(name), bool)
        for name in ("keyBytes", "nonceBytes", "tagBytes")
    ):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if {
        "algorithm": aead.get("algorithm"),
        "keyBytes": aead.get("keyBytes"),
        "nonceBytes": aead.get("nonceBytes"),
        "nonceEncoding": aead.get("nonceEncoding"),
        "tagBytes": aead.get("tagBytes"),
    } != {
        "algorithm": "AES-256-GCM",
        "keyBytes": KEY_BYTES,
        "nonceBytes": NONCE_BYTES,
        "nonceEncoding": "base64url",
        "tagBytes": TAG_BYTES,
    }:
        _authentication_failed()
    nonce = _b64url_decode(cast(str, aead["nonce"]), maximum=NONCE_BYTES)
    if len(nonce) != NONCE_BYTES:
        _authentication_failed()

    if header["mode"] == "one-time-key":
        if dict(kdf) != {"algorithm": "none"}:
            _authentication_failed()
        return None, nonce

    expected_kdf_keys = {
        "algorithm",
        "iterations",
        "memoryKiB",
        "outputBytes",
        "parallelism",
        "salt",
        "saltEncoding",
    }
    if set(kdf) != expected_kdf_keys:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(kdf.get("salt"), str):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if any(
        not isinstance(kdf.get(name), int) or isinstance(kdf.get(name), bool)
        for name in ("iterations", "memoryKiB", "outputBytes", "parallelism")
    ):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if {
        "algorithm": kdf.get("algorithm"),
        "iterations": kdf.get("iterations"),
        "memoryKiB": kdf.get("memoryKiB"),
        "outputBytes": kdf.get("outputBytes"),
        "parallelism": kdf.get("parallelism"),
        "saltEncoding": kdf.get("saltEncoding"),
    } != {
        "algorithm": "Argon2id",
        "iterations": ARGON2_ITERATIONS,
        "memoryKiB": ARGON2_MEMORY_KIB,
        "outputBytes": KEY_BYTES,
        "parallelism": ARGON2_PARALLELISM,
        "saltEncoding": "base64url",
    }:
        _authentication_failed()
    salt = _b64url_decode(cast(str, kdf["salt"]), maximum=SALT_BYTES)
    if len(salt) != SALT_BYTES:
        _authentication_failed()
    return salt, nonce


def _parse_inner_payload(plaintext: bytes) -> Mapping[str, object]:
    parsed = _strict_json_loads(plaintext, "ENVELOPE_STRUCTURE_CORRUPT")
    if not isinstance(parsed, Mapping):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    return cast(Mapping[str, object], parsed)


def _validate_inner_payload(
    payload: Mapping[str, object], header: Mapping[str, object]
) -> OpenedSecretPayload:
    required = {
        "artifactBinding",
        "credentialEncryptionKeyRequired",
        "payloadSchemaVersion",
        "secretEntries",
        "sourceInstanceId",
    }
    if set(payload) != required:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    version = payload["payloadSchemaVersion"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    if version != 1:
        if version > 0:
            raise SecretEnvelopeUnsupportedError("ENVELOPE_VERSION_UNSUPPORTED")
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    source_instance_id = payload["sourceInstanceId"]
    required_cek = payload["credentialEncryptionKeyRequired"]
    binding = payload["artifactBinding"]
    raw_entries = payload["secretEntries"]
    if (
        not _is_canonical_uuid(source_instance_id)
        or not isinstance(required_cek, bool)
        or not isinstance(binding, Mapping)
        or not isinstance(raw_entries, list)
        or len(raw_entries) > MAX_SECRET_ENTRIES
    ):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    outer_binding = cast(Mapping[str, object], header["binding"])
    if dict(binding) != dict(outer_binding):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")

    entries: list[SecretEntry] = []
    seen: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
        name = raw_entry.get("name")
        classification = raw_entry.get("classification")
        handling = raw_entry.get("handling")
        if (
            not isinstance(name, str)
            or name not in _SECRET_POLICY
            or classification != _SECRET_POLICY[name]
            or handling
            not in (
                ("PRESERVE",)
                if classification == "PRESERVE"
                else ("PRESERVE", "RECONFIGURE")
            )
            or name in seen
        ):
            raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
        seen.add(name)
        if handling == "PRESERVE":
            if set(raw_entry) != {
                "classification",
                "handling",
                "name",
                "value",
                "valueEncoding",
            }:
                raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
            value = raw_entry.get("value")
            if raw_entry.get("valueEncoding") != "base64url" or not isinstance(
                value, str
            ):
                raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
            try:
                material = _b64url_decode(value, maximum=MAX_SECRET_BYTES)
            except SecretEnvelopeCorruptError:
                raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT") from None
            if not material:
                raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
            entries.append(SecretEntry.preserve(name, material))
        else:
            if set(raw_entry) != {"classification", "handling", "name"}:
                raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
            entries.append(SecretEntry.reconfigure(name))
    if required_cek and "CREDENTIAL_ENCRYPTION_KEY" not in seen:
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    return OpenedSecretPayload(
        artifact_type=cast(ArtifactType, outer_binding["artifactType"]),
        artifact_id=cast(str, outer_binding["artifactId"]),
        artifact_binding_digest=cast(str, outer_binding["artifactBindingDigest"]),
        source_instance_id=cast(str, source_instance_id),
        credential_encryption_key_required=required_cek,
        entries=tuple(entries),
    )


def _strict_json_loads(value: bytes, error_code: str) -> object:
    def no_constant(_: str) -> object:
        raise ValueError

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, item in pairs:
            if name in result:
                raise ValueError
            result[name] = item
        return result

    try:
        text = value.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=no_constant,
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise SecretEnvelopeCorruptError(error_code) from None


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, maximum: int) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) > ((maximum + 2) // 3) * 4
        or not _BASE64URL_PATTERN.fullmatch(value)
        or len(value) % 4 == 1
    ):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        raise SecretEnvelopeCorruptError("ENVELOPE_STRUCTURE_CORRUPT") from None


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _authentication_failed() -> None:
    raise SecretEnvelopeCorruptError("ENVELOPE_AUTHENTICATION_FAILED") from None
