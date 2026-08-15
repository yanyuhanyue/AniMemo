"""Canonical Compatibility Matrix v1 decision engine.

The engine deliberately does not inspect artifacts, hosts, releases, or secrets.
Those bounded verifiers produce seven ordered dimension assessments; this module
validates and aggregates those facts into the one cross-runtime decision shape.
Operational collection failures must be raised before calling this evaluator (or
as :class:`CompatibilityEvaluationError`) and never become a fifth outcome.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, NoReturn

from durability.canonical import canonical_json_bytes, sha256_identity

MATRIX_IDENTITY = "animemo.compatibility/v1"
MATRIX_FORMAT_IDENTITY = "animemo.compatibility"
MATRIX_FORMAT_VERSION = 1


class CompatibilityOutcome(str, Enum):
    """The complete and exclusive public Compatibility Matrix outcomes."""

    COMPATIBLE = "COMPATIBLE"
    REQUIRES_UPGRADE = "REQUIRES_UPGRADE"
    UNSUPPORTED = "UNSUPPORTED"
    CORRUPT = "CORRUPT"


class CompatibilityOperation(str, Enum):
    INSTALL = "install"
    UPDATE = "update"
    BACKUP = "backup"
    RESTORE = "restore"
    MIGRATION = "migration"
    DOCTOR = "doctor"


class Dimension(str, Enum):
    FORMAT = "format"
    INTEGRITY_AUTHENTICATION = "integrityAuthentication"
    DEPLOYMENT_CONTRACT = "deploymentContract"
    SCHEMA_CONTRACTS = "schemaContracts"
    EXACT_RELEASE_IDENTITY = "exactReleaseIdentity"
    PLATFORM_RUNTIME = "platformRuntime"
    SUPPORTED_PATH = "supportedPath"


EVALUATION_ORDER = (
    Dimension.FORMAT,
    Dimension.INTEGRITY_AUTHENTICATION,
    Dimension.DEPLOYMENT_CONTRACT,
    Dimension.SCHEMA_CONTRACTS,
    Dimension.EXACT_RELEASE_IDENTITY,
    Dimension.PLATFORM_RUNTIME,
    Dimension.SUPPORTED_PATH,
)


class ReasonCode(str, Enum):
    """Versioned machine reasons accepted by the v1 evaluator."""

    ALL_DIMENSIONS_COMPATIBLE = "ALL_DIMENSIONS_COMPATIBLE"

    FORMAT_SUPPORTED = "FORMAT_SUPPORTED"
    FORMAT_CONVERSION_REQUIRED = "FORMAT_CONVERSION_REQUIRED"
    FORMAT_IDENTITY_UNSUPPORTED = "FORMAT_IDENTITY_UNSUPPORTED"
    FORMAT_VERSION_UNSUPPORTED = "FORMAT_VERSION_UNSUPPORTED"
    FORMAT_BOUNDS_EXCEEDED = "FORMAT_BOUNDS_EXCEEDED"
    FORMAT_STRUCTURE_CORRUPT = "FORMAT_STRUCTURE_CORRUPT"

    INTEGRITY_AUTHENTICATED = "INTEGRITY_AUTHENTICATED"
    INTEGRITY_SCHEME_UNSUPPORTED = "INTEGRITY_SCHEME_UNSUPPORTED"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    CROSS_MEMBER_BINDING_FAILED = "CROSS_MEMBER_BINDING_FAILED"

    ENVELOPE_COMPATIBLE = "ENVELOPE_COMPATIBLE"
    ENVELOPE_UPGRADE_REQUIRED = "ENVELOPE_UPGRADE_REQUIRED"
    ENVELOPE_VERSION_UNSUPPORTED = "ENVELOPE_VERSION_UNSUPPORTED"
    ENVELOPE_SUITE_UNSUPPORTED = "ENVELOPE_SUITE_UNSUPPORTED"
    ENVELOPE_AUTHENTICATION_FAILED = "ENVELOPE_AUTHENTICATION_FAILED"
    ENVELOPE_STRUCTURE_CORRUPT = "ENVELOPE_STRUCTURE_CORRUPT"

    DEPLOYMENT_CONTRACT_SUPPORTED = "DEPLOYMENT_CONTRACT_SUPPORTED"
    DEPLOYMENT_CUTOVER_REQUIRED = "DEPLOYMENT_CUTOVER_REQUIRED"
    DEPLOYMENT_CONTRACT_UNSUPPORTED = "DEPLOYMENT_CONTRACT_UNSUPPORTED"
    DEPLOYMENT_CONTRACT_INVALID = "DEPLOYMENT_CONTRACT_INVALID"

    SCHEMA_CONTRACTS_SUPPORTED = "SCHEMA_CONTRACTS_SUPPORTED"
    SCHEMA_MIGRATION_REQUIRED = "SCHEMA_MIGRATION_REQUIRED"
    SCHEMA_CONTRACT_UNSUPPORTED = "SCHEMA_CONTRACT_UNSUPPORTED"
    SCHEMA_CONTRACT_INVALID = "SCHEMA_CONTRACT_INVALID"

    RELEASE_IDENTITY_VERIFIED = "RELEASE_IDENTITY_VERIFIED"
    RELEASE_HOP_REQUIRED = "RELEASE_HOP_REQUIRED"
    RELEASE_IDENTITY_UNSUPPORTED = "RELEASE_IDENTITY_UNSUPPORTED"
    RELEASE_IDENTITY_INVALID = "RELEASE_IDENTITY_INVALID"

    PLATFORM_RUNTIME_SUPPORTED = "PLATFORM_RUNTIME_SUPPORTED"
    PLATFORM_RUNTIME_UPGRADE_REQUIRED = "PLATFORM_RUNTIME_UPGRADE_REQUIRED"
    PLATFORM_RUNTIME_UNSUPPORTED = "PLATFORM_RUNTIME_UNSUPPORTED"
    PLATFORM_RUNTIME_EVIDENCE_INVALID = "PLATFORM_RUNTIME_EVIDENCE_INVALID"

    DIRECT_PATH_SUPPORTED = "DIRECT_PATH_SUPPORTED"
    ORDERED_PATH_REQUIRED = "ORDERED_PATH_REQUIRED"
    SUPPORTED_PATH_UNAVAILABLE = "SUPPORTED_PATH_UNAVAILABLE"
    SUPPORTED_PATH_INVALID = "SUPPORTED_PATH_INVALID"


class CompatibilityEvaluationError(ValueError):
    """A fail-closed operational/input error that contains no decision."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def as_dict(self) -> dict[str, object]:
        return {
            "matrixVersion": MATRIX_IDENTITY,
            "error": {"code": self.code},
        }


@dataclass(frozen=True)
class ArtifactIdentity:
    format_identity: str
    format_version: int
    artifact_id: str
    manifest_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.format_identity,
            "schemaVersion": self.format_version,
            "artifactId": self.artifact_id,
            "manifestDigest": self.manifest_digest,
        }


@dataclass(frozen=True)
class DimensionAssessment:
    name: Dimension
    outcome: CompatibilityOutcome
    reason_code: ReasonCode
    source: Mapping[str, object]
    target: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name.value,
            "source": _thaw_json(self.source),
            "target": _thaw_json(self.target),
            "status": self.outcome.value,
            "reasonCode": self.reason_code.value,
        }


@dataclass(frozen=True)
class UpgradeAction:
    order: int
    kind: str
    input_identity: Mapping[str, object]
    output_identity: Mapping[str, object]
    required_release_identity: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "kind": self.kind,
            "inputIdentity": _thaw_json(self.input_identity),
            "outputIdentity": _thaw_json(self.output_identity),
            "requiredReleaseIdentity": _thaw_json(self.required_release_identity),
        }


@dataclass(frozen=True)
class CompatibilityDecision:
    operation: CompatibilityOperation
    outcome: CompatibilityOutcome
    reason_code: ReasonCode
    summary: str
    blocking_dimension: Dimension | None
    artifact: ArtifactIdentity
    evaluated_dimensions: tuple[DimensionAssessment, ...]
    actions: tuple[UpgradeAction, ...]

    @property
    def matrix_identity(self) -> str:
        return MATRIX_IDENTITY

    @property
    def format_identity(self) -> str:
        return MATRIX_FORMAT_IDENTITY

    @property
    def format_version(self) -> int:
        return MATRIX_FORMAT_VERSION

    def as_dict(self) -> dict[str, object]:
        artifact_identity = self.artifact.as_dict()
        return {
            "matrixVersion": MATRIX_IDENTITY,
            "formatIdentity": MATRIX_FORMAT_IDENTITY,
            "formatVersion": MATRIX_FORMAT_VERSION,
            "operation": self.operation.value,
            "overallStatus": self.outcome.value,
            "reasonCode": self.reason_code.value,
            "summary": self.summary,
            "blockingDimension": (
                self.blocking_dimension.value
                if self.blocking_dimension is not None
                else None
            ),
            "artifact": artifact_identity,
            "dimensions": [
                dimension.as_dict() for dimension in self.evaluated_dimensions
            ],
            "actions": [action.as_dict() for action in self.actions],
            "evaluatedArtifactIdentity": dict(artifact_identity),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def digest(self) -> str:
        return sha256_identity(self.canonical_bytes())


_REASON_CONTEXT: dict[ReasonCode, tuple[Dimension, CompatibilityOutcome]] = {
    ReasonCode.FORMAT_SUPPORTED: (Dimension.FORMAT, CompatibilityOutcome.COMPATIBLE),
    ReasonCode.FORMAT_CONVERSION_REQUIRED: (
        Dimension.FORMAT,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.FORMAT_IDENTITY_UNSUPPORTED: (
        Dimension.FORMAT,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.FORMAT_VERSION_UNSUPPORTED: (
        Dimension.FORMAT,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.FORMAT_BOUNDS_EXCEEDED: (Dimension.FORMAT, CompatibilityOutcome.CORRUPT),
    ReasonCode.FORMAT_STRUCTURE_CORRUPT: (
        Dimension.FORMAT,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.INTEGRITY_AUTHENTICATED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.INTEGRITY_SCHEME_UNSUPPORTED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.CHECKSUM_MISMATCH: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.AUTHENTICATION_FAILED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.CROSS_MEMBER_BINDING_FAILED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.ENVELOPE_COMPATIBLE: (
        Dimension.FORMAT,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.ENVELOPE_UPGRADE_REQUIRED: (
        Dimension.FORMAT,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.ENVELOPE_VERSION_UNSUPPORTED: (
        Dimension.FORMAT,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.ENVELOPE_SUITE_UNSUPPORTED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.ENVELOPE_AUTHENTICATION_FAILED: (
        Dimension.INTEGRITY_AUTHENTICATION,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.ENVELOPE_STRUCTURE_CORRUPT: (
        Dimension.FORMAT,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.DEPLOYMENT_CONTRACT_SUPPORTED: (
        Dimension.DEPLOYMENT_CONTRACT,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.DEPLOYMENT_CUTOVER_REQUIRED: (
        Dimension.DEPLOYMENT_CONTRACT,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.DEPLOYMENT_CONTRACT_UNSUPPORTED: (
        Dimension.DEPLOYMENT_CONTRACT,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.DEPLOYMENT_CONTRACT_INVALID: (
        Dimension.DEPLOYMENT_CONTRACT,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.SCHEMA_CONTRACTS_SUPPORTED: (
        Dimension.SCHEMA_CONTRACTS,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.SCHEMA_MIGRATION_REQUIRED: (
        Dimension.SCHEMA_CONTRACTS,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.SCHEMA_CONTRACT_UNSUPPORTED: (
        Dimension.SCHEMA_CONTRACTS,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.SCHEMA_CONTRACT_INVALID: (
        Dimension.SCHEMA_CONTRACTS,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.RELEASE_IDENTITY_VERIFIED: (
        Dimension.EXACT_RELEASE_IDENTITY,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.RELEASE_HOP_REQUIRED: (
        Dimension.EXACT_RELEASE_IDENTITY,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.RELEASE_IDENTITY_UNSUPPORTED: (
        Dimension.EXACT_RELEASE_IDENTITY,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.RELEASE_IDENTITY_INVALID: (
        Dimension.EXACT_RELEASE_IDENTITY,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.PLATFORM_RUNTIME_SUPPORTED: (
        Dimension.PLATFORM_RUNTIME,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.PLATFORM_RUNTIME_UPGRADE_REQUIRED: (
        Dimension.PLATFORM_RUNTIME,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.PLATFORM_RUNTIME_UNSUPPORTED: (
        Dimension.PLATFORM_RUNTIME,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.PLATFORM_RUNTIME_EVIDENCE_INVALID: (
        Dimension.PLATFORM_RUNTIME,
        CompatibilityOutcome.CORRUPT,
    ),
    ReasonCode.DIRECT_PATH_SUPPORTED: (
        Dimension.SUPPORTED_PATH,
        CompatibilityOutcome.COMPATIBLE,
    ),
    ReasonCode.ORDERED_PATH_REQUIRED: (
        Dimension.SUPPORTED_PATH,
        CompatibilityOutcome.REQUIRES_UPGRADE,
    ),
    ReasonCode.SUPPORTED_PATH_UNAVAILABLE: (
        Dimension.SUPPORTED_PATH,
        CompatibilityOutcome.UNSUPPORTED,
    ),
    ReasonCode.SUPPORTED_PATH_INVALID: (
        Dimension.SUPPORTED_PATH,
        CompatibilityOutcome.CORRUPT,
    ),
}

_PRECEDENCE = {
    CompatibilityOutcome.COMPATIBLE: 0,
    CompatibilityOutcome.REQUIRES_UPGRADE: 1,
    CompatibilityOutcome.UNSUPPORTED: 2,
    CompatibilityOutcome.CORRUPT: 3,
}

_SUMMARY = {
    CompatibilityOutcome.COMPATIBLE: "All required compatibility dimensions are compatible.",
    CompatibilityOutcome.REQUIRES_UPGRADE: (
        "A complete, ordered and supported upgrade path is required."
    ),
    CompatibilityOutcome.UNSUPPORTED: (
        "At least one required compatibility dimension is unsupported."
    ),
    CompatibilityOutcome.CORRUPT: "Artifact structure or authenticated integrity is corrupt.",
}

_FORMAT_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTION_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_SENSITIVE_WORDS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "env",
        "environment",
        "fingerprint",
        "key",
        "password",
        "secret",
        "token",
    }
)
_SAFE_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "credentialstatus",
        "externalsecretavailability",
        "secretenvelopeformat",
        "secretmode",
        "secretstatus",
        "secretsuite",
    }
)
_MAX_EVIDENCE_DEPTH = 16
_MAX_EVIDENCE_MEMBERS = 256
_MAX_EVIDENCE_STRING = 4096


def _fail(code: str) -> NoReturn:
    raise CompatibilityEvaluationError(code)


def _key_words(key: str) -> set[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return {part.lower() for part in re.split(r"[^A-Za-z0-9]+", separated) if part}


def _is_sensitive_key(key: str) -> bool:
    compact = "".join(part.lower() for part in re.split(r"[^A-Za-z0-9]+", key) if part)
    if compact in _SAFE_SENSITIVE_METADATA_KEYS:
        return False
    return bool(_key_words(key) & _SENSITIVE_WORDS) or bool(_ENV_NAME_RE.fullmatch(key))


def _is_sensitive_string(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith("bearer ")
        or "-----begin private key-----" in lowered
        or bool(re.search(r"://[^/@\s:]+:[^/@\s]+@", value))
        or bool(re.match(r"^[A-Z][A-Z0-9_]+\s*=", value))
    )


def _normalize_json(
    value: object, *, depth: int = 0, counter: list[int] | None = None
) -> object:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > _MAX_EVIDENCE_DEPTH or counter[0] > _MAX_EVIDENCE_MEMBERS:
        _fail("IDENTITY_EVIDENCE_INVALID")

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_EVIDENCE_STRING:
            _fail("IDENTITY_EVIDENCE_INVALID")
        if _is_sensitive_string(value):
            _fail("SENSITIVE_EVIDENCE_FORBIDDEN")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_EVIDENCE_MEMBERS:
            _fail("IDENTITY_EVIDENCE_INVALID")
        normalized: dict[str, object] = {}
        keys = tuple(value.keys())
        if any(not isinstance(key, str) for key in keys):
            _fail("IDENTITY_EVIDENCE_INVALID")
        for key in sorted(keys):
            if not isinstance(key, str) or not key or len(key) > 128:
                _fail("IDENTITY_EVIDENCE_INVALID")
            if _is_sensitive_key(key):
                _fail("SENSITIVE_EVIDENCE_FORBIDDEN")
            normalized[key] = _normalize_json(
                value[key], depth=depth + 1, counter=counter
            )
        return MappingProxyType(normalized)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_EVIDENCE_MEMBERS:
            _fail("IDENTITY_EVIDENCE_INVALID")
        return tuple(
            _normalize_json(item, depth=depth + 1, counter=counter) for item in value
        )
    _fail("IDENTITY_EVIDENCE_INVALID")


def _normalize_evidence(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("IDENTITY_EVIDENCE_INVALID")
    normalized = _normalize_json(value)
    if not isinstance(normalized, Mapping):  # pragma: no cover - guarded above
        _fail("IDENTITY_EVIDENCE_INVALID")
    return normalized


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_artifact(artifact: object) -> ArtifactIdentity:
    if not isinstance(artifact, ArtifactIdentity):
        _fail("ARTIFACT_IDENTITY_INVALID")
    if not _FORMAT_IDENTITY_RE.fullmatch(artifact.format_identity):
        _fail("ARTIFACT_IDENTITY_INVALID")
    if (
        isinstance(artifact.format_version, bool)
        or not isinstance(artifact.format_version, int)
        or not 1 <= artifact.format_version <= 2_147_483_647
    ):
        _fail("ARTIFACT_IDENTITY_INVALID")
    if (
        not isinstance(artifact.artifact_id, str)
        or not artifact.artifact_id
        or len(artifact.artifact_id) > 512
    ):
        _fail("ARTIFACT_IDENTITY_INVALID")
    if _is_sensitive_string(artifact.artifact_id):
        _fail("SENSITIVE_EVIDENCE_FORBIDDEN")
    if not isinstance(artifact.manifest_digest, str) or not _DIGEST_RE.fullmatch(
        artifact.manifest_digest
    ):
        _fail("ARTIFACT_IDENTITY_INVALID")
    return artifact


def _normalize_dimensions(
    dimensions: Sequence[DimensionAssessment],
) -> tuple[DimensionAssessment, ...]:
    if not isinstance(dimensions, Sequence) or isinstance(
        dimensions, (str, bytes, bytearray)
    ):
        _fail("DIMENSIONS_INVALID")
    if len(dimensions) != len(EVALUATION_ORDER):
        _fail("DIMENSIONS_INVALID")
    supplied = tuple(dimensions)

    normalized: list[DimensionAssessment] = []
    for expected_name, assessment in zip(EVALUATION_ORDER, supplied, strict=True):
        if not isinstance(assessment, DimensionAssessment):
            _fail("DIMENSIONS_INVALID")
        if (
            not isinstance(assessment.name, Dimension)
            or assessment.name is not expected_name
        ):
            _fail("DIMENSIONS_INVALID")
        if not isinstance(assessment.outcome, CompatibilityOutcome):
            _fail("OUTCOME_INVALID")
        if not isinstance(assessment.reason_code, ReasonCode):
            _fail("REASON_CODE_INVALID")
        if _REASON_CONTEXT.get(assessment.reason_code) != (
            assessment.name,
            assessment.outcome,
        ):
            _fail("REASON_CODE_INVALID")
        normalized.append(
            DimensionAssessment(
                name=assessment.name,
                outcome=assessment.outcome,
                reason_code=assessment.reason_code,
                source=_normalize_evidence(assessment.source),
                target=_normalize_evidence(assessment.target),
            )
        )
    return tuple(normalized)


def _normalize_actions(actions: Sequence[UpgradeAction]) -> tuple[UpgradeAction, ...]:
    if not isinstance(actions, Sequence) or isinstance(
        actions, (str, bytes, bytearray)
    ):
        _fail("ACTIONS_INVALID")
    if len(actions) > _MAX_EVIDENCE_MEMBERS:
        _fail("ACTIONS_INVALID")
    supplied = tuple(actions)

    normalized: list[UpgradeAction] = []
    for action in supplied:
        if not isinstance(action, UpgradeAction):
            _fail("ACTIONS_INVALID")
        if isinstance(action.order, bool) or not isinstance(action.order, int):
            _fail("ACTION_ORDER_INVALID")
        if not isinstance(action.kind, str) or not _ACTION_KIND_RE.fullmatch(
            action.kind
        ):
            _fail("ACTION_KIND_INVALID")
        normalized.append(
            UpgradeAction(
                order=action.order,
                kind=action.kind,
                input_identity=_normalize_evidence(action.input_identity),
                output_identity=_normalize_evidence(action.output_identity),
                required_release_identity=_normalize_evidence(
                    action.required_release_identity
                ),
            )
        )

    if [action.order for action in normalized] != list(range(1, len(normalized) + 1)):
        _fail("ACTION_ORDER_INVALID")
    return tuple(normalized)


def _operation(value: CompatibilityOperation | str) -> CompatibilityOperation:
    if isinstance(value, CompatibilityOperation):
        return value
    if isinstance(value, str):
        try:
            return CompatibilityOperation(value)
        except ValueError:
            pass
    _fail("OPERATION_INVALID")


def evaluate_compatibility(
    operation: CompatibilityOperation | str,
    artifact: ArtifactIdentity,
    dimensions: Sequence[DimensionAssessment],
    *,
    actions: Sequence[UpgradeAction] = (),
) -> CompatibilityDecision:
    """Validate seven facts and produce a deterministic Matrix v1 decision.

    The caller cannot provide ``overallStatus``.  Every invalid or incomplete
    evaluation raises a secret-safe ``CompatibilityEvaluationError`` instead of
    returning an UNKNOWN/partial decision.
    """

    normalized_operation = _operation(operation)
    normalized_artifact = _validate_artifact(artifact)
    normalized_dimensions = _normalize_dimensions(dimensions)
    normalized_actions = _normalize_actions(actions)

    highest = max(_PRECEDENCE[item.outcome] for item in normalized_dimensions)
    outcome = next(
        candidate
        for candidate, precedence in _PRECEDENCE.items()
        if precedence == highest
    )
    blocker = next(
        (item for item in normalized_dimensions if item.outcome is outcome),
        None,
    )

    if outcome is CompatibilityOutcome.REQUIRES_UPGRADE:
        if (
            normalized_dimensions[-1].outcome
            is not CompatibilityOutcome.REQUIRES_UPGRADE
        ):
            _fail("SUPPORTED_PATH_REQUIRED")
        if not normalized_actions:
            _fail("UPGRADE_ACTIONS_REQUIRED")
    elif normalized_actions:
        _fail("ACTIONS_FORBIDDEN")

    if outcome is CompatibilityOutcome.COMPATIBLE:
        reason_code = ReasonCode.ALL_DIMENSIONS_COMPATIBLE
        blocking_dimension = None
    else:
        if blocker is None:  # pragma: no cover - aggregation guarantees a blocker
            _fail("DIMENSIONS_INVALID")
        reason_code = blocker.reason_code
        blocking_dimension = blocker.name

    return CompatibilityDecision(
        operation=normalized_operation,
        outcome=outcome,
        reason_code=reason_code,
        summary=_SUMMARY[outcome],
        blocking_dimension=blocking_dimension,
        artifact=normalized_artifact,
        evaluated_dimensions=normalized_dimensions,
        actions=normalized_actions,
    )


__all__ = [
    "EVALUATION_ORDER",
    "MATRIX_FORMAT_IDENTITY",
    "MATRIX_FORMAT_VERSION",
    "MATRIX_IDENTITY",
    "ArtifactIdentity",
    "CompatibilityDecision",
    "CompatibilityEvaluationError",
    "CompatibilityOperation",
    "CompatibilityOutcome",
    "Dimension",
    "DimensionAssessment",
    "ReasonCode",
    "UpgradeAction",
    "evaluate_compatibility",
]
