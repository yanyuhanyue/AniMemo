from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .canonical import canonical_json_bytes, sha256_identity
from .compatibility import (
    CompatibilityOutcome,
    Dimension,
    DimensionAssessment,
    ReasonCode,
)

PLATFORM_QUALIFICATION_SCHEMA = "animemo.platform-qualification/v1"
STANDARD_PLATFORM_PROFILE = "v1.1-standard-linux-amd64"

REQUIRED_CAPABILITIES = (
    "compose_profiles",
    "compose_v2",
    "compose_wait",
    "directory_fsync",
    "docker_daemon",
    "file_fsync",
    "immutable_image_digest",
    "loopback_port_binding",
    "nofollow_regular_file",
    "posix_owner_mode",
    "postgres_plain_dump",
    "postgres_psql_restore",
    "same_directory_atomic_replace",
    "single_link_file",
    "systemd_unit_lifecycle",
    "unix_socket_permissions",
)
REQUIRED_REHEARSALS = (
    "doctor_complete",
    "fresh_install",
    "logical_migration",
    "logical_restore",
    "updater_handoff",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "profile",
        "candidateSha",
        "workflow",
        "run",
        "observedAt",
        "host",
        "databasePath",
        "imageDigests",
        "capabilities",
        "rehearsals",
        "evidenceDigest",
    }
)
_UNSIGNED_FIELDS = _TOP_LEVEL_FIELDS - {"evidenceDigest"}
_WORKFLOW_FIELDS = frozenset({"path", "ref", "sha"})
_RUN_FIELDS = frozenset({"id", "attempt"})
_HOST_FIELDS = frozenset(
    {
        "os",
        "architecture",
        "distributionId",
        "distributionVersion",
        "kernel",
        "systemdVersion",
        "dockerVersion",
        "composeVersion",
    }
)
_DATABASE_FIELDS = frozenset(
    {
        "dumpFormat",
        "sourceServerMajor",
        "pgDumpMajor",
        "psqlMajor",
        "targetServerMajor",
    }
)
_IMAGE_FIELDS = frozenset({"postgres", "redis"})
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class PlatformQualificationError(ValueError):
    """No compatibility decision exists when qualification evidence is invalid."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DatabasePathEvidence:
    dump_format: str
    source_server_major: int
    pg_dump_major: int
    psql_major: int
    target_server_major: int

    def as_dict(self) -> dict[str, object]:
        return {
            "dumpFormat": self.dump_format,
            "sourceServerMajor": self.source_server_major,
            "pgDumpMajor": self.pg_dump_major,
            "psqlMajor": self.psql_major,
            "targetServerMajor": self.target_server_major,
        }


@dataclass(frozen=True)
class PlatformQualification:
    candidate_sha: str
    workflow: Mapping[str, object]
    run: Mapping[str, object]
    observed_at: str
    host: Mapping[str, object]
    database_path: DatabasePathEvidence
    image_digests: Mapping[str, str]
    capabilities: Mapping[str, bool]
    rehearsals: Mapping[str, str]
    evidence_digest: str

    @property
    def schema(self) -> str:
        return PLATFORM_QUALIFICATION_SCHEMA

    @property
    def profile(self) -> str:
        return STANDARD_PLATFORM_PROFILE

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile": self.profile,
            "candidateSha": self.candidate_sha,
            "workflow": dict(self.workflow),
            "run": dict(self.run),
            "observedAt": self.observed_at,
            "host": dict(self.host),
            "databasePath": self.database_path.as_dict(),
            "imageDigests": dict(self.image_digests),
            "capabilities": dict(self.capabilities),
            "rehearsals": dict(self.rehearsals),
            "evidenceDigest": self.evidence_digest,
        }


@dataclass(frozen=True)
class HostCapabilityEvidence:
    os: str
    architecture: str
    profile: str
    capabilities: Mapping[str, bool]
    database_path: DatabasePathEvidence


def _fail(code: str) -> None:
    raise PlatformQualificationError(code)


def _exact(value: object, fields: frozenset[str], code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail(code)
    return value


def _text(value: object, code: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        _fail(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(code)
    return value


def _database_path(value: object) -> DatabasePathEvidence:
    item = _exact(value, _DATABASE_FIELDS, "PLATFORM_DATABASE_PATH_INVALID")
    if item["dumpFormat"] != "plain":
        _fail("PLATFORM_DATABASE_PATH_INVALID")
    return DatabasePathEvidence(
        dump_format="plain",
        source_server_major=_positive_int(
            item["sourceServerMajor"], "PLATFORM_DATABASE_PATH_INVALID"
        ),
        pg_dump_major=_positive_int(
            item["pgDumpMajor"], "PLATFORM_DATABASE_PATH_INVALID"
        ),
        psql_major=_positive_int(item["psqlMajor"], "PLATFORM_DATABASE_PATH_INVALID"),
        target_server_major=_positive_int(
            item["targetServerMajor"], "PLATFORM_DATABASE_PATH_INVALID"
        ),
    )


def _normalize_payload(payload: Mapping[str, object]) -> dict[str, object]:
    fields = frozenset(payload)
    if fields not in {_TOP_LEVEL_FIELDS, _UNSIGNED_FIELDS}:
        _fail("PLATFORM_SCHEMA_INVALID")
    if payload.get("schema") != PLATFORM_QUALIFICATION_SCHEMA:
        _fail("PLATFORM_SCHEMA_UNSUPPORTED")
    if payload.get("profile") != STANDARD_PLATFORM_PROFILE:
        _fail("PLATFORM_PROFILE_UNSUPPORTED")
    candidate = _text(payload.get("candidateSha"), "PLATFORM_IDENTITY_INVALID")
    if not _SHA.fullmatch(candidate):
        _fail("PLATFORM_IDENTITY_INVALID")

    workflow = dict(
        _exact(payload.get("workflow"), _WORKFLOW_FIELDS, "PLATFORM_WORKFLOW_INVALID")
    )
    if (
        not _text(workflow["path"], "PLATFORM_WORKFLOW_INVALID").startswith(
            ".github/workflows/"
        )
        or not _text(workflow["ref"], "PLATFORM_WORKFLOW_INVALID")
        or not _SHA.fullmatch(_text(workflow["sha"], "PLATFORM_WORKFLOW_INVALID"))
    ):
        _fail("PLATFORM_WORKFLOW_INVALID")
    if workflow["sha"] != candidate:
        _fail("PLATFORM_WORKFLOW_INVALID")

    run = dict(_exact(payload.get("run"), _RUN_FIELDS, "PLATFORM_RUN_INVALID"))
    run["id"] = _text(run["id"], "PLATFORM_RUN_INVALID", maximum=32)
    if not str(run["id"]).isdigit() or str(run["id"]).startswith("0"):
        _fail("PLATFORM_RUN_INVALID")
    run["attempt"] = _positive_int(run["attempt"], "PLATFORM_RUN_INVALID")
    observed_at = _text(payload.get("observedAt"), "PLATFORM_TIME_INVALID")
    if not _TIMESTAMP.fullmatch(observed_at):
        _fail("PLATFORM_TIME_INVALID")

    host = dict(_exact(payload.get("host"), _HOST_FIELDS, "PLATFORM_HOST_INVALID"))
    for key in _HOST_FIELDS:
        host[key] = _text(host[key], "PLATFORM_HOST_INVALID")
    if host["os"] != "linux" or host["architecture"] != "amd64":
        _fail("PLATFORM_HOST_UNSUPPORTED")

    database_path = _database_path(payload.get("databasePath"))
    images = dict(
        _exact(payload.get("imageDigests"), _IMAGE_FIELDS, "PLATFORM_IMAGES_INVALID")
    )
    for key in _IMAGE_FIELDS:
        image = _text(images[key], "PLATFORM_IMAGES_INVALID")
        if not _IMAGE.fullmatch(image):
            _fail("PLATFORM_IMAGES_INVALID")
        images[key] = image

    capabilities_raw = payload.get("capabilities")
    if (
        not isinstance(capabilities_raw, Mapping)
        or tuple(sorted(capabilities_raw)) != REQUIRED_CAPABILITIES
    ):
        _fail("PLATFORM_CAPABILITIES_INVALID")
    capabilities = {key: capabilities_raw[key] for key in REQUIRED_CAPABILITIES}
    if any(value is not True for value in capabilities.values()):
        _fail("PLATFORM_CAPABILITY_NOT_QUALIFIED")

    rehearsals_raw = payload.get("rehearsals")
    if (
        not isinstance(rehearsals_raw, Mapping)
        or tuple(sorted(rehearsals_raw)) != REQUIRED_REHEARSALS
    ):
        _fail("PLATFORM_REHEARSALS_INVALID")
    rehearsals = {key: rehearsals_raw[key] for key in REQUIRED_REHEARSALS}
    if any(value != "PASS" for value in rehearsals.values()):
        _fail("PLATFORM_REHEARSAL_NOT_QUALIFIED")

    normalized: dict[str, object] = {
        "schema": PLATFORM_QUALIFICATION_SCHEMA,
        "profile": STANDARD_PLATFORM_PROFILE,
        "candidateSha": candidate,
        "workflow": workflow,
        "run": run,
        "observedAt": observed_at,
        "host": host,
        "databasePath": database_path.as_dict(),
        "imageDigests": images,
        "capabilities": capabilities,
        "rehearsals": rehearsals,
    }
    if "evidenceDigest" in payload:
        digest = payload["evidenceDigest"]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            _fail("PLATFORM_DIGEST_INVALID")
        if digest != sha256_identity(canonical_json_bytes(normalized)):
            _fail("PLATFORM_DIGEST_MISMATCH")
        normalized["evidenceDigest"] = digest
    return normalized


def finalize_platform_qualification(
    payload: Mapping[str, object],
) -> PlatformQualification:
    unsigned = copy.deepcopy(dict(payload))
    unsigned.pop("evidenceDigest", None)
    normalized = _normalize_payload(unsigned)
    normalized["evidenceDigest"] = sha256_identity(canonical_json_bytes(normalized))
    return _qualification_from_payload(_normalize_payload(normalized))


def _qualification_from_payload(payload: Mapping[str, object]) -> PlatformQualification:
    return PlatformQualification(
        candidate_sha=str(payload["candidateSha"]),
        workflow=MappingProxyType(dict(payload["workflow"])),  # type: ignore[arg-type]
        run=MappingProxyType(dict(payload["run"])),  # type: ignore[arg-type]
        observed_at=str(payload["observedAt"]),
        host=MappingProxyType(dict(payload["host"])),  # type: ignore[arg-type]
        database_path=_database_path(payload["databasePath"]),
        image_digests=MappingProxyType(dict(payload["imageDigests"])),  # type: ignore[arg-type]
        capabilities=MappingProxyType(dict(payload["capabilities"])),  # type: ignore[arg-type]
        rehearsals=MappingProxyType(dict(payload["rehearsals"])),  # type: ignore[arg-type]
        evidence_digest=str(payload["evidenceDigest"]),
    )


def parse_platform_qualification(raw: bytes) -> PlatformQualification:
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
        _fail("PLATFORM_EVIDENCE_SIZE_INVALID")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _fail("PLATFORM_EVIDENCE_INVALID")
    if not isinstance(payload, Mapping):
        _fail("PLATFORM_SCHEMA_INVALID")
    normalized = _normalize_payload(payload)
    if "evidenceDigest" not in normalized:
        _fail("PLATFORM_DIGEST_INVALID")
    return _qualification_from_payload(normalized)


def canonical_platform_qualification_bytes(
    qualification: PlatformQualification,
) -> bytes:
    normalized = _normalize_payload(qualification.as_dict())
    return canonical_json_bytes(normalized) + b"\n"


def assess_platform(
    host: HostCapabilityEvidence,
    qualification: PlatformQualification,
) -> DimensionAssessment:
    if not isinstance(host, HostCapabilityEvidence):
        _fail("PLATFORM_HOST_EVIDENCE_INVALID")
    capabilities = dict(host.capabilities)
    if set(capabilities) != set(REQUIRED_CAPABILITIES) or any(
        not isinstance(value, bool) for value in capabilities.values()
    ):
        _fail("PLATFORM_HOST_EVIDENCE_INVALID")
    supported = (
        host.os == "linux"
        and host.architecture == "amd64"
        and host.profile == STANDARD_PLATFORM_PROFILE
        and all(capabilities.values())
        and host.database_path == qualification.database_path
    )
    return DimensionAssessment(
        name=Dimension.PLATFORM_RUNTIME,
        outcome=(
            CompatibilityOutcome.COMPATIBLE
            if supported
            else CompatibilityOutcome.UNSUPPORTED
        ),
        reason_code=(
            ReasonCode.PLATFORM_RUNTIME_SUPPORTED
            if supported
            else ReasonCode.PLATFORM_RUNTIME_UNSUPPORTED
        ),
        source={
            "profile": qualification.profile,
            "qualificationDigest": qualification.evidence_digest,
            "databasePath": qualification.database_path.as_dict(),
        },
        target={
            "profile": host.profile,
            "os": host.os,
            "architecture": host.architecture,
            "capabilities": sorted(key for key, value in capabilities.items() if value),
            "databasePath": host.database_path.as_dict(),
        },
    )


def read_platform_qualification(path: Path) -> PlatformQualification:
    try:
        return parse_platform_qualification(path.read_bytes())
    except OSError as error:
        raise PlatformQualificationError("PLATFORM_EVIDENCE_UNREADABLE") from error


__all__ = [
    "PLATFORM_QUALIFICATION_SCHEMA",
    "REQUIRED_CAPABILITIES",
    "REQUIRED_REHEARSALS",
    "STANDARD_PLATFORM_PROFILE",
    "DatabasePathEvidence",
    "HostCapabilityEvidence",
    "PlatformQualification",
    "PlatformQualificationError",
    "assess_platform",
    "canonical_platform_qualification_bytes",
    "finalize_platform_qualification",
    "parse_platform_qualification",
    "read_platform_qualification",
]
