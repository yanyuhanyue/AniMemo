from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.contract import validate_manifest

from .errors import RequestRejected, StateError
from .local_bundle import LocalBundleTransportPolicy
from .state import (
    _absolute,
    _atomic_json,
    _read_private_text,
    _validate_private_directory,
)
from .transport import ExplicitTransportPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _manifest_hash(manifest: dict[str, object]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _canonical_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _manifest_identity(manifest: dict[str, object]) -> str:
    return "sha256:" + _manifest_hash(manifest)


def _validate_release_binding(
    value: object,
    *,
    manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StateError("Update plan release binding is invalid")
    source = value.get("source")
    if source == "github":
        policy = ExplicitTransportPolicy.github()
    elif source == "official-mirror":
        policy = ExplicitTransportPolicy.official_mirror()
    elif source == "local-bundle":
        policy = LocalBundleTransportPolicy()
    else:
        raise StateError("Update plan transport source is invalid")
    common_fields = {
        "source",
        "transportPolicyIdentity",
        "verifiedReleaseIdentity",
    }
    local_fields = common_fields | {
        "transportIdentity",
        "payloadIdentity",
        "releaseAttestationIdentity",
        "releaseExecutionReceipt",
        "trustProfileVersion",
        "trustProfileIdentity",
        "manifestIdentity",
        "deploymentContractIdentity",
        "apiDigest",
        "webDigest",
        "postgresDigest",
        "redisDigest",
    }
    expected_fields = local_fields if source == "local-bundle" else common_fields
    if set(value) != expected_fields:
        raise StateError("Update plan release binding is invalid")
    verified_identity = value.get("verifiedReleaseIdentity")
    policy_identity = value.get("transportPolicyIdentity")
    if not _digest(verified_identity) or policy_identity != policy.identity:
        raise StateError("Update plan release binding is invalid")
    if source == "local-bundle":
        digest_fields = local_fields - common_fields - {
            "trustProfileVersion",
            "releaseExecutionReceipt",
        }
        if any(not _digest(value.get(field)) for field in digest_fields):
            raise StateError("Update plan local bundle authority binding is invalid")
        profile_version = value.get("trustProfileVersion")
        if type(profile_version) is not int or profile_version < 1:
            raise StateError("Update plan local bundle trust profile is invalid")
        execution = value.get("releaseExecutionReceipt")
        if (
            not isinstance(execution, dict)
            or set(execution)
            != {
                "schema",
                "publicationIdentity",
                "publicationExecutionReceiptIdentity",
                "signedClaimIdentity",
                "signedAt",
                "identity",
            }
            or execution.get("schema") != "animemo.release-execution-receipt/v1"
            or any(
                not _digest(execution.get(field))
                for field in (
                    "publicationIdentity",
                    "publicationExecutionReceiptIdentity",
                    "signedClaimIdentity",
                    "identity",
                )
            )
            or not _canonical_timestamp(execution.get("signedAt"))
        ):
            raise StateError("Update plan release execution receipt is invalid")
        unsigned_execution = dict(execution)
        execution_identity = unsigned_execution.pop("identity")
        if execution_identity != "sha256:" + hashlib.sha256(
            json.dumps(
                unsigned_execution,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest():
            raise StateError("Update plan release execution receipt is invalid")
        if manifest is not None:
            try:
                images = manifest["images"]
                deployment = manifest["deployment"]
                expected = {
                    "manifestIdentity": _manifest_identity(manifest),
                    "deploymentContractIdentity": deployment["contractSha256"],
                    "apiDigest": images["api"]["digest"],
                    "webDigest": images["web"]["digest"],
                    "postgresDigest": images["postgres"]["digest"],
                    "redisDigest": images["redis"]["digest"],
                }
            except (KeyError, TypeError) as error:
                raise StateError("Update plan local bundle manifest binding is invalid") from error
            if any(value.get(field) != expected_value for field, expected_value in expected.items()):
                raise StateError("Update plan local bundle manifest binding is invalid")
    return dict(value)


class PlanStore:
    def __init__(self, root: Path, *, ttl_seconds: int = 900):
        self.state_root = _absolute(root)
        self.root = self.state_root / "plans"
        self.ttl_seconds = ttl_seconds

    def _path(self, plan_id: str) -> Path:
        if len(plan_id) != 32 or any(character not in "0123456789abcdef" for character in plan_id):
            raise RequestRejected("Invalid update plan id")
        return self.root / f"{plan_id}.json"

    def create(
        self,
        manifest: dict[str, object],
        plan: dict[str, object],
        *,
        release_binding: dict[str, object],
        planning_context_identity: str,
    ) -> dict[str, object]:
        validate_manifest(manifest)
        binding = _validate_release_binding(release_binding, manifest=manifest)
        if not _digest(planning_context_identity):
            raise StateError("Update plan instance context identity is invalid")
        created = _now()
        payload = {
            "schemaVersion": 3,
            "id": secrets.token_hex(16),
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "expiresAt": (created + timedelta(seconds=self.ttl_seconds)).isoformat().replace("+00:00", "Z"),
            "consumedAt": None,
            "manifestHash": _manifest_hash(manifest),
            "manifest": manifest,
            "plan": plan,
            "releaseBinding": binding,
            "planningContextIdentity": planning_context_identity,
        }
        _atomic_json(self._path(payload["id"]), payload, root=self.state_root)
        return payload
    def get(self, plan_id: str) -> dict[str, object]:
        _validate_private_directory(self.state_root, self.root)
        try:
            payload = json.loads(_read_private_text(self.state_root, self._path(plan_id)))
            validate_manifest(payload["manifest"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RequestRejected("Update plan is unavailable") from error
        if not isinstance(payload, dict) or set(payload) != {
            "schemaVersion",
            "id",
            "createdAt",
            "expiresAt",
            "consumedAt",
            "manifestHash",
            "manifest",
            "plan",
            "releaseBinding",
            "planningContextIdentity",
        }:
            raise StateError("Update plan fields are not closed")
        if payload.get("id") != plan_id or payload.get("manifestHash") != _manifest_hash(payload["manifest"]):
            raise StateError("Update plan manifest binding is invalid")
        if payload.get("schemaVersion") != 3:
            raise StateError(
                "Update plan schema is unsupported; explicit migration is required"
            )
        _validate_release_binding(
            payload.get("releaseBinding"),
            manifest=payload["manifest"],
        )
        if not _digest(payload.get("planningContextIdentity")):
            raise StateError("Update plan instance context identity is invalid")
        expires = datetime.fromisoformat(str(payload["expiresAt"]).replace("Z", "+00:00"))
        if expires <= _now():
            raise RequestRejected("Update plan has expired")
        return payload

    def consume(self, plan_id: str) -> dict[str, object]:
        payload = self.get(plan_id)
        if payload.get("consumedAt"):
            raise RequestRejected("Update plan has already been consumed")
        payload["consumedAt"] = _now().isoformat().replace("+00:00", "Z")
        _atomic_json(self._path(plan_id), payload, root=self.state_root)
        return payload
