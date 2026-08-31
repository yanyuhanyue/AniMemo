"""Durable, fail-closed orchestration for immutable release mutations.

The public controller is deliberately independent of any one remote transport.
Production adapters perform Git, registry, GitHub Release, or attestation I/O;
tests cross the same seam with deterministic fakes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .publication import PublicationError, validate_publication_plan
from .publication_input import PUBLISH_CANDIDATE_PLAN_SCHEMA

SCHEMA_PATH = Path(__file__).with_name("publication-transaction-ledger.schema.json")
ATTEMPT_LIMIT = 10
# Revision numbers are inclusive of the initial revision zero.
MAX_LEDGER_REVISIONS = 4097
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_STEP_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}", re.ASCII)
_DIAGNOSTIC = re.compile(r"[A-Z][A-Z0-9_]{0,95}", re.ASCII)
_HEX = re.compile(r"[0-9a-f]{40}", re.ASCII)
_CANDIDATE_PATHS = {
    "candidate_input": "candidate-input.json",
    "verified_candidate": "verified-candidate.json",
    "candidate_acceptance_receipt": "candidate-acceptance-receipt.json",
    "release_manifest": "release-manifest.json",
    "producer_toolchain_receipt": "release-producer-toolchain-receipt.json",
    "checksums": "checksums.txt",
    "deployment_contract": "deployment-contract.json",
    "installer_materials": "installer-materials.tar",
    "candidate_runtime": "candidate-runtime",
}
_CANDIDATE_IMAGE_REPOSITORIES = {
    "api": "ghcr.io/yanyuhanyue/animemo-api",
    "web": "ghcr.io/yanyuhanyue/animemo-web",
}
_KINDS = frozenset(
    {
        "REGISTRY_PUSH",
        "REGISTRY_TAG",
        "ATTESTATION",
        "GIT_TAG",
        "GITHUB_RELEASE_DRAFT",
        "GITHUB_RELEASE_ASSET",
        "GITHUB_RELEASE_PUBLISH",
    }
)


class PublicationTransactionError(RuntimeError):
    """The durable transaction cannot prove that another mutation is safe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ObservationClass(str, Enum):
    ABSENT = "ABSENT"
    SAME = "SAME"
    DIFFERENT = "DIFFERENT"
    UNKNOWN = "UNKNOWN"


class ResponseClass(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    TERMINAL = "TERMINAL"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class RemoteObservation:
    classification: ObservationClass
    identity: str | None = None
    diagnostic_code: str | None = None

    @classmethod
    def absent(cls) -> RemoteObservation:
        return cls(ObservationClass.ABSENT)

    @classmethod
    def same(cls, identity: str) -> RemoteObservation:
        return cls(ObservationClass.SAME, identity)

    @classmethod
    def different(cls, identity: str) -> RemoteObservation:
        return cls(ObservationClass.DIFFERENT, identity)

    @classmethod
    def unknown(cls, code: str = "REMOTE_OBSERVATION_UNKNOWN") -> RemoteObservation:
        return cls(ObservationClass.UNKNOWN, diagnostic_code=code)


@dataclass(frozen=True)
class MutationResponse:
    classification: ResponseClass
    diagnostic_code: str | None = None

    @classmethod
    def acknowledged(cls) -> MutationResponse:
        return cls(ResponseClass.ACKNOWLEDGED)

    @classmethod
    def terminal(cls, code: str = "REMOTE_REQUEST_TERMINAL") -> MutationResponse:
        return cls(ResponseClass.TERMINAL, code)

    @classmethod
    def ambiguous(cls, code: str = "REMOTE_REQUEST_AMBIGUOUS") -> MutationResponse:
        return cls(ResponseClass.AMBIGUOUS, code)


@dataclass(frozen=True)
class MutationIntent:
    name: str
    kind: str
    remote_key: str
    expected_identity: str

    def __post_init__(self) -> None:
        if not _STEP_NAME.fullmatch(self.name):
            raise PublicationTransactionError("TRANSACTION_STEP_NAME_INVALID")
        if self.kind not in _KINDS:
            raise PublicationTransactionError("TRANSACTION_STEP_KIND_INVALID")
        if (
            not isinstance(self.remote_key, str)
            or not self.remote_key
            or len(self.remote_key) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in self.remote_key)
        ):
            raise PublicationTransactionError("TRANSACTION_REMOTE_KEY_INVALID")
        _require_sha256(self.expected_identity, "TRANSACTION_EXPECTED_IDENTITY_INVALID")


class RemoteMutationAdapter(Protocol):
    def observe(self, intent: MutationIntent) -> RemoteObservation: ...

    def mutate(self, intent: MutationIntent) -> MutationResponse: ...


class JournalStore(Protocol):
    def load(self, operation_id: str) -> dict[str, Any] | None: ...

    def append(self, snapshot: Mapping[str, Any]) -> dict[str, Any]: ...


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise PublicationTransactionError("TRANSACTION_CANONICALIZATION_FAILED") from error
    return (encoded + "\n").encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PublicationTransactionError(code)
    return value


def _schema() -> dict[str, Any]:
    try:
        value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationTransactionError("TRANSACTION_SCHEMA_UNAVAILABLE") from error
    if not isinstance(value, dict):
        raise PublicationTransactionError("TRANSACTION_SCHEMA_INVALID")
    return value


def operation_id_for_plan(plan: Mapping[str, Any]) -> str:
    """Bind one durable operation to one public repository/channel/tag key."""

    authority = _normalize_authority_plan(plan, source_tree=None)
    return _identity(
        {
            "schema": "animemo.publication-operation-key/v1",
            "repository": authority["repository"],
            "channel": authority["channel"],
            "tag": authority["tag"],
        }
    )


def _ledger_identity(snapshot: Mapping[str, Any]) -> str:
    unsigned = dict(snapshot)
    unsigned.pop("ledgerIdentity", None)
    return _identity(unsigned)


def _observation_payload(observation: RemoteObservation) -> dict[str, Any]:
    if not isinstance(observation.classification, ObservationClass):
        return {
            "classification": ObservationClass.UNKNOWN.value,
            "identity": None,
            "diagnosticCode": "REMOTE_OBSERVATION_INVALID",
        }
    diagnostic = observation.diagnostic_code
    if observation.classification is ObservationClass.UNKNOWN and diagnostic is None:
        diagnostic = "REMOTE_OBSERVATION_UNKNOWN"
    if diagnostic is not None and not _DIAGNOSTIC.fullmatch(diagnostic):
        return {
            "classification": ObservationClass.UNKNOWN.value,
            "identity": None,
            "diagnosticCode": "REMOTE_DIAGNOSTIC_INVALID",
        }
    return {
        "classification": observation.classification.value,
        "identity": observation.identity,
        "diagnosticCode": diagnostic,
    }


def _response_payload(response: MutationResponse) -> dict[str, Any]:
    if not isinstance(response.classification, ResponseClass):
        return {
            "classification": ResponseClass.AMBIGUOUS.value,
            "diagnosticCode": "REMOTE_RESPONSE_INVALID",
        }
    diagnostic = response.diagnostic_code
    if response.classification is not ResponseClass.ACKNOWLEDGED and diagnostic is None:
        diagnostic = (
            "REMOTE_REQUEST_TERMINAL"
            if response.classification is ResponseClass.TERMINAL
            else "REMOTE_REQUEST_AMBIGUOUS"
        )
    if diagnostic is not None and not _DIAGNOSTIC.fullmatch(diagnostic):
        return {
            "classification": ResponseClass.AMBIGUOUS.value,
            "diagnosticCode": "REMOTE_DIAGNOSTIC_INVALID",
        }
    return {
        "classification": response.classification.value,
        "diagnosticCode": diagnostic,
    }


def _candidate_authority(plan: Mapping[str, Any], source_tree: str | None) -> dict[str, Any]:
    expected_fields = {
        "schema",
        "version",
        "repository",
        "qualification_run_id",
        "qualification_run_attempt",
        "source_sha",
        "source_tree",
        "candidate_version",
        "candidate_input_digest",
        "verified_candidate_digest",
        "candidate_acceptance_receipt_digest",
        "release_manifest_digest",
        "producer_toolchain_receipt_digest",
        "candidate_runtime_inventory_digest",
        "paths",
        "images",
        "publish_rebuild_count",
        "manifest_generation_count",
        "mutation_authorized",
        "plan_digest",
    }
    if not isinstance(plan, Mapping) or set(plan) != expected_fields:
        raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
    value = copy.deepcopy(dict(plan))
    unsigned = dict(value)
    supplied_digest = unsigned.pop("plan_digest", None)
    digest_fields = (
        "candidate_input_digest",
        "verified_candidate_digest",
        "candidate_acceptance_receipt_digest",
        "release_manifest_digest",
        "producer_toolchain_receipt_digest",
        "candidate_runtime_inventory_digest",
    )
    if (
        value["schema"] != PUBLISH_CANDIDATE_PLAN_SCHEMA
        or value["version"] != 1
        or value["repository"] != "yanyuhanyue/AniMemo"
        or isinstance(value["qualification_run_id"], bool)
        or not isinstance(value["qualification_run_id"], int)
        or value["qualification_run_id"] < 1
        or isinstance(value["qualification_run_attempt"], bool)
        or not isinstance(value["qualification_run_attempt"], int)
        or value["qualification_run_attempt"] < 1
        or value["publish_rebuild_count"] != 0
        or value["manifest_generation_count"] != 0
        or value["mutation_authorized"] is not False
        or supplied_digest != _identity(unsigned)
        or any(
            not isinstance(value[field], str) or not _SHA256.fullmatch(value[field])
            for field in digest_fields
        )
        or not isinstance(value["source_sha"], str)
        or not _COMMIT.fullmatch(value["source_sha"])
        or not isinstance(value["source_tree"], str)
        or not _COMMIT.fullmatch(value["source_tree"])
        or (source_tree is not None and source_tree != value["source_tree"])
        or value["paths"] != _CANDIDATE_PATHS
    ):
        raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
    tag = value["candidate_version"]
    if not isinstance(tag, str) or not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+-(?:beta|rc)\.(?:[1-9][0-9]*|TEST)",
        tag,
        re.ASCII,
    ):
        raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
    channel = "beta" if "-beta." in tag else "rc"
    images = value["images"]
    if not isinstance(images, Mapping) or set(images) != {"api", "web"}:
        raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
    digests: dict[str, str] = {}
    for role in ("api", "web"):
        image = images[role]
        if not isinstance(image, Mapping) or set(image) != {
            "digest",
            "layout_path",
            "platform",
            "repository",
        }:
            raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
        if (
            image["layout_path"] != f"candidate-runtime/oci/{role}"
            or image["platform"] != "linux/amd64"
            or image["repository"] != _CANDIDATE_IMAGE_REPOSITORIES[role]
        ):
            raise PublicationTransactionError("TRANSACTION_CANDIDATE_PLAN_INVALID")
        digests[role] = _require_sha256(
            image["digest"], "TRANSACTION_CANDIDATE_PLAN_INVALID"
        )
    return {
        "schema": value["schema"],
        "plan_digest": supplied_digest,
        "plan_identity": supplied_digest,
        "repository": value["repository"],
        "channel": channel,
        "tag": tag,
        "source_sha": value["source_sha"],
        "source_tree": value["source_tree"],
        "api_digest": digests["api"],
        "web_digest": digests["web"],
        "assets": {},
        "transport_assets": {},
    }


def _normalize_authority_plan(
    plan: Mapping[str, Any], source_tree: str | None
) -> dict[str, Any]:
    if isinstance(plan, Mapping) and plan.get("schema") == PUBLISH_CANDIDATE_PLAN_SCHEMA:
        return _candidate_authority(plan, source_tree)
    try:
        validated = validate_publication_plan(plan)
    except (KeyError, PublicationError, TypeError, ValueError) as error:
        raise PublicationTransactionError("TRANSACTION_PUBLICATION_PLAN_INVALID") from error
    if source_tree is None:
        resolved_tree = "0" * 40
    elif not isinstance(source_tree, str) or not _COMMIT.fullmatch(source_tree):
        raise PublicationTransactionError("TRANSACTION_SOURCE_TREE_INVALID")
    else:
        resolved_tree = source_tree
    return {
        "schema": validated["schema"],
        "plan_digest": _identity(validated),
        "plan_identity": validated["identity"],
        "repository": validated["repository"],
        "channel": validated["channel"],
        "tag": validated["tag"],
        "source_sha": validated["commit"],
        "source_tree": resolved_tree,
        "api_digest": validated["api_digest"],
        "web_digest": validated["web_digest"],
        "assets": copy.deepcopy(validated["assets"]),
        "transport_assets": copy.deepcopy(validated.get("transport_assets", {})),
    }


def _expected_projection(authority: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "apiDigest": authority["api_digest"],
        "webDigest": authority["web_digest"],
        "assets": copy.deepcopy(authority["assets"]),
        "transportAssets": copy.deepcopy(authority["transport_assets"]),
    }


def build_initial_ledger(
    plan: Mapping[str, Any],
    *,
    source_tree: str,
    intents: Sequence[MutationIntent],
) -> dict[str, Any]:
    authority = _normalize_authority_plan(plan, source_tree)
    if not intents or len(intents) > 64:
        raise PublicationTransactionError("TRANSACTION_STEP_SET_INVALID")
    names = [item.name for item in intents]
    if len(names) != len(set(names)):
        raise PublicationTransactionError("TRANSACTION_STEP_SET_INVALID")
    snapshot: dict[str, Any] = {
        "schemaVersion": 1,
        "operationId": operation_id_for_plan(plan),
        "planSchema": authority["schema"],
        "planDigest": authority["plan_digest"],
        "planIdentity": authority["plan_identity"],
        "repository": authority["repository"],
        "channel": authority["channel"],
        "source": {"sha": authority["source_sha"], "tree": authority["source_tree"]},
        "target": {"tag": authority["tag"], "version": authority["tag"]},
        "expected": _expected_projection(authority),
        "attemptLimit": ATTEMPT_LIMIT,
        "steps": [
            {
                "name": item.name,
                "kind": item.kind,
                "remoteKey": item.remote_key,
                "expectedIdentity": item.expected_identity,
                "state": "WAITING",
                "preflight": None,
                "attempts": [],
                "committed": False,
            }
            for item in intents
        ],
        "revision": 0,
        "previousLedgerIdentity": None,
        "finalState": "ACTIVE",
        "recoveryStatus": "NOT_REQUIRED",
    }
    snapshot["ledgerIdentity"] = _ledger_identity(snapshot)
    return validate_ledger(snapshot)


def validate_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicationTransactionError("TRANSACTION_LEDGER_INVALID")
    snapshot = copy.deepcopy(dict(value))
    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(snapshot),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise PublicationTransactionError("TRANSACTION_LEDGER_SCHEMA_INVALID")
    if snapshot["ledgerIdentity"] != _ledger_identity(snapshot):
        raise PublicationTransactionError("TRANSACTION_LEDGER_IDENTITY_MISMATCH")
    expected_operation = _identity(
        {
            "schema": "animemo.publication-operation-key/v1",
            "repository": snapshot["repository"],
            "channel": snapshot["channel"],
            "tag": snapshot["target"]["tag"],
        }
    )
    if snapshot["operationId"] != expected_operation:
        raise PublicationTransactionError("TRANSACTION_OPERATION_ID_MISMATCH")
    if snapshot["target"]["version"] != snapshot["target"]["tag"]:
        raise PublicationTransactionError("TRANSACTION_TARGET_IDENTITY_MISMATCH")
    names: set[str] = set()
    unresolved = 0

    def require_observation(payload: Mapping[str, Any], expected: str) -> None:
        classification = payload["classification"]
        identity = payload["identity"]
        if classification == ObservationClass.SAME.value:
            valid = identity == expected
        elif classification == ObservationClass.DIFFERENT.value:
            valid = (
                isinstance(identity, str)
                and _SHA256.fullmatch(identity) is not None
                and identity != expected
            )
        else:
            valid = identity is None
        if not valid or (
            classification == ObservationClass.UNKNOWN.value
            and payload["diagnosticCode"] is None
        ):
            raise PublicationTransactionError("TRANSACTION_OBSERVATION_INVALID")

    for step in snapshot["steps"]:
        if step["name"] in names:
            raise PublicationTransactionError("TRANSACTION_STEP_SET_INVALID")
        names.add(step["name"])
        if step["preflight"] is not None:
            require_observation(step["preflight"], step["expectedIdentity"])
        if step["committed"] is not (step["state"] == "COMMITTED"):
            raise PublicationTransactionError("TRANSACTION_STEP_STATE_INVALID")
        for index, attempt in enumerate(step["attempts"], start=1):
            if attempt["number"] != index:
                raise PublicationTransactionError("TRANSACTION_ATTEMPT_SEQUENCE_INVALID")
            pending = attempt["response"] is None or attempt["readback"] is None
            require_observation(attempt["prestate"], step["expectedIdentity"])
            if attempt["readback"] is not None:
                require_observation(attempt["readback"], step["expectedIdentity"])
            if (
                attempt["requestStarted"] is not True
                or attempt["prestate"]["classification"]
                != ObservationClass.ABSENT.value
                or attempt["prestate"]["identity"] is not None
            ):
                raise PublicationTransactionError("TRANSACTION_ATTEMPT_STATE_INVALID")
            if pending:
                unresolved += 1
                if (
                    index != len(step["attempts"])
                    or step["state"] != "REQUEST_STARTED"
                    or attempt["requestStarted"] is not True
                    or attempt["committed"] is not False
                ):
                    raise PublicationTransactionError("TRANSACTION_ATTEMPT_STATE_INVALID")
            elif attempt["committed"] is not (
                attempt["readback"]["classification"] == ObservationClass.SAME.value
            ):
                raise PublicationTransactionError("TRANSACTION_ATTEMPT_STATE_INVALID")
            elif (
                attempt["response"]["classification"] == ResponseClass.TERMINAL.value
                and attempt["readback"]["classification"]
                != ObservationClass.SAME.value
                and step["state"] != "FROZEN"
            ):
                raise PublicationTransactionError("TRANSACTION_ATTEMPT_STATE_INVALID")
        if step["state"] == "REQUEST_STARTED" and not step["attempts"]:
            raise PublicationTransactionError("TRANSACTION_ATTEMPT_STATE_INVALID")
    if unresolved > 1:
        raise PublicationTransactionError("TRANSACTION_MULTIPLE_REQUESTS_IN_FLIGHT")
    if snapshot["finalState"] == "COMPLETE" and not all(
        step["committed"] for step in snapshot["steps"]
    ):
        raise PublicationTransactionError("TRANSACTION_FINAL_STATE_INVALID")
    if snapshot["finalState"] == "FROZEN" and not any(
        step["state"] == "FROZEN" for step in snapshot["steps"]
    ):
        raise PublicationTransactionError("TRANSACTION_FINAL_STATE_INVALID")
    if snapshot["finalState"] == "ACTIVE" and any(
        step["state"] == "FROZEN" for step in snapshot["steps"]
    ):
        raise PublicationTransactionError("TRANSACTION_FINAL_STATE_INVALID")
    if (snapshot["recoveryStatus"] == "COMPLETE") is not (
        snapshot["finalState"] == "COMPLETE"
    ):
        raise PublicationTransactionError("TRANSACTION_FINAL_STATE_INVALID")
    return snapshot


def _next_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise PublicationTransactionError("TRANSACTION_LEDGER_INVALID")
    next_value = copy.deepcopy(dict(snapshot))
    parent_identity = _require_sha256(
        next_value.pop("ledgerIdentity", None),
        "TRANSACTION_LEDGER_IDENTITY_MISMATCH",
    )
    # Controller transitions intentionally modify a copy of the prior snapshot.
    # Re-sign that proposed state to run every schema/semantic check before it is
    # assigned the next revision; the journal independently verifies parent_identity.
    next_value["ledgerIdentity"] = _ledger_identity(next_value)
    next_value = validate_ledger(next_value)
    next_value["revision"] += 1
    next_value["previousLedgerIdentity"] = parent_identity
    next_value.pop("ledgerIdentity", None)
    next_value["ledgerIdentity"] = _ledger_identity(next_value)
    return validate_ledger(next_value)


class LocalAtomicJournal:
    """Append canonical snapshots atomically without trusting a mutable HEAD file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise PublicationTransactionError("TRANSACTION_JOURNAL_BOUNDARY_INVALID")

    def _directory(self, operation_id: str) -> Path:
        _require_sha256(operation_id, "TRANSACTION_OPERATION_ID_INVALID")
        return self.root / operation_id.removeprefix("sha256:")

    def load(self, operation_id: str) -> dict[str, Any] | None:
        directory = self._directory(operation_id)
        if not directory.exists():
            return None
        if not directory.is_dir() or directory.is_symlink():
            raise PublicationTransactionError("TRANSACTION_JOURNAL_BOUNDARY_INVALID")
        try:
            entries = tuple(directory.iterdir())
        except OSError as error:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_CORRUPT") from error
        if any(
            entry.is_symlink()
            or (
                not entry.is_file()
                or (
                    not re.fullmatch(
                        r"[0-9]{6}-sha256-[0-9a-f]{64}\.json", entry.name
                    )
                    and not re.fullmatch(r"\.ledger-[^.]+\.tmp", entry.name)
                )
            )
            for entry in entries
        ):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_CORRUPT")
        paths = sorted(directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9]-sha256-*.json"))
        if len(paths) > MAX_LEDGER_REVISIONS:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_RESOURCE_LIMIT")
        previous: dict[str, Any] | None = None
        seen_revisions: set[int] = set()
        for path in paths:
            try:
                raw = path.read_bytes()
                value = json.loads(raw.decode("utf-8", errors="strict"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_CORRUPT") from error
            snapshot = validate_ledger(value)
            revision = snapshot["revision"]
            expected_name = (
                f"{revision:06d}-{snapshot['ledgerIdentity'].replace(':', '-')}.json"
            )
            if path.name != expected_name or revision in seen_revisions:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_FORK")
            seen_revisions.add(revision)
            if snapshot["operationId"] != operation_id:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_OPERATION_MISMATCH")
            if previous is None:
                if revision != 0 or snapshot["previousLedgerIdentity"] is not None:
                    raise PublicationTransactionError("TRANSACTION_JOURNAL_CHAIN_INVALID")
            elif (
                revision != previous["revision"] + 1
                or snapshot["previousLedgerIdentity"] != previous["ledgerIdentity"]
            ):
                raise PublicationTransactionError("TRANSACTION_JOURNAL_CHAIN_INVALID")
            previous = snapshot
        return copy.deepcopy(previous)

    def append(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_ledger(snapshot)
        directory = self._directory(value["operationId"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise PublicationTransactionError("TRANSACTION_JOURNAL_BOUNDARY_INVALID")
        current = self.load(value["operationId"])
        if current is not None and current["ledgerIdentity"] == value["ledgerIdentity"]:
            return current
        if current is None:
            valid_parent = value["revision"] == 0 and value["previousLedgerIdentity"] is None
        else:
            valid_parent = (
                value["revision"] == current["revision"] + 1
                and value["previousLedgerIdentity"] == current["ledgerIdentity"]
            )
        if not valid_parent:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_APPEND_CONFLICT")
        final = directory / (
            f"{value['revision']:06d}-{value['ledgerIdentity'].replace(':', '-')}.json"
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            payload = _canonical_bytes(value)
            consumed = 0
            while consumed < len(payload):
                written = os.write(descriptor, payload[consumed:])
                if written <= 0:
                    raise OSError("short write")
                consumed += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, final)
            try:
                directory_descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                if os.name != "nt":
                    raise
        except OSError as error:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_WRITE_FAILED") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        readback = self.load(value["operationId"])
        if readback is None or readback["ledgerIdentity"] != value["ledgerIdentity"]:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
        return readback


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


GitRunner = Callable[
    [tuple[str, ...], int, bytes | None, Mapping[str, str] | None],
    GitCommandResult,
]


def _run_git_command(
    argv: tuple[str, ...],
    timeout_seconds: int,
    input_bytes: bytes | None,
    environment: Mapping[str, str] | None,
) -> GitCommandResult:
    resolved_environment = os.environ.copy()
    if environment:
        resolved_environment.update(environment)
    completed = subprocess.run(
        argv,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
        env=resolved_environment,
    )
    return GitCommandResult(completed.returncode, completed.stdout, completed.stderr)


class GitRemoteAppendOnlyJournal:
    """Persist snapshots as a fast-forward-only Git ref, with push readback."""

    def __init__(
        self,
        repository: Path,
        *,
        remote: str = "origin",
        ref_prefix: str = "refs/heads/publication-transactions/",
        run_git: GitRunner = _run_git_command,
    ) -> None:
        if (
            not ref_prefix.startswith("refs/heads/")
            or not ref_prefix.endswith("/")
            or any(character.isspace() for character in ref_prefix)
        ):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_REF_INVALID")
        if not remote or any(character.isspace() for character in remote):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_REMOTE_INVALID")
        self.repository = Path(repository)
        self.remote = remote
        self.ref_prefix = ref_prefix
        self.run_git = run_git

    def _ref(self, operation_id: str) -> str:
        _require_sha256(operation_id, "TRANSACTION_OPERATION_ID_INVALID")
        return self.ref_prefix + operation_id.removeprefix("sha256:")

    def _git(
        self,
        *arguments: str,
        timeout: int = 30,
        input_bytes: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> GitCommandResult:
        return self.run_git(
            ("git", "-C", str(self.repository), *arguments),
            timeout,
            input_bytes,
            environment,
        )

    def _remote_head(self, operation_id: str) -> str | None:
        ref = self._ref(operation_id)
        try:
            result = self._git("ls-remote", "--refs", self.remote, ref)
        except subprocess.TimeoutExpired as error:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN") from error
        if result.returncode != 0:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
        lines = [line for line in result.stdout.decode("ascii", errors="strict").splitlines() if line]
        if not lines:
            return None
        if len(lines) != 1:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_REF_AMBIGUOUS")
        head, separator, observed_ref = lines[0].partition("\t")
        if not separator or observed_ref != ref or not _HEX.fullmatch(head):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_REF_AMBIGUOUS")
        return head

    def load(self, operation_id: str) -> dict[str, Any] | None:
        head = self._remote_head(operation_id)
        if head is None:
            return None
        ref = self._ref(operation_id)
        result = self._git("fetch", "--no-tags", self.remote, ref, timeout=90)
        if result.returncode != 0:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
        resolved = self._git("rev-parse", "FETCH_HEAD")
        if resolved.returncode != 0 or resolved.stdout.decode("ascii", errors="strict").strip() != head:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
        history = self._git("rev-list", "--reverse", "--parents", head)
        if history.returncode != 0:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_HISTORY_INVALID")
        rows = [line.split() for line in history.stdout.decode("ascii", errors="strict").splitlines()]
        if not rows or len(rows) > MAX_LEDGER_REVISIONS:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_RESOURCE_LIMIT")
        previous_commit: str | None = None
        previous: dict[str, Any] | None = None
        for row in rows:
            if len(row) != (1 if previous_commit is None else 2):
                raise PublicationTransactionError("TRANSACTION_JOURNAL_HISTORY_INVALID")
            commit = row[0]
            if previous_commit is not None and row[1] != previous_commit:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_HISTORY_INVALID")
            tree = self._git("ls-tree", "-r", "--full-tree", commit)
            tree_line = tree.stdout.decode("ascii", errors="strict").rstrip("\n")
            if (
                tree.returncode != 0
                or re.fullmatch(
                    r"100644 blob [0-9a-f]{40}\tledger\.json", tree_line
                )
                is None
            ):
                raise PublicationTransactionError("TRANSACTION_JOURNAL_TREE_INVALID")
            shown = self._git("show", f"{commit}:ledger.json")
            try:
                snapshot = validate_ledger(json.loads(shown.stdout.decode("utf-8", errors="strict")))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_CORRUPT") from error
            if shown.returncode != 0 or snapshot["operationId"] != operation_id:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_OPERATION_MISMATCH")
            if previous is None:
                valid_chain = snapshot["revision"] == 0 and snapshot["previousLedgerIdentity"] is None
            else:
                valid_chain = (
                    snapshot["revision"] == previous["revision"] + 1
                    and snapshot["previousLedgerIdentity"] == previous["ledgerIdentity"]
                )
            if not valid_chain:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_CHAIN_INVALID")
            previous_commit = commit
            previous = snapshot
        return copy.deepcopy(previous)

    def append(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_ledger(snapshot)
        current = self.load(value["operationId"])
        if current is not None and current["ledgerIdentity"] == value["ledgerIdentity"]:
            return current
        if current is None:
            parent = None
        else:
            resolved = self._git("rev-parse", "FETCH_HEAD")
            parent = resolved.stdout.decode("ascii", errors="strict").strip()
            if resolved.returncode != 0 or not _HEX.fullmatch(parent):
                raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
            if self._remote_head(value["operationId"]) != parent:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_APPEND_CONFLICT")
        if current is None:
            valid_parent = value["revision"] == 0 and value["previousLedgerIdentity"] is None
        else:
            valid_parent = (
                value["revision"] == current["revision"] + 1
                and value["previousLedgerIdentity"] == current["ledgerIdentity"]
            )
        if not valid_parent:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_APPEND_CONFLICT")
        blob = self._git("hash-object", "-w", "--stdin", input_bytes=_canonical_bytes(value))
        blob_sha = blob.stdout.decode("ascii", errors="strict").strip()
        if blob.returncode != 0 or not _HEX.fullmatch(blob_sha):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_OBJECT_WRITE_FAILED")
        tree_input = f"100644 blob {blob_sha}\tledger.json\n".encode("ascii")
        tree = self._git("mktree", input_bytes=tree_input)
        tree_sha = tree.stdout.decode("ascii", errors="strict").strip()
        if tree.returncode != 0 or not _HEX.fullmatch(tree_sha):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_OBJECT_WRITE_FAILED")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        arguments = ["commit-tree", tree_sha]
        if parent is not None:
            arguments.extend(("-p", parent))
        commit = self._git(
            *arguments,
            input_bytes=(
                f"记录发布事务 {value['target']['tag']} 修订 {value['revision']}\n"
            ).encode("utf-8"),
            environment={
                "GIT_AUTHOR_NAME": "AniMemo 发布事务控制器",
                "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "AniMemo 发布事务控制器",
                "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                "GIT_AUTHOR_DATE": now,
                "GIT_COMMITTER_DATE": now,
            },
        )
        commit_sha = commit.stdout.decode("ascii", errors="strict").strip()
        if commit.returncode != 0 or not _HEX.fullmatch(commit_sha):
            raise PublicationTransactionError("TRANSACTION_JOURNAL_OBJECT_WRITE_FAILED")
        ref = self._ref(value["operationId"])
        try:
            pushed = self._git(
                "push",
                self.remote,
                f"{commit_sha}:{ref}",
                timeout=180,
            )
            push_failed = pushed.returncode != 0
        except subprocess.TimeoutExpired:
            push_failed = True
        observed = self._remote_head(value["operationId"])
        if observed == commit_sha:
            loaded = self.load(value["operationId"])
            if loaded is None or loaded["ledgerIdentity"] != value["ledgerIdentity"]:
                raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")
            return loaded
        if observed == parent or (observed is None and parent is None):
            code = "TRANSACTION_JOURNAL_APPEND_NOT_COMMITTED"
        else:
            code = "TRANSACTION_JOURNAL_APPEND_CONFLICT"
        if push_failed:
            raise PublicationTransactionError(code)
        raise PublicationTransactionError("TRANSACTION_JOURNAL_READBACK_UNKNOWN")


class DurablePublicationController:
    """Advance one durable publication plan through readback-gated mutations."""

    def __init__(
        self,
        *,
        initial: Mapping[str, Any],
        journal: JournalStore,
        adapters: Mapping[str, RemoteMutationAdapter],
    ) -> None:
        self.initial = validate_ledger(initial)
        self.journal = journal
        expected_names = {step["name"] for step in self.initial["steps"]}
        if set(adapters) != expected_names:
            raise PublicationTransactionError("TRANSACTION_ADAPTER_SET_INVALID")
        self.adapters = dict(adapters)
        loaded = journal.load(self.initial["operationId"])
        if loaded is None:
            self.ledger = journal.append(self.initial)
        else:
            self._require_same_transaction(loaded)
            self.ledger = loaded

    @classmethod
    def open(
        cls,
        plan: Mapping[str, Any],
        *,
        source_tree: str,
        intents: Sequence[MutationIntent],
        journal: JournalStore,
        adapters: Mapping[str, RemoteMutationAdapter],
    ) -> DurablePublicationController:
        return cls(
            initial=build_initial_ledger(plan, source_tree=source_tree, intents=intents),
            journal=journal,
            adapters=adapters,
        )

    def _require_same_transaction(self, loaded: Mapping[str, Any]) -> None:
        observed = validate_ledger(loaded)
        static_fields = (
            "operationId",
            "planSchema",
            "planDigest",
            "planIdentity",
            "repository",
            "channel",
            "source",
            "target",
            "expected",
            "attemptLimit",
        )
        if any(observed[field] != self.initial[field] for field in static_fields):
            raise PublicationTransactionError("TRANSACTION_PLAN_MISMATCH")
        expected_steps = [
            {
                "name": step["name"],
                "kind": step["kind"],
                "remoteKey": step["remoteKey"],
                "expectedIdentity": step["expectedIdentity"],
            }
            for step in self.initial["steps"]
        ]
        observed_steps = [
            {
                "name": step["name"],
                "kind": step["kind"],
                "remoteKey": step["remoteKey"],
                "expectedIdentity": step["expectedIdentity"],
            }
            for step in observed["steps"]
        ]
        if observed_steps != expected_steps:
            raise PublicationTransactionError("TRANSACTION_PLAN_MISMATCH")

    def _reload(self) -> None:
        loaded = self.journal.load(self.initial["operationId"])
        if loaded is None:
            raise PublicationTransactionError("TRANSACTION_JOURNAL_MISSING")
        self._require_same_transaction(loaded)
        self.ledger = loaded

    def _persist(self, value: Mapping[str, Any]) -> None:
        self.ledger = self.journal.append(_next_snapshot(value))

    def _step(self, value: dict[str, Any], name: str) -> dict[str, Any]:
        try:
            return next(step for step in value["steps"] if step["name"] == name)
        except StopIteration as error:
            raise PublicationTransactionError("TRANSACTION_STEP_UNKNOWN") from error

    @staticmethod
    def _intent(step: Mapping[str, Any]) -> MutationIntent:
        return MutationIntent(
            name=step["name"],
            kind=step["kind"],
            remote_key=step["remoteKey"],
            expected_identity=step["expectedIdentity"],
        )

    def _observe(self, step: Mapping[str, Any]) -> RemoteObservation:
        try:
            observation = self.adapters[step["name"]].observe(self._intent(step))
        except Exception:
            return RemoteObservation.unknown("REMOTE_OBSERVATION_FAILED")
        if not isinstance(observation, RemoteObservation):
            return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
        if not isinstance(observation.classification, ObservationClass):
            return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
        if (
            observation.diagnostic_code is not None
            and not _DIAGNOSTIC.fullmatch(observation.diagnostic_code)
        ):
            return RemoteObservation.unknown("REMOTE_DIAGNOSTIC_INVALID")
        expected = step["expectedIdentity"]
        if observation.classification is ObservationClass.SAME:
            if observation.identity != expected:
                return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
        elif observation.classification is ObservationClass.DIFFERENT:
            if not isinstance(observation.identity, str) or not _SHA256.fullmatch(observation.identity):
                return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
            if observation.identity == expected:
                return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
        elif observation.identity is not None:
            return RemoteObservation.unknown("REMOTE_OBSERVATION_INVALID")
        if (
            observation.classification is ObservationClass.UNKNOWN
            and observation.diagnostic_code is None
        ):
            return RemoteObservation.unknown()
        return observation

    def _freeze(
        self,
        value: dict[str, Any],
        step: dict[str, Any],
        *,
        unknown: bool,
    ) -> None:
        step["state"] = "FROZEN"
        step["committed"] = False
        value["finalState"] = "FROZEN"
        value["recoveryStatus"] = (
            "GLOBAL_FREEZE_UNKNOWN" if unknown else "GLOBAL_FREEZE_DIFFERENT"
        )

    def preflight_all(self) -> dict[str, Any]:
        """Observe every key before any mutation; one conflict freezes the batch."""

        self._reload()
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        if self.ledger["finalState"] == "COMPLETE":
            return copy.deepcopy(self.ledger)
        if any(step["state"] == "REQUEST_STARTED" for step in self.ledger["steps"]):
            self.resume_pending()
            self._reload()
        value = copy.deepcopy(self.ledger)
        observations: list[tuple[dict[str, Any], RemoteObservation]] = []
        for step in value["steps"]:
            observation = self._observe(step)
            step["preflight"] = _observation_payload(observation)
            observations.append((step, observation))
        conflict = next(
            (
                (step, observation)
                for step, observation in observations
                if observation.classification
                in {ObservationClass.DIFFERENT, ObservationClass.UNKNOWN}
            ),
            None,
        )
        regression = next(
            (
                (step, observation)
                for step, observation in observations
                if step["committed"]
                and observation.classification is ObservationClass.ABSENT
            ),
            None,
        )
        if conflict is not None:
            step, observation = conflict
            self._freeze(
                value,
                step,
                unknown=observation.classification is ObservationClass.UNKNOWN,
            )
        elif regression is not None:
            step, _observation = regression
            self._freeze(value, step, unknown=True)
        else:
            saw_same = False
            for step, observation in observations:
                if observation.classification is ObservationClass.SAME:
                    step["state"] = "COMMITTED"
                    step["committed"] = True
                    saw_same = True
                elif not step["committed"]:
                    step["state"] = "READY"
            value["recoveryStatus"] = (
                "IDEMPOTENT_REMOTE_SAME" if saw_same else "NOT_REQUIRED"
            )
        self._persist(value)
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        return copy.deepcopy(self.ledger)

    def _apply_readback(
        self,
        value: dict[str, Any],
        step: dict[str, Any],
        attempt: dict[str, Any],
        observation: RemoteObservation,
        *,
        interrupted: bool,
    ) -> None:
        attempt["readback"] = _observation_payload(observation)
        if observation.classification is ObservationClass.SAME:
            attempt["committed"] = True
            step["state"] = "COMMITTED"
            step["committed"] = True
            value["recoveryStatus"] = (
                "CONTROLLER_INTERRUPTION_RECONCILED"
                if interrupted
                else "REMOTE_COMMIT_CONFIRMED"
            )
        elif (
            observation.classification is ObservationClass.ABSENT
            and attempt["response"]["classification"]
            == ResponseClass.TERMINAL.value
        ):
            attempt["committed"] = False
            self._freeze(value, step, unknown=True)
        elif observation.classification is ObservationClass.ABSENT:
            attempt["committed"] = False
            step["committed"] = False
            if len(step["attempts"]) >= ATTEMPT_LIMIT:
                step["state"] = "FROZEN"
                value["finalState"] = "FROZEN"
                value["recoveryStatus"] = "ATTEMPT_LIMIT_EXHAUSTED"
            else:
                step["state"] = "READY"
                value["recoveryStatus"] = "REMOTE_ABSENT_EXACT_CONTINUE"
        else:
            attempt["committed"] = False
            self._freeze(
                value,
                step,
                unknown=observation.classification is ObservationClass.UNKNOWN,
            )

    def resume_pending(self) -> dict[str, Any]:
        """Read remote state only; never replay an interrupted request here."""

        self._reload()
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        value = copy.deepcopy(self.ledger)
        pending = [step for step in value["steps"] if step["state"] == "REQUEST_STARTED"]
        if not pending:
            return copy.deepcopy(self.ledger)
        if len(pending) != 1:
            raise PublicationTransactionError("TRANSACTION_MULTIPLE_REQUESTS_IN_FLIGHT")
        step = pending[0]
        attempt = step["attempts"][-1]
        if attempt["response"] is None:
            attempt["response"] = _response_payload(
                MutationResponse.ambiguous("CONTROLLER_INTERRUPTED")
            )
        observation = self._observe(step)
        self._apply_readback(value, step, attempt, observation, interrupted=True)
        self._persist(value)
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        return copy.deepcopy(self.ledger)

    def begin_external(self, step_name: str) -> dict[str, Any]:
        """Durably record one request intent before an external action runs."""

        self._reload()
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        if any(step["state"] == "REQUEST_STARTED" for step in self.ledger["steps"]):
            self.resume_pending()
            self._reload()
        value = copy.deepcopy(self.ledger)
        step = self._step(value, step_name)
        if step["state"] == "COMMITTED" and step["committed"]:
            return copy.deepcopy(self.ledger)
        first_uncommitted = next(
            (item for item in value["steps"] if not item["committed"]), None
        )
        if first_uncommitted is None:
            return copy.deepcopy(self.ledger)
        if first_uncommitted["name"] != step["name"]:
            raise PublicationTransactionError("TRANSACTION_STEP_ORDER_INVALID")
        if step["state"] != "READY":
            raise PublicationTransactionError("TRANSACTION_PREFLIGHT_REQUIRED")
        prestate = self._observe(step)
        if prestate.classification is ObservationClass.SAME:
            step["state"] = "COMMITTED"
            step["committed"] = True
            value["recoveryStatus"] = "IDEMPOTENT_REMOTE_SAME"
            self._persist(value)
            return copy.deepcopy(self.ledger)
        if prestate.classification in {ObservationClass.DIFFERENT, ObservationClass.UNKNOWN}:
            self._freeze(
                value,
                step,
                unknown=prestate.classification is ObservationClass.UNKNOWN,
            )
            self._persist(value)
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        if len(step["attempts"]) >= ATTEMPT_LIMIT:
            step["state"] = "FROZEN"
            value["finalState"] = "FROZEN"
            value["recoveryStatus"] = "ATTEMPT_LIMIT_EXHAUSTED"
            self._persist(value)
            raise PublicationTransactionError("TRANSACTION_ATTEMPT_LIMIT_EXHAUSTED")
        number = len(step["attempts"]) + 1
        intent_identity = _identity(
            {
                "schema": "animemo.publication-mutation-intent/v1",
                "operationId": value["operationId"],
                "step": step["name"],
                "kind": step["kind"],
                "remoteKey": step["remoteKey"],
                "expectedIdentity": step["expectedIdentity"],
                "attempt": number,
            }
        )
        step["attempts"].append(
            {
                "number": number,
                "prestate": _observation_payload(prestate),
                "intentIdentity": intent_identity,
                "requestStarted": True,
                "response": None,
                "readback": None,
                "committed": False,
            }
        )
        step["state"] = "REQUEST_STARTED"
        step["committed"] = False
        self._persist(value)
        return copy.deepcopy(self.ledger)

    def reconcile_external(
        self,
        step_name: str,
        *,
        response: MutationResponse,
    ) -> dict[str, Any]:
        """Read back an externally executed request; never invoke the mutation."""

        self._reload()
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        value = copy.deepcopy(self.ledger)
        step = self._step(value, step_name)
        if step["state"] == "COMMITTED":
            return copy.deepcopy(self.ledger)
        if step["state"] != "REQUEST_STARTED" or not step["attempts"]:
            raise PublicationTransactionError("TRANSACTION_EXTERNAL_REQUEST_NOT_PENDING")
        attempt = step["attempts"][-1]
        if attempt["response"] is not None or attempt["readback"] is not None:
            raise PublicationTransactionError("TRANSACTION_EXTERNAL_REQUEST_NOT_PENDING")
        if not isinstance(response, MutationResponse):
            response = MutationResponse.ambiguous("REMOTE_RESPONSE_INVALID")
        attempt["response"] = _response_payload(response)
        observation = self._observe(step)
        self._apply_readback(value, step, attempt, observation, interrupted=False)
        self._persist(value)
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        return copy.deepcopy(self.ledger)

    def advance(self, step_name: str) -> dict[str, Any]:
        """Advance the first unresolved step, invoking its adapter at most once."""

        started = self.begin_external(step_name)
        started_step = self._step(started, step_name)
        if started_step["state"] == "COMMITTED":
            return copy.deepcopy(started)

        try:
            response = self.adapters[step_name].mutate(self._intent(started_step))
            if not isinstance(response, MutationResponse):
                response = MutationResponse.ambiguous("REMOTE_RESPONSE_INVALID")
        except (TimeoutError, ConnectionError, OSError, subprocess.TimeoutExpired):
            response = MutationResponse.ambiguous("REMOTE_REQUEST_INTERRUPTED")
        except Exception:
            response = MutationResponse.ambiguous("REMOTE_REQUEST_FAILED")
        return self.reconcile_external(step_name, response=response)

    def finalize(self) -> dict[str, Any]:
        self._reload()
        if self.ledger["finalState"] == "FROZEN":
            raise PublicationTransactionError("TRANSACTION_GLOBAL_FREEZE")
        if self.ledger["finalState"] == "COMPLETE":
            return copy.deepcopy(self.ledger)
        # COMPLETE is written only after one fresh, batch-wide remote readback.
        self.preflight_all()
        self._reload()
        if not all(step["committed"] for step in self.ledger["steps"]):
            raise PublicationTransactionError("TRANSACTION_STEPS_INCOMPLETE")
        value = copy.deepcopy(self.ledger)
        value["finalState"] = "COMPLETE"
        value["recoveryStatus"] = "COMPLETE"
        self._persist(value)
        return copy.deepcopy(self.ledger)
