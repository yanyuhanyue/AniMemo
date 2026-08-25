"""Trusted, read-only metadata freshness collection and verification."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .materials import DuplicateJsonFieldError, reject_duplicate_json_keys
from .notes import (
    ReleaseNotesError,
    build_release_notes,
    render_release_notes,
    validate_release_notes,
)

REPOSITORY = "yanyuhanyue/AniMemo"
WORKFLOW_PATH = ".github/workflows/release-metadata-freshness.yml"
QUALIFICATION_WORKFLOW_NAME = "Release Producer"
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/release.yml"
FRESHNESS_WORKFLOW_NAME = "Release Metadata Freshness"
SCHEMA_VERSION = 1
MINIMUM_SNAPSHOT_INTERVAL_SECONDS = 60
FRESHNESS_TTL_SECONDS = 15 * 60
MAX_COMPLETE_ATTEMPTS = 3
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_MEMBER_BYTES = 8 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
ENDPOINT_TEMPLATE = (
    "repos/yanyuhanyue/AniMemo/commits/{sha}/pulls?per_page=100"
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

ARTIFACT_FILES = frozenset(
    {
        "candidate-acceptance-receipt.json",
        "metadata-freshness.json",
        "snapshot-a-input.json",
        "snapshot-a.json",
        "snapshot-a.md",
        "snapshot-b-input.json",
        "snapshot-b.json",
        "snapshot-b.md",
        "snapshot-comparison.json",
        "request-diagnostics.json",
    }
)

RECEIPT_FIELDS = frozenset(
    {
        "schemaVersion",
        "workflowRunId",
        "workflowAttempt",
        "workflowPath",
        "workflowSha",
        "candidateSha",
        "candidateTree",
        "qualificationRunId",
        "qualificationArtifactId",
        "candidateAcceptanceReceiptSha256",
        "candidateVersion",
        "qualifiedReleaseTag",
        "qualifiedReleaseNotesIdentity",
        "qualifiedMarkdownSha",
        "qualifiedJsonSha",
        "qualifiedConfigurationIdentity",
        "qualifiedRendererIdentity",
        "snapshotCount",
        "artifactFileCount",
        "snapshotACompletedAt",
        "snapshotBCompletedAt",
        "snapshotIntervalSeconds",
        "snapshotAIdentity",
        "snapshotBIdentity",
        "snapshotAMarkdownSha",
        "snapshotBMarkdownSha",
        "snapshotAJsonSha",
        "snapshotBJsonSha",
        "population",
        "conflicts",
        "unclassified",
        "duplicates",
        "requestFailureCount",
        "result",
        "completedAt",
        "receiptDigest",
    }
)

DIAGNOSTIC_FIELDS = frozenset(
    {
        "endpointTemplate",
        "commitSha",
        "snapshot",
        "attempt",
        "startedAt",
        "durationMs",
        "exitCode",
        "httpStatus",
        "contentType",
        "responseBytes",
        "arrayLength",
        "githubRequestId",
        "rateLimitLimit",
        "rateLimitRemaining",
        "rateLimitUsed",
        "rateLimitReset",
        "retryAfter",
        "sanitizedErrorClass",
        "result",
    }
)

COMPARISON_FIELDS = frozenset(
    {
        "schemaVersion",
        "inputByteIdentical",
        "snapshotJsonByteIdentical",
        "markdownByteIdentical",
        "identityIdentical",
        "qualificationIdentityMatch",
        "qualificationMarkdownMatch",
        "qualificationJsonMatch",
        "populationMatch",
        "minimumIntervalSeconds",
        "observedIntervalSeconds",
        "partialAttemptCombination",
        "result",
    }
)

RETRYABLE_ERROR_CODES = frozenset(
    {
        "TRANSPORT_CONNECTION_RESET",
        "TRANSPORT_EOF",
        "TRANSPORT_TIMEOUT",
        "SECONDARY_RATE_LIMIT",
        "SERVER_ERROR",
    }
)

ERROR_CODES = RETRYABLE_ERROR_CODES | {
    "TRANSPORT_OTHER",
    "PRIMARY_RATE_LIMIT",
    "AUTHENTICATION_FAILURE",
    "PERMISSION_FAILURE",
    "NOT_FOUND",
    "JSON_INVALID",
    "RESPONSE_SHAPE_INVALID",
    "ASSOCIATED_PULLS_EMPTY",
    "RELEASE_NOTES_CONTRACT_FAILURE",
}


class MetadataFreshnessError(ValueError):
    """Trusted metadata freshness cannot be established."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "RELEASE_NOTES_CONTRACT_FAILURE",
        retry_after_seconds: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self._retryable = retryable

    @property
    def retryable(self) -> bool:
        if self._retryable is not None:
            return self._retryable
        return self.code in RETRYABLE_ERROR_CODES


@dataclass(frozen=True)
class FreshnessRunIdentity:
    workflow_run_id: int
    workflow_attempt: int
    workflow_path: str
    workflow_sha: str
    candidate_sha: str
    candidate_tree: str
    qualification_run_id: int
    qualification_artifact_id: int
    candidate_acceptance_receipt_sha256: str
    candidate_version: str

    def validate(self) -> None:
        for value, label in (
            (self.workflow_run_id, "workflow run ID"),
            (self.qualification_run_id, "qualification run ID"),
            (self.qualification_artifact_id, "qualification artifact ID"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MetadataFreshnessError(f"{label} is invalid")
        if self.workflow_attempt != 1:
            raise MetadataFreshnessError("freshness workflow attempt must be one")
        if self.workflow_path != WORKFLOW_PATH:
            raise MetadataFreshnessError("freshness workflow path is invalid")
        for value, label in (
            (self.workflow_sha, "workflow SHA"),
            (self.candidate_sha, "candidate SHA"),
            (self.candidate_tree, "candidate tree"),
        ):
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise MetadataFreshnessError(f"{label} is invalid")
        if self.workflow_sha != self.candidate_sha:
            raise MetadataFreshnessError(
                "freshness workflow definition differs from candidate SHA"
            )
        if not _DIGEST.fullmatch(self.candidate_acceptance_receipt_sha256):
            raise MetadataFreshnessError(
                "candidate acceptance receipt digest is invalid"
            )
        if not re.fullmatch(
            r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-rc\.[1-9][0-9]*",
            self.candidate_version,
        ):
            raise MetadataFreshnessError("candidate version is invalid")


@dataclass(frozen=True)
class FreshnessExpectation:
    workflow_run_id: int
    candidate_sha: str
    candidate_tree: str
    qualification_run_id: int
    qualification_artifact_id: int
    candidate_acceptance_receipt_sha256: str
    candidate_version: str

    def validate(self) -> None:
        FreshnessRunIdentity(
            workflow_run_id=self.workflow_run_id,
            workflow_attempt=1,
            workflow_path=WORKFLOW_PATH,
            workflow_sha=self.candidate_sha,
            candidate_sha=self.candidate_sha,
            candidate_tree=self.candidate_tree,
            qualification_run_id=self.qualification_run_id,
            qualification_artifact_id=self.qualification_artifact_id,
            candidate_acceptance_receipt_sha256=(
                self.candidate_acceptance_receipt_sha256
            ),
            candidate_version=self.candidate_version,
        ).validate()


@dataclass(frozen=True)
class FetchResponse:
    pulls: list[dict[str, Any]] | None
    diagnostic: dict[str, Any]
    error_code: str | None = None
    retry_after_seconds: int | None = None
    retryable: bool | None = None


class AssociatedPullSource(Protocol):
    def fetch(
        self,
        *,
        repository: str,
        commit: str,
        snapshot: str,
        attempt: int,
    ) -> FetchResponse: ...


class FreshnessClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemFreshnessClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        raise MetadataFreshnessError("freshness timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise MetadataFreshnessError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise MetadataFreshnessError(f"{label} is invalid") from error
    return parsed.astimezone(timezone.utc)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _strict_json_bytes(value: bytes, *, label: str) -> object:
    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (DuplicateJsonFieldError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MetadataFreshnessError(f"{label} is not strict JSON") from error


def _strict_json_file(path: Path, *, label: str) -> tuple[object, bytes]:
    try:
        file_stat = path.lstat()
    except OSError as error:
        raise MetadataFreshnessError(f"{label} is unavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or file_stat.st_size <= 0
        or file_stat.st_size > MAX_ARTIFACT_MEMBER_BYTES
    ):
        raise MetadataFreshnessError(f"{label} is not a single-link regular file")
    value = path.read_bytes()
    if len(value) != file_stat.st_size:
        raise MetadataFreshnessError(f"{label} changed while reading")
    return _strict_json_bytes(value, label=label), value


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers.get(name, ""))
    except (TypeError, ValueError):
        return None


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = _header_int(headers, "retry-after")
    return value if value is not None and 0 <= value <= FRESHNESS_TTL_SECONDS else None


def _http_error_code(status_code: int, headers: Mapping[str, str], body: bytes) -> str:
    message = body[:4096].decode("utf-8", errors="replace").lower()
    if status_code == 401:
        return "AUTHENTICATION_FAILURE"
    if status_code == 403:
        if _header_int(headers, "x-ratelimit-remaining") == 0:
            return "PRIMARY_RATE_LIMIT"
        if headers.get("retry-after") or "secondary rate limit" in message:
            return "SECONDARY_RATE_LIMIT"
        return "PERMISSION_FAILURE"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 429:
        return "SECONDARY_RATE_LIMIT"
    if status_code in {502, 503, 504}:
        return "SERVER_ERROR"
    if status_code >= 500:
        return "SERVER_ERROR"
    return "PERMISSION_FAILURE"


def _transport_error_code(error: BaseException) -> str:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "TRANSPORT_TIMEOUT"
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        return "TRANSPORT_CONNECTION_RESET"
    if isinstance(reason, (EOFError, http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return "TRANSPORT_EOF"
    text = str(reason).lower()
    if "reset" in text or "forcibly closed" in text:
        return "TRANSPORT_CONNECTION_RESET"
    if "eof" in text or "remote end closed" in text:
        return "TRANSPORT_EOF"
    if "timed out" in text or "timeout" in text:
        return "TRANSPORT_TIMEOUT"
    return "TRANSPORT_OTHER"


class GitHubAssociatedPullSource:
    """Fixed-endpoint GitHub adapter with closed, redacted diagnostics."""

    def __init__(
        self,
        token: str,
        *,
        clock: FreshnessClock | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise MetadataFreshnessError("GitHub workflow token is missing")
        self._token = token
        self._clock = clock or SystemFreshnessClock()
        self._opener = opener

    def fetch(
        self,
        *,
        repository: str,
        commit: str,
        snapshot: str,
        attempt: int,
    ) -> FetchResponse:
        if repository != REPOSITORY or not _SHA.fullmatch(commit):
            raise MetadataFreshnessError("associated-pulls request authority is invalid")
        started_at = self._clock.now()
        started_clock = self._clock.monotonic()
        url = f"https://api.github.com/repos/{repository}/commits/{commit}/pulls?per_page=100"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "AniMemo-Release-Metadata-Freshness/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        status_code: int | None = None
        headers: dict[str, str] = {}
        body = b""
        exit_code = 1
        error_code: str | None = None
        value: object = None
        try:
            with self._opener(request, timeout=30) as response:
                status_code = int(response.status)
                headers = {name.lower(): item for name, item in response.headers.items()}
                body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            exit_code = 0
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                error_code = "RESPONSE_SHAPE_INVALID"
            elif status_code != 200:
                error_code = _http_error_code(status_code, headers, body)
            elif 'rel="next"' in headers.get("link", ""):
                error_code = "RESPONSE_SHAPE_INVALID"
            else:
                try:
                    value = _strict_json_bytes(body, label="associated-pulls response")
                except MetadataFreshnessError:
                    error_code = "JSON_INVALID"
                if error_code is None and not isinstance(value, list):
                    error_code = "RESPONSE_SHAPE_INVALID"
                if error_code is None and not value:
                    error_code = "ASSOCIATED_PULLS_EMPTY"
        except urllib.error.HTTPError as error:
            status_code = int(error.code)
            headers = {name.lower(): item for name, item in error.headers.items()}
            body = error.read(MAX_HTTP_RESPONSE_BYTES + 1)
            exit_code = 0
            error_code = _http_error_code(status_code, headers, body)
        except (OSError, EOFError, http.client.HTTPException, urllib.error.URLError) as error:
            error_code = _transport_error_code(error)

        diagnostic = {
            "endpointTemplate": ENDPOINT_TEMPLATE,
            "commitSha": commit,
            "snapshot": snapshot,
            "attempt": attempt,
            "startedAt": _iso8601(started_at),
            "durationMs": round(
                max(0.0, self._clock.monotonic() - started_clock) * 1000,
                3,
            ),
            "exitCode": exit_code,
            "httpStatus": status_code,
            "contentType": headers.get("content-type"),
            "responseBytes": len(body),
            "arrayLength": len(value) if isinstance(value, list) else None,
            "githubRequestId": headers.get("x-github-request-id"),
            "rateLimitLimit": _header_int(headers, "x-ratelimit-limit"),
            "rateLimitRemaining": _header_int(headers, "x-ratelimit-remaining"),
            "rateLimitUsed": _header_int(headers, "x-ratelimit-used"),
            "rateLimitReset": _header_int(headers, "x-ratelimit-reset"),
            "retryAfter": _retry_after(headers),
            "sanitizedErrorClass": error_code,
            "result": "PASS" if error_code is None else "FAIL",
        }
        _validate_diagnostic(diagnostic)
        return FetchResponse(
            pulls=value if isinstance(value, list) and error_code is None else None,
            diagnostic=diagnostic,
            error_code=error_code,
            retry_after_seconds=_retry_after(headers),
            retryable=(
                error_code
                in {
                    "TRANSPORT_CONNECTION_RESET",
                    "TRANSPORT_EOF",
                    "TRANSPORT_TIMEOUT",
                    "SECONDARY_RATE_LIMIT",
                }
                or (error_code == "SERVER_ERROR" and status_code in {502, 503, 504})
            ),
        )


def _validate_diagnostic(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != DIAGNOSTIC_FIELDS:
        raise MetadataFreshnessError("request diagnostic schema is not closed")
    if value["endpointTemplate"] != ENDPOINT_TEMPLATE:
        raise MetadataFreshnessError("request diagnostic endpoint differs")
    if not isinstance(value["commitSha"], str) or not _SHA.fullmatch(value["commitSha"]):
        raise MetadataFreshnessError("request diagnostic commit is invalid")
    if value["snapshot"] not in {"A", "B"}:
        raise MetadataFreshnessError("request diagnostic snapshot is invalid")
    if isinstance(value["attempt"], bool) or value["attempt"] not in {1, 2, 3}:
        raise MetadataFreshnessError("request diagnostic attempt is invalid")
    if value["result"] not in {"PASS", "FAIL"}:
        raise MetadataFreshnessError("request diagnostic result is invalid")
    _parse_timestamp(value["startedAt"], label="request diagnostic timestamp")
    if (
        not isinstance(value["durationMs"], (int, float))
        or isinstance(value["durationMs"], bool)
        or value["durationMs"] < 0
        or value["exitCode"] not in {0, 1}
        or (
            value["httpStatus"] is not None
            and (
                isinstance(value["httpStatus"], bool)
                or not isinstance(value["httpStatus"], int)
                or not 100 <= value["httpStatus"] <= 599
            )
        )
        or isinstance(value["responseBytes"], bool)
        or not isinstance(value["responseBytes"], int)
        or not 0 <= value["responseBytes"] <= MAX_HTTP_RESPONSE_BYTES + 1
        or (
            value["arrayLength"] is not None
            and (
                isinstance(value["arrayLength"], bool)
                or not isinstance(value["arrayLength"], int)
                or value["arrayLength"] < 0
            )
        )
        or value["sanitizedErrorClass"] not in ERROR_CODES | {None}
        or (value["result"] == "PASS")
        != (value["sanitizedErrorClass"] is None)
    ):
        raise MetadataFreshnessError("request diagnostic values are invalid")
    for field in (
        "rateLimitLimit",
        "rateLimitRemaining",
        "rateLimitUsed",
        "rateLimitReset",
        "retryAfter",
    ):
        item = value[field]
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 0
        ):
            raise MetadataFreshnessError("request diagnostic rate limit is invalid")
    for field, maximum in (("contentType", 256), ("githubRequestId", 128)):
        item = value[field]
        if item is not None and (
            not isinstance(item, str)
            or not item
            or len(item) > maximum
            or any(ord(character) < 32 for character in item)
        ):
            raise MetadataFreshnessError(f"request diagnostic {field} is invalid")
    forbidden = re.compile(
        r"(?i)(authorization\s*:|cookie\s*:|bearer\s+[a-z0-9._~+/=-]{12,}|github_pat_|gh[pousr]_)",
    )
    if forbidden.search(json.dumps(value, ensure_ascii=False)):
        raise MetadataFreshnessError("request diagnostic contains credential material")
    return value


def _validate_comparison(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != COMPARISON_FIELDS:
        raise MetadataFreshnessError("snapshot comparison schema is not closed")
    if (
        value["schemaVersion"] != SCHEMA_VERSION
        or value["minimumIntervalSeconds"] != MINIMUM_SNAPSHOT_INTERVAL_SECONDS
        or value["result"] != "PASS"
        or value["partialAttemptCombination"] is not False
        or any(
            value[field] is not True
            for field in COMPARISON_FIELDS
            - {
                "schemaVersion",
                "minimumIntervalSeconds",
                "observedIntervalSeconds",
                "partialAttemptCombination",
                "result",
            }
        )
        or not isinstance(value["observedIntervalSeconds"], (int, float))
        or isinstance(value["observedIntervalSeconds"], bool)
        or value["observedIntervalSeconds"] < MINIMUM_SNAPSHOT_INTERVAL_SECONDS
    ):
        raise MetadataFreshnessError("snapshot comparison result is invalid")
    return value


def _workflow_run_identity(
    value: object,
    *,
    expected_run_id: int,
    expected_sha: str,
    expected_name: str,
    expected_path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetadataFreshnessError("workflow run metadata is invalid")
    repository = value.get("repository")
    if (
        isinstance(expected_run_id, bool)
        or not isinstance(expected_run_id, int)
        or expected_run_id <= 0
        or not isinstance(expected_sha, str)
        or not _SHA.fullmatch(expected_sha)
        or value.get("id") != expected_run_id
        or value.get("name") != expected_name
        or value.get("path") != expected_path
        or value.get("event") != "workflow_dispatch"
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or value.get("run_attempt") != 1
        or not isinstance(repository, dict)
        or repository.get("full_name") != REPOSITORY
        or value.get("head_branch") != "main"
        or value.get("head_sha") != expected_sha
    ):
        raise MetadataFreshnessError("workflow run authority binding differs")
    return value


def _select_artifact_metadata(
    value: object,
    *,
    expected_run_id: int,
    expected_sha: str,
    expected_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise MetadataFreshnessError("artifact listing metadata is invalid")
    artifacts = value["artifacts"]
    if value.get("total_count") != len(artifacts):
        raise MetadataFreshnessError("artifact listing is incomplete")
    matches = []
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("name") == expected_name:
            matches.append(artifact)
    if len(matches) != 1:
        raise MetadataFreshnessError("artifact authority cardinality differs")
    artifact = matches[0]
    artifact_id = artifact.get("id")
    workflow_run = artifact.get("workflow_run")
    expected_url = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/"
        f"{artifact_id}/zip"
    )
    if (
        isinstance(artifact_id, bool)
        or not isinstance(artifact_id, int)
        or artifact_id <= 0
        or not isinstance(workflow_run, dict)
        or workflow_run.get("id") != expected_run_id
        or workflow_run.get("head_sha") != expected_sha
        or artifact.get("expired") is not False
        or not isinstance(artifact.get("digest"), str)
        or not _DIGEST.fullmatch(artifact["digest"])
        or artifact.get("archive_download_url") != expected_url
    ):
        raise MetadataFreshnessError("artifact authority binding differs")
    return {
        "artifactId": artifact_id,
        "archiveDownloadUrl": expected_url,
        "digest": artifact["digest"],
        "name": expected_name,
        "workflowRunId": expected_run_id,
        "headSha": expected_sha,
    }


def validate_qualification_run_metadata(
    *,
    run_metadata: object,
    jobs_metadata: object,
    artifacts_metadata: object,
    expected_run_id: int,
    expected_sha: str,
) -> dict[str, Any]:
    """Validate Phase A producer, qualify-only job proof and exact artifact."""

    _workflow_run_identity(
        run_metadata,
        expected_run_id=expected_run_id,
        expected_sha=expected_sha,
        expected_name=QUALIFICATION_WORKFLOW_NAME,
        expected_path=QUALIFICATION_WORKFLOW_PATH,
    )
    if not isinstance(jobs_metadata, dict) or not isinstance(
        jobs_metadata.get("jobs"), list
    ):
        raise MetadataFreshnessError("qualification job metadata is invalid")
    jobs = jobs_metadata["jobs"]
    if jobs_metadata.get("total_count") != len(jobs):
        raise MetadataFreshnessError("qualification job listing is incomplete")
    phase_a = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "phase-a-qualification-evidence"
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
    ]
    publish = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "publish-immutable-prerelease"
        and job.get("status") == "completed"
        and job.get("conclusion") == "skipped"
    ]
    if len(phase_a) != 1 or len(publish) != 1:
        raise MetadataFreshnessError("qualification operation proof differs")
    return _select_artifact_metadata(
        artifacts_metadata,
        expected_run_id=expected_run_id,
        expected_sha=expected_sha,
        expected_name=f"release-qualification-{expected_run_id}",
    )


def validate_freshness_run_metadata(
    *,
    run_metadata: object,
    artifacts_metadata: object,
    expected_run_id: int,
    expected_sha: str,
) -> dict[str, Any]:
    """Validate the trusted Phase F producer and its unique artifact."""

    _workflow_run_identity(
        run_metadata,
        expected_run_id=expected_run_id,
        expected_sha=expected_sha,
        expected_name=FRESHNESS_WORKFLOW_NAME,
        expected_path=WORKFLOW_PATH,
    )
    return _select_artifact_metadata(
        artifacts_metadata,
        expected_run_id=expected_run_id,
        expected_sha=expected_sha,
        expected_name=f"release-metadata-freshness-{expected_run_id}",
    )


def _git_commit_range(repository_root: Path, range_start: str, candidate_sha: str) -> list[str]:
    if not _SHA.fullmatch(range_start) or not _SHA.fullmatch(candidate_sha):
        raise MetadataFreshnessError("release note commit range is invalid")
    ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "merge-base",
            "--is-ancestor",
            range_start,
            candidate_sha,
        ],
        check=False,
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise MetadataFreshnessError("release note range start is not an ancestor")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "rev-list",
            "--reverse",
            f"{range_start}..{candidate_sha}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise MetadataFreshnessError("unable to read exact release note commit range")
    commits = completed.stdout.splitlines()
    if not commits or not all(_SHA.fullmatch(commit) for commit in commits):
        raise MetadataFreshnessError("release note commit population is invalid")
    return commits


def _qualification_evidence_name(directory: Path, run_id: int) -> str:
    exact = f"release-qualification-{run_id}.json"
    if (directory / exact).is_file():
        return exact
    if (directory / "release-qualification.json").is_file():
        return "release-qualification.json"
    raise MetadataFreshnessError("qualification evidence file is missing")


def _load_qualification(
    directory: Path,
    *,
    identity: FreshnessRunIdentity | FreshnessExpectation,
) -> dict[str, Any]:
    if not directory.is_dir() or directory.is_symlink():
        raise MetadataFreshnessError("qualification directory is invalid")
    evidence_name = _qualification_evidence_name(directory, identity.qualification_run_id)
    expected_files = {
        evidence_name,
        "platform-qualification.json",
        "release-notes.json",
        "release-notes.md",
        "prepublication-materials.json",
        "installer-materials.tar",
        "deployment-contract.json",
    }
    actual_files = {path.name for path in directory.iterdir() if path.is_file()}
    if actual_files != expected_files or len(list(directory.iterdir())) != len(expected_files):
        raise MetadataFreshnessError("qualification artifact file set differs")
    for path in directory.iterdir():
        file_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise MetadataFreshnessError("qualification artifact contains an unsafe file")

    evidence_value, _ = _strict_json_file(
        directory / evidence_name, label="qualification evidence"
    )
    notes_value, notes_bytes = _strict_json_file(
        directory / "release-notes.json", label="qualified release notes"
    )
    prepublication, _ = _strict_json_file(
        directory / "prepublication-materials.json",
        label="prepublication materials",
    )
    if not isinstance(evidence_value, dict) or not isinstance(prepublication, dict):
        raise MetadataFreshnessError("qualification evidence shape is invalid")
    notes = validate_release_notes(notes_value)
    markdown_path = directory / "release-notes.md"
    markdown_stat = markdown_path.lstat()
    if (
        markdown_path.is_symlink()
        or not stat.S_ISREG(markdown_stat.st_mode)
        or markdown_stat.st_nlink != 1
        or markdown_stat.st_size > MAX_ARTIFACT_MEMBER_BYTES
    ):
        raise MetadataFreshnessError("qualified release notes Markdown is unsafe")
    markdown_bytes = markdown_path.read_bytes()

    run = evidence_value.get("run")
    workflow = evidence_value.get("workflow")
    release_notes = evidence_value.get("release_notes")
    if (
        evidence_value.get("schema") != "animemo.release-qualification/v2"
        or evidence_value.get("repository") != REPOSITORY
        or evidence_value.get("candidate_sha") != identity.candidate_sha
        or evidence_value.get("release_tag") != notes["context"]["release_tag"]
        or not isinstance(run, dict)
        or run.get("id") != str(identity.qualification_run_id)
        or run.get("attempt") != 1
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(workflow, dict)
        or workflow.get("name") != "Release Producer"
        or workflow.get("path") != ".github/workflows/release.yml"
        or workflow.get("sha") != identity.candidate_sha
        or not isinstance(release_notes, dict)
        or release_notes.get("snapshot_identity") != notes["identity"]
        or release_notes.get("markdown_sha256") != _digest_bytes(markdown_bytes)
    ):
        raise MetadataFreshnessError("qualification evidence binding differs")
    if (
        prepublication.get("candidateSha") != identity.candidate_sha
        or prepublication.get("candidateTreeSha") != identity.candidate_tree
        or notes["context"]["candidate_sha"] != identity.candidate_sha
    ):
        raise MetadataFreshnessError("qualification source identity differs")
    if render_release_notes(notes).encode("utf-8") != markdown_bytes:
        raise MetadataFreshnessError("qualified Markdown differs from canonical rendering")
    return {
        "notes": notes,
        "notesBytes": notes_bytes,
        "markdownBytes": markdown_bytes,
        "jsonSha": _digest_bytes(notes_bytes),
        "markdownSha": _digest_bytes(markdown_bytes),
        "population": len(notes["pulls"]),
        "releaseTag": notes["context"]["release_tag"],
        "configurationIdentity": notes["configuration"]["identity"],
        "rendererIdentity": notes["configuration"]["renderer"],
    }


@dataclass(frozen=True)
class _Snapshot:
    input_bytes: bytes
    json_bytes: bytes
    markdown_bytes: bytes
    value: dict[str, Any]
    completed_at: datetime


def _complete_snapshot(
    *,
    repository_root: Path,
    context: Mapping[str, Any],
    source: AssociatedPullSource,
    clock: FreshnessClock,
    snapshot_label: str,
    attempt: int,
    diagnostics: list[dict[str, Any]],
    commit_loader: Callable[[Path, str, str], list[str]],
) -> _Snapshot:
    from scripts.release_notes_snapshot import (
        SnapshotCollectionError,
        collect_pull_metadata,
    )

    commits = commit_loader(
        repository_root,
        str(context["comparison_base_sha"]),
        str(context["candidate_sha"]),
    )

    def query(repository: str, commit: str) -> list[dict[str, Any]]:
        response = source.fetch(
            repository=repository,
            commit=commit,
            snapshot=snapshot_label,
            attempt=attempt,
        )
        diagnostics.append(_validate_diagnostic(response.diagnostic))
        if response.error_code is not None:
            raise MetadataFreshnessError(
                f"associated-pulls request failed with {response.error_code}",
                code=response.error_code,
                retry_after_seconds=response.retry_after_seconds,
                retryable=response.retryable,
            )
        if response.pulls is None:
            raise MetadataFreshnessError(
                "associated-pulls response is missing",
                code="RESPONSE_SHAPE_INVALID",
            )
        return response.pulls

    try:
        pulls = collect_pull_metadata(
            repository=REPOSITORY,
            commits=commits,
            query=query,
        )
        input_value = {"context": dict(context), "pulls": pulls}
        snapshot_value = build_release_notes(context=context, pulls=pulls)
        markdown = render_release_notes(snapshot_value)
    except MetadataFreshnessError:
        raise
    except (SnapshotCollectionError, ReleaseNotesError, KeyError, TypeError) as error:
        raise MetadataFreshnessError(
            "release notes contract failed",
            code="RELEASE_NOTES_CONTRACT_FAILURE",
        ) from error
    return _Snapshot(
        input_bytes=_json_bytes(input_value),
        json_bytes=_json_bytes(snapshot_value),
        markdown_bytes=markdown.encode("utf-8"),
        value=snapshot_value,
        completed_at=clock.now(),
    )


def _collect_with_retry(
    *,
    repository_root: Path,
    context: Mapping[str, Any],
    source: AssociatedPullSource,
    clock: FreshnessClock,
    snapshot_label: str,
    diagnostics: list[dict[str, Any]],
    commit_loader: Callable[[Path, str, str], list[str]],
) -> _Snapshot:
    for attempt in range(1, MAX_COMPLETE_ATTEMPTS + 1):
        try:
            return _complete_snapshot(
                repository_root=repository_root,
                context=context,
                source=source,
                clock=clock,
                snapshot_label=snapshot_label,
                attempt=attempt,
                diagnostics=diagnostics,
                commit_loader=commit_loader,
            )
        except MetadataFreshnessError as error:
            if not error.retryable or attempt == MAX_COMPLETE_ATTEMPTS:
                raise
            delay = max(5 * (2 ** (attempt - 1)), error.retry_after_seconds or 0)
            clock.sleep(delay)
    raise AssertionError("bounded retry loop did not terminate")


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    unsigned = dict(receipt)
    unsigned.pop("receiptDigest", None)
    return _digest_bytes(_json_bytes(unsigned))


def _write_exclusive(path: Path, value: bytes) -> None:
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(path, 0o600)


def _load_candidate_acceptance_receipt(
    path: Path,
    *,
    identity: FreshnessRunIdentity,
    current_time: datetime,
) -> tuple[dict[str, Any], bytes]:
    # Imported lazily because candidate.py also reuses the Qualification
    # metadata validator from this module.  The contract remains one-way at
    # runtime: Freshness consumes an already-created receipt and never mints it.
    from .candidate import (
        CandidateContractError,
        canonical_json_bytes,
        validate_aggregate_receipt,
    )

    value, encoded = _strict_json_file(
        path,
        label="candidate acceptance receipt",
    )
    try:
        receipt = validate_aggregate_receipt(value)
    except CandidateContractError as error:
        raise MetadataFreshnessError(
            "candidate acceptance receipt is invalid",
            code=error.code,
        ) from error
    if canonical_json_bytes(receipt) != encoded:
        raise MetadataFreshnessError(
            "candidate acceptance receipt JSON is not canonical"
        )
    if (
        receipt["result"] != "PASS"
        or receipt["all_profiles_pass"] is not True
        or receipt["qualification_run_id"] != identity.qualification_run_id
        or receipt["qualification_run_attempt"] != 1
        or receipt["source_sha"] != identity.candidate_sha
        or receipt["source_tree"] != identity.candidate_tree
        or receipt["candidate_version"] != identity.candidate_version
    ):
        raise MetadataFreshnessError(
            "candidate acceptance receipt authority binding differs"
        )
    if _digest_bytes(encoded) != identity.candidate_acceptance_receipt_sha256:
        raise MetadataFreshnessError(
            "candidate acceptance receipt digest differs"
        )
    completed_at = _parse_timestamp(
        receipt["completed_at"], label="candidate acceptance completion timestamp"
    )
    if completed_at > current_time.astimezone(timezone.utc):
        raise MetadataFreshnessError(
            "candidate acceptance completion time is in the future"
        )
    return receipt, encoded


def collect_metadata_freshness(
    *,
    identity: FreshnessRunIdentity,
    qualification_directory: Path,
    output_directory: Path,
    repository_root: Path,
    source: AssociatedPullSource,
    candidate_acceptance_receipt: Path,
    clock: FreshnessClock | None = None,
    commit_loader: Callable[[Path, str, str], list[str]] = _git_commit_range,
) -> dict[str, Any]:
    """Create one exact ten-file trusted freshness transport."""

    identity.validate()
    runtime_clock = clock or SystemFreshnessClock()
    if output_directory.exists() or output_directory.is_symlink():
        raise MetadataFreshnessError("freshness output directory must not exist")
    if not repository_root.is_dir() or repository_root.is_symlink():
        raise MetadataFreshnessError("repository root is invalid")
    _, candidate_receipt_bytes = _load_candidate_acceptance_receipt(
        candidate_acceptance_receipt,
        identity=identity,
        current_time=runtime_clock.now(),
    )
    qualification = _load_qualification(
        qualification_directory,
        identity=identity,
    )
    context = qualification["notes"]["context"]
    diagnostics: list[dict[str, Any]] = []
    snapshot_a = _collect_with_retry(
        repository_root=repository_root,
        context=context,
        source=source,
        clock=runtime_clock,
        snapshot_label="A",
        diagnostics=diagnostics,
        commit_loader=commit_loader,
    )
    runtime_clock.sleep(MINIMUM_SNAPSHOT_INTERVAL_SECONDS)
    snapshot_b = _collect_with_retry(
        repository_root=repository_root,
        context=context,
        source=source,
        clock=runtime_clock,
        snapshot_label="B",
        diagnostics=diagnostics,
        commit_loader=commit_loader,
    )
    interval = (snapshot_b.completed_at - snapshot_a.completed_at).total_seconds()
    if interval < MINIMUM_SNAPSHOT_INTERVAL_SECONDS:
        raise MetadataFreshnessError("metadata snapshot interval is shorter than 60 seconds")
    if (
        snapshot_a.input_bytes != snapshot_b.input_bytes
        or snapshot_a.json_bytes != snapshot_b.json_bytes
        or snapshot_a.markdown_bytes != snapshot_b.markdown_bytes
        or snapshot_a.value["identity"] != snapshot_b.value["identity"]
    ):
        raise MetadataFreshnessError(
            "trusted metadata snapshots differ",
            code="ACTUAL_METADATA_DRIFT",
        )
    if (
        snapshot_a.json_bytes != qualification["notesBytes"]
        or snapshot_b.json_bytes != qualification["notesBytes"]
        or snapshot_a.markdown_bytes != qualification["markdownBytes"]
        or snapshot_b.markdown_bytes != qualification["markdownBytes"]
        or snapshot_a.value["identity"] != qualification["notes"]["identity"]
        or len(snapshot_a.value["pulls"]) != qualification["population"]
    ):
        raise MetadataFreshnessError(
            "current metadata differs from qualification",
            code="ACTUAL_METADATA_DRIFT",
        )

    completed_at = runtime_clock.now()
    comparison = {
        "schemaVersion": SCHEMA_VERSION,
        "inputByteIdentical": True,
        "snapshotJsonByteIdentical": True,
        "markdownByteIdentical": True,
        "identityIdentical": True,
        "qualificationIdentityMatch": True,
        "qualificationMarkdownMatch": True,
        "qualificationJsonMatch": True,
        "populationMatch": True,
        "minimumIntervalSeconds": MINIMUM_SNAPSHOT_INTERVAL_SECONDS,
        "observedIntervalSeconds": interval,
        "partialAttemptCombination": False,
        "result": "PASS",
    }
    _validate_comparison(comparison)
    request_diagnostics = {
        "schemaVersion": SCHEMA_VERSION,
        "endpointTemplate": ENDPOINT_TEMPLATE,
        "requests": diagnostics,
    }
    receipt: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "workflowRunId": identity.workflow_run_id,
        "workflowAttempt": identity.workflow_attempt,
        "workflowPath": identity.workflow_path,
        "workflowSha": identity.workflow_sha,
        "candidateSha": identity.candidate_sha,
        "candidateTree": identity.candidate_tree,
        "qualificationRunId": identity.qualification_run_id,
        "qualificationArtifactId": identity.qualification_artifact_id,
        "candidateAcceptanceReceiptSha256": (
            identity.candidate_acceptance_receipt_sha256
        ),
        "candidateVersion": identity.candidate_version,
        "qualifiedReleaseTag": qualification["releaseTag"],
        "qualifiedReleaseNotesIdentity": qualification["notes"]["identity"],
        "qualifiedMarkdownSha": qualification["markdownSha"],
        "qualifiedJsonSha": qualification["jsonSha"],
        "qualifiedConfigurationIdentity": qualification["configurationIdentity"],
        "qualifiedRendererIdentity": qualification["rendererIdentity"],
        "snapshotCount": 2,
        "artifactFileCount": len(ARTIFACT_FILES),
        "snapshotACompletedAt": _iso8601(snapshot_a.completed_at),
        "snapshotBCompletedAt": _iso8601(snapshot_b.completed_at),
        "snapshotIntervalSeconds": interval,
        "snapshotAIdentity": snapshot_a.value["identity"],
        "snapshotBIdentity": snapshot_b.value["identity"],
        "snapshotAMarkdownSha": _digest_bytes(snapshot_a.markdown_bytes),
        "snapshotBMarkdownSha": _digest_bytes(snapshot_b.markdown_bytes),
        "snapshotAJsonSha": _digest_bytes(snapshot_a.json_bytes),
        "snapshotBJsonSha": _digest_bytes(snapshot_b.json_bytes),
        "population": len(snapshot_a.value["pulls"]),
        "conflicts": 0,
        "unclassified": 0,
        "duplicates": 0,
        "requestFailureCount": sum(
            1 for item in diagnostics if item["result"] == "FAIL"
        ),
        "result": "PASS",
        "completedAt": _iso8601(completed_at),
        "receiptDigest": "",
    }
    receipt["receiptDigest"] = _receipt_digest(receipt)
    _validate_receipt(receipt)

    output_directory.mkdir(parents=False, mode=0o700)
    outputs = {
        "candidate-acceptance-receipt.json": candidate_receipt_bytes,
        "metadata-freshness.json": _json_bytes(receipt),
        "snapshot-a-input.json": snapshot_a.input_bytes,
        "snapshot-a.json": snapshot_a.json_bytes,
        "snapshot-a.md": snapshot_a.markdown_bytes,
        "snapshot-b-input.json": snapshot_b.input_bytes,
        "snapshot-b.json": snapshot_b.json_bytes,
        "snapshot-b.md": snapshot_b.markdown_bytes,
        "snapshot-comparison.json": _json_bytes(comparison),
        "request-diagnostics.json": _json_bytes(request_diagnostics),
    }
    try:
        for name, value in outputs.items():
            _write_exclusive(output_directory / name, value)
    except Exception:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise
    return {
        "status": "PASS",
        "artifactFileCount": len(outputs),
        "workflowRunId": identity.workflow_run_id,
        "qualificationRunId": identity.qualification_run_id,
        "candidateSha": identity.candidate_sha,
        "receiptDigest": receipt["receiptDigest"],
    }


def _validate_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        raise MetadataFreshnessError("metadata freshness receipt schema is not closed")
    if value["schemaVersion"] != SCHEMA_VERSION or value["result"] != "PASS":
        raise MetadataFreshnessError("metadata freshness receipt result is invalid")
    if value["workflowAttempt"] != 1 or value["workflowPath"] != WORKFLOW_PATH:
        raise MetadataFreshnessError("metadata freshness producer identity is invalid")
    if not isinstance(value["qualifiedReleaseTag"], str) or not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*",
        value["qualifiedReleaseTag"],
    ):
        raise MetadataFreshnessError("metadata freshness release tag is invalid")
    if (
        not isinstance(value["candidateVersion"], str)
        or not re.fullmatch(
            r"v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*",
            value["candidateVersion"],
        )
        or value["candidateVersion"] != value["qualifiedReleaseTag"]
    ):
        raise MetadataFreshnessError("metadata freshness candidate version differs")
    for field in ("workflowSha", "candidateSha", "candidateTree"):
        if not isinstance(value[field], str) or not _SHA.fullmatch(value[field]):
            raise MetadataFreshnessError(f"metadata freshness {field} is invalid")
    if value["workflowSha"] != value["candidateSha"]:
        raise MetadataFreshnessError("metadata freshness workflow SHA differs")
    for field in (
        "qualifiedReleaseNotesIdentity",
        "qualifiedMarkdownSha",
        "qualifiedJsonSha",
        "qualifiedConfigurationIdentity",
        "snapshotAIdentity",
        "snapshotBIdentity",
        "snapshotAMarkdownSha",
        "snapshotBMarkdownSha",
        "snapshotAJsonSha",
        "snapshotBJsonSha",
        "candidateAcceptanceReceiptSha256",
        "receiptDigest",
    ):
        if not isinstance(value[field], str) or not _DIGEST.fullmatch(value[field]):
            raise MetadataFreshnessError(f"metadata freshness {field} is invalid")
    if value["qualifiedRendererIdentity"] != "animemo.release-notes.renderer/v2":
        raise MetadataFreshnessError("metadata freshness renderer differs")
    for field in (
        "workflowRunId",
        "qualificationRunId",
        "qualificationArtifactId",
        "snapshotCount",
        "artifactFileCount",
        "population",
        "conflicts",
        "unclassified",
        "duplicates",
        "requestFailureCount",
    ):
        if isinstance(value[field], bool) or not isinstance(value[field], int) or value[field] < 0:
            raise MetadataFreshnessError(f"metadata freshness {field} is invalid")
    if (
        value["workflowRunId"] <= 0
        or value["qualificationRunId"] <= 0
        or value["qualificationArtifactId"] <= 0
        or value["snapshotCount"] != 2
        or value["artifactFileCount"] != len(ARTIFACT_FILES)
        or value["population"] <= 0
        or value["conflicts"] != 0
        or value["unclassified"] != 0
        or value["duplicates"] != 0
    ):
        raise MetadataFreshnessError("metadata freshness closed counts differ")
    if not isinstance(value["snapshotIntervalSeconds"], (int, float)) or isinstance(
        value["snapshotIntervalSeconds"], bool
    ):
        raise MetadataFreshnessError("metadata freshness interval is invalid")
    snapshot_a = _parse_timestamp(value["snapshotACompletedAt"], label="snapshot A timestamp")
    snapshot_b = _parse_timestamp(value["snapshotBCompletedAt"], label="snapshot B timestamp")
    completed = _parse_timestamp(value["completedAt"], label="freshness completion timestamp")
    observed = (snapshot_b - snapshot_a).total_seconds()
    if (
        observed < MINIMUM_SNAPSHOT_INTERVAL_SECONDS
        or value["snapshotIntervalSeconds"] != observed
        or completed < snapshot_b
        or (completed - snapshot_b).total_seconds() > 60
    ):
        raise MetadataFreshnessError("metadata freshness timestamp ordering differs")
    if value["receiptDigest"] != _receipt_digest(value):
        raise MetadataFreshnessError("metadata freshness receipt digest differs")
    return value


def extract_metadata_freshness_artifact(
    archive_path: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    """Safely extract the exact nine-file freshness transport."""

    if not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256):
        raise MetadataFreshnessError("freshness artifact digest is invalid")
    if destination.exists() or destination.is_symlink():
        raise MetadataFreshnessError("freshness artifact destination must not exist")
    file_stat = archive_path.lstat()
    if (
        archive_path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or file_stat.st_size <= 0
        or file_stat.st_size > MAX_ARTIFACT_BYTES
    ):
        raise MetadataFreshnessError("freshness artifact archive is unsafe")
    archive_bytes = archive_path.read_bytes()
    if _digest_bytes(archive_bytes) != expected_sha256:
        raise MetadataFreshnessError("freshness artifact digest differs")
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(entries) != len(ARTIFACT_FILES)
                or len(names) != len(set(names))
                or set(names) != ARTIFACT_FILES
            ):
                raise MetadataFreshnessError("freshness artifact file set differs")
            total = 0
            for entry in entries:
                unix_mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if (
                    entry.filename != Path(entry.filename).name
                    or entry.is_dir()
                    or entry.flag_bits & 0x1
                    or file_type not in {0, stat.S_IFREG}
                    or entry.file_size <= 0
                    or entry.file_size > MAX_ARTIFACT_MEMBER_BYTES
                ):
                    raise MetadataFreshnessError("freshness artifact ZIP entry is unsafe")
                total += entry.file_size
            if total > MAX_ARTIFACT_BYTES:
                raise MetadataFreshnessError("freshness artifact is too large")
            destination.mkdir(parents=False, mode=0o700)
            for entry in sorted(entries, key=lambda item: item.filename):
                target = destination / entry.filename
                with archive.open(entry, mode="r") as source, target.open("xb") as output:
                    copied = 0
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > entry.file_size:
                            raise MetadataFreshnessError("freshness ZIP member grew while reading")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if copied != entry.file_size:
                    raise MetadataFreshnessError("freshness ZIP member size differs")
                os.chmod(target, 0o600)
    except MetadataFreshnessError:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile) as error:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise MetadataFreshnessError("freshness artifact extraction failed") from error
    return {"status": "PASS", "fileCount": len(ARTIFACT_FILES)}


def verify_metadata_freshness_artifact(
    *,
    artifact_directory: Path,
    qualification_directory: Path,
    expectation: FreshnessExpectation,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Verify producer, bindings, exact bytes, filesystem safety and TTL."""

    expectation.validate()
    if not artifact_directory.is_dir() or artifact_directory.is_symlink():
        raise MetadataFreshnessError("freshness artifact directory is invalid")
    entries = list(artifact_directory.iterdir())
    if {path.name for path in entries} != ARTIFACT_FILES or len(entries) != len(
        ARTIFACT_FILES
    ):
        raise MetadataFreshnessError("freshness artifact file set differs")
    for path in entries:
        file_stat = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_size <= 0
            or file_stat.st_size > MAX_ARTIFACT_MEMBER_BYTES
        ):
            raise MetadataFreshnessError("freshness artifact contains an unsafe file")
    receipt_value, _ = _strict_json_file(
        artifact_directory / "metadata-freshness.json",
        label="metadata freshness receipt",
    )
    receipt = _validate_receipt(receipt_value)
    if (
        receipt["workflowRunId"] != expectation.workflow_run_id
        or receipt["workflowSha"] != expectation.candidate_sha
        or receipt["candidateSha"] != expectation.candidate_sha
        or receipt["candidateTree"] != expectation.candidate_tree
        or receipt["qualificationRunId"] != expectation.qualification_run_id
        or receipt["qualificationArtifactId"]
        != expectation.qualification_artifact_id
        or receipt["candidateAcceptanceReceiptSha256"]
        != expectation.candidate_acceptance_receipt_sha256
        or receipt["candidateVersion"] != expectation.candidate_version
    ):
        raise MetadataFreshnessError("freshness artifact authority binding differs")

    _, candidate_receipt_bytes = _load_candidate_acceptance_receipt(
        artifact_directory / "candidate-acceptance-receipt.json",
        identity=FreshnessRunIdentity(
            workflow_run_id=expectation.workflow_run_id,
            workflow_attempt=1,
            workflow_path=WORKFLOW_PATH,
            workflow_sha=expectation.candidate_sha,
            candidate_sha=expectation.candidate_sha,
            candidate_tree=expectation.candidate_tree,
            qualification_run_id=expectation.qualification_run_id,
            qualification_artifact_id=expectation.qualification_artifact_id,
            candidate_acceptance_receipt_sha256=(
                expectation.candidate_acceptance_receipt_sha256
            ),
            candidate_version=expectation.candidate_version,
        ),
        current_time=verified_at or datetime.now(timezone.utc),
    )
    if _digest_bytes(candidate_receipt_bytes) != receipt[
        "candidateAcceptanceReceiptSha256"
    ]:
        raise MetadataFreshnessError(
            "freshness candidate acceptance receipt binding differs"
        )

    qualification = _load_qualification(
        qualification_directory,
        identity=expectation,
    )
    input_a = (artifact_directory / "snapshot-a-input.json").read_bytes()
    input_b = (artifact_directory / "snapshot-b-input.json").read_bytes()
    json_a = (artifact_directory / "snapshot-a.json").read_bytes()
    json_b = (artifact_directory / "snapshot-b.json").read_bytes()
    markdown_a = (artifact_directory / "snapshot-a.md").read_bytes()
    markdown_b = (artifact_directory / "snapshot-b.md").read_bytes()
    snapshot_a = validate_release_notes(
        _strict_json_bytes(json_a, label="snapshot A")
    )
    snapshot_b = validate_release_notes(
        _strict_json_bytes(json_b, label="snapshot B")
    )
    input_value_a = _strict_json_bytes(input_a, label="snapshot A input")
    input_value_b = _strict_json_bytes(input_b, label="snapshot B input")
    expected_input_pulls = [
        {
            field: pull[field]
            for field in ("number", "title", "source_identity", "labels")
        }
        for pull in snapshot_a["pulls"]
    ]
    if (
        not isinstance(input_value_a, dict)
        or set(input_value_a) != {"context", "pulls"}
        or input_value_a != input_value_b
        or input_value_a["context"] != snapshot_a["context"]
        or input_value_a["pulls"] != expected_input_pulls
    ):
        raise MetadataFreshnessError("freshness input snapshot contract differs")
    if input_a != input_b:
        raise MetadataFreshnessError("freshness input metadata differs")
    if json_a != json_b or snapshot_a != snapshot_b:
        raise MetadataFreshnessError("freshness snapshot JSON differs")
    if markdown_a != markdown_b:
        raise MetadataFreshnessError("freshness snapshot Markdown differs")
    if (
        json_a != qualification["notesBytes"]
        or markdown_a != qualification["markdownBytes"]
        or snapshot_a["identity"] != qualification["notes"]["identity"]
        or receipt["qualifiedJsonSha"] != qualification["jsonSha"]
        or receipt["qualifiedMarkdownSha"] != qualification["markdownSha"]
        or receipt["qualifiedReleaseNotesIdentity"]
        != qualification["notes"]["identity"]
        or receipt["qualifiedConfigurationIdentity"]
        != qualification["configurationIdentity"]
        or receipt["qualifiedRendererIdentity"] != qualification["rendererIdentity"]
        or receipt["qualifiedReleaseTag"] != qualification["releaseTag"]
    ):
        raise MetadataFreshnessError("freshness artifact differs from qualification")
    if (
        receipt["snapshotAIdentity"] != snapshot_a["identity"]
        or receipt["snapshotBIdentity"] != snapshot_b["identity"]
        or receipt["snapshotAJsonSha"] != _digest_bytes(json_a)
        or receipt["snapshotBJsonSha"] != _digest_bytes(json_b)
        or receipt["snapshotAMarkdownSha"] != _digest_bytes(markdown_a)
        or receipt["snapshotBMarkdownSha"] != _digest_bytes(markdown_b)
        or receipt["population"] != len(snapshot_a["pulls"])
    ):
        raise MetadataFreshnessError("freshness receipt snapshot binding differs")

    comparison_value, _ = _strict_json_file(
        artifact_directory / "snapshot-comparison.json",
        label="snapshot comparison",
    )
    comparison = _validate_comparison(comparison_value)
    if comparison["observedIntervalSeconds"] != receipt["snapshotIntervalSeconds"]:
        raise MetadataFreshnessError("snapshot comparison interval differs")
    diagnostics_value, _ = _strict_json_file(
        artifact_directory / "request-diagnostics.json",
        label="request diagnostics",
    )
    if (
        not isinstance(diagnostics_value, dict)
        or set(diagnostics_value) != {"schemaVersion", "endpointTemplate", "requests"}
        or diagnostics_value.get("schemaVersion") != SCHEMA_VERSION
        or diagnostics_value.get("endpointTemplate") != ENDPOINT_TEMPLATE
        or not isinstance(diagnostics_value.get("requests"), list)
    ):
        raise MetadataFreshnessError("request diagnostics envelope is invalid")
    diagnostics = [
        _validate_diagnostic(item) for item in diagnostics_value["requests"]
    ]
    if receipt["requestFailureCount"] != sum(
        1 for item in diagnostics if item["result"] == "FAIL"
    ):
        raise MetadataFreshnessError("request failure count differs")

    verification_time = verified_at or datetime.now(timezone.utc)
    if verification_time.tzinfo is None:
        raise MetadataFreshnessError("freshness verification time must be timezone-aware")
    completed_at = _parse_timestamp(
        receipt["completedAt"], label="freshness completion timestamp"
    )
    age = (verification_time.astimezone(timezone.utc) - completed_at).total_seconds()
    if age < 0:
        raise MetadataFreshnessError("freshness completion time is in the future")
    if age > FRESHNESS_TTL_SECONDS:
        raise MetadataFreshnessError(
            "metadata freshness has expired",
            code="METADATA_FRESHNESS_EXPIRED",
        )
    return {
        "status": "PASS",
        "workflowRunId": expectation.workflow_run_id,
        "qualificationRunId": expectation.qualification_run_id,
        "candidateSha": expectation.candidate_sha,
        "candidateTree": expectation.candidate_tree,
        "candidateAcceptanceReceiptSha256": (
            expectation.candidate_acceptance_receipt_sha256
        ),
        "candidateVersion": expectation.candidate_version,
        "releaseTag": receipt["qualifiedReleaseTag"],
        "snapshotCount": receipt["snapshotCount"],
        "snapshotIntervalSeconds": receipt["snapshotIntervalSeconds"],
        "freshnessAgeSeconds": age,
        "freshnessTtlSeconds": FRESHNESS_TTL_SECONDS,
        "receiptDigest": receipt["receiptDigest"],
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
        "artifactRole": "TRANSPORT_AND_QUALIFICATION_EVIDENCE",
    }
