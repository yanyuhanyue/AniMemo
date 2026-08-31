"""Read-only controller authority verification for one exact immutable RC."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from release.acquisition import validate_attestation_sidecar
from release.candidate import (
    aggregate_receipt_digest,
    canonical_json_bytes,
    sha256_bytes,
    validate_aggregate_receipt,
    validate_candidate_input,
    validate_verified_candidate,
)
from release.contract import (
    deployment_contract_digest,
    validate_deployment_contract,
    validate_manifest,
)
from release.mirror import (
    MIRROR_ORIGIN,
    MIRROR_PATH_PREFIX,
    MIRROR_RECEIPT_NAME,
    OfficialMirrorPublicReader,
    load_mirror_receipt_bytes,
    mirror_release_assets,
)
from release.portable import MAX_PORTABLE_TOTAL_BYTES, inspect_portable_archive
from release.publication import (
    declared_publication_assets,
    validate_publication_plan,
    verify_post_publish,
)
from release.publication_evidence import (
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    GITHUB_RELEASE_PREDICATE_TYPE,
    OWNER_ID,
    REPOSITORY_ID,
    close_github_release_publication,
)
from release.publication_transaction import (
    _candidate_authority,
    _normalize_authority_plan,
    validate_ledger,
)
from updater import __version__ as updater_version

_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "finalRepoHead",
        "finalRepoTree",
        "qualificationRunId",
        "candidateInputSha256",
        "verifiedCandidateIdentity",
        "candidateAggregateReceiptSha256",
        "releaseTag",
        "releaseVersion",
        "releaseChannel",
        "publishRunId",
        "mirrorRunId",
        "publicationIdentity",
        "apiDigest",
        "webDigest",
        "publishRebuildCount",
        "globalMutationFreeze",
    }
)
_PROHIBITED_ACTIONS = frozenset(
    {
        "PUBLIC_DNS_MUTATION",
        "CLOUDFLARE_CONTROL_PLANE",
        "FIREWALL_MUTATION",
        "OPENRESTY_MUTATION",
        "V1_0_STOP_DELETE_OR_REPLACE",
        "STABLE_PROMOTION_OR_TAG",
    }
)
_REPOSITORY = "yanyuhanyue/AniMemo"
_RELEASE_WORKFLOW = ".github/workflows/release.yml"
_MIRROR_WORKFLOW = ".github/workflows/release-mirror.yml"
_RELEASE_METADATA_FILES = frozenset(
    {
        "release-qualification.json",
        "release-notes.json",
        "release-notes.md",
        "candidate-input.json",
        "verified-candidate.json",
        "candidate-acceptance-receipt.json",
        "publish-candidate-plan.json",
        "publication-plan.json",
        "publication-transaction-ledger.json",
        "post-publish-verification.json",
        "portable-build-receipt.json",
        "portable-pipeline-authority.json",
        "release-attestation-acquisition-receipt.json",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_COMMIT = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_RC_TAG = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-rc\.[1-9][0-9]*$",
    re.ASCII,
)
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_COMMAND_BYTES = 4 * 1024 * 1024
_MAX_GHCR_TOKEN_BYTES = 64 * 1024
_MAX_GH_EXECUTABLE_BYTES = 256 * 1024 * 1024
_GHCR_TOKEN_URL = "https://ghcr.io/token"
_GHCR_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)
_REGISTRY_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", re.ASCII)
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9._~+/-]+={0,2}$", re.ASCII)
_PUBLIC_IDENTITY_FIELDS = frozenset(
    {
        "schema",
        "finalRepoHead",
        "finalRepoTree",
        "qualificationRunId",
        "qualificationRunAttempt",
        "qualificationArtifacts",
        "candidateInputSha256",
        "verifiedCandidateIdentity",
        "releaseTag",
        "releaseVersion",
        "releaseChannel",
        "continuationRoot",
        "evidenceRoot",
        "sealRoot",
        "producerAuthority",
        "releaseStageRoot",
    }
)


class ControllerReleaseAuthorityError(RuntimeError):
    """Stable fail-closed release authority error."""


@dataclass(frozen=True)
class ReleaseAuthorityEvidence:
    final_repo_head: str
    final_repo_tree: str
    qualification_run_id: int
    candidate_input_sha256: str
    verified_candidate_identity: str
    candidate_aggregate_receipt_sha256: str
    release_tag: str
    release_version: str
    release_channel: str
    publish_run_id: int
    mirror_run_id: int
    publication_identity: str
    api_digest: str
    web_digest: str
    publish_rebuild_count: int
    global_mutation_freeze: bool
    publish_result: str
    mirror_result: str
    remote_readback_result: str
    zero_rebuild: bool


class ReleaseAuthorityObserver(Protocol):
    """Read-only system boundary used to collect release observations."""

    def observe(
        self,
        *,
        authority_request: dict[str, object],
        expected_public_identity: dict[str, object],
        candidate_result: dict[str, object],
    ) -> ReleaseAuthorityEvidence: ...


def _reject(code: str = "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID") -> None:
    raise ControllerReleaseAuthorityError(code)


def _valid_authority_request(value: dict[str, object]) -> bool:
    tag = value.get("releaseTag")
    return (
        value.get("schema") == "animemo.rc19-release-stage-authority-request/v1"
        and isinstance(value.get("finalRepoHead"), str)
        and _COMMIT.fullmatch(value["finalRepoHead"]) is not None
        and isinstance(value.get("finalRepoTree"), str)
        and _COMMIT.fullmatch(value["finalRepoTree"]) is not None
        and type(value.get("qualificationRunId")) is int
        and value["qualificationRunId"] > 0
        and all(
            isinstance(value.get(name), str)
            and _SHA256.fullmatch(value[name]) is not None
            for name in (
                "candidateInputSha256",
                "verifiedCandidateIdentity",
                "candidateAggregateReceiptSha256",
                "publicationIdentity",
                "apiDigest",
                "webDigest",
            )
        )
        and isinstance(tag, str)
        and _RC_TAG.fullmatch(tag) is not None
        and value.get("releaseVersion") == tag.removeprefix("v")
        and value.get("releaseChannel") == "rc"
        and type(value.get("publishRunId")) is int
        and value["publishRunId"] > 0
        and type(value.get("mirrorRunId")) is int
        and value["mirrorRunId"] > 0
        and value.get("publishRebuildCount") == 0
        and type(value.get("publishRebuildCount")) is int
        and value.get("globalMutationFreeze") is False
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _json_bytes(value: bytes) -> dict[str, Any]:
    if not isinstance(value, bytes) or not 1 <= len(value) <= _MAX_JSON_BYTES:
        _reject()
    try:
        decoded = json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _reject(),
        )
    except ControllerReleaseAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControllerReleaseAuthorityError(
            "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
        ) from error
    if type(decoded) is not dict:
        _reject()
    return decoded


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _regular_file_stat(metadata: os.stat_result, *, minimum: int, maximum: int) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and minimum <= metadata.st_size <= maximum
    )


class _VerifiedRegularFile:
    """Bind one local path to one unchanged regular opened file handle."""

    def __init__(self, path: Path, *, minimum: int, maximum: int) -> None:
        self._path = path
        self._minimum = minimum
        self._maximum = maximum
        self._stream: Any = None
        self._opened: os.stat_result | None = None

    def __enter__(self) -> tuple[Any, int]:
        stream = None
        try:
            metadata = self._path.lstat()
            if bool(
                getattr(self._path, "is_junction", lambda: False)()
            ) or not _regular_file_stat(
                metadata, minimum=self._minimum, maximum=self._maximum
            ):
                _reject()
            stream = self._path.open("rb")
            opened = os.fstat(stream.fileno())
            if not _regular_file_stat(
                opened, minimum=self._minimum, maximum=self._maximum
            ) or _file_identity(metadata) != _file_identity(opened):
                _reject()
        except ControllerReleaseAuthorityError:
            if stream is not None:
                stream.close()
            raise
        except OSError as error:
            if stream is not None:
                stream.close()
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            ) from error
        self._stream = stream
        self._opened = opened
        return stream, opened.st_size

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        del exc_type, exc_value, traceback
        if self._stream is None or self._opened is None:
            _reject()
        try:
            after = os.fstat(self._stream.fileno())
            current = self._path.lstat()
            if (
                bool(getattr(self._path, "is_junction", lambda: False)())
                or not _regular_file_stat(
                    after, minimum=self._minimum, maximum=self._maximum
                )
                or not _regular_file_stat(
                    current, minimum=self._minimum, maximum=self._maximum
                )
                or _file_identity(self._opened) != _file_identity(after)
                or _file_identity(self._opened) != _file_identity(current)
            ):
                _reject()
        except ControllerReleaseAuthorityError:
            raise
        except OSError as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            ) from error
        finally:
            self._stream.close()
        return False


def _read_file(path: Path, *, minimum: int, maximum: int) -> bytes:
    try:
        with _VerifiedRegularFile(path, minimum=minimum, maximum=maximum) as (
            stream,
            expected_size,
        ):
            value = stream.read(maximum + 1)
    except ControllerReleaseAuthorityError:
        raise
    except OSError as error:
        raise ControllerReleaseAuthorityError(
            "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
        ) from error
    if len(value) != expected_size:
        _reject()
    return value


def _json_file(path: Path) -> dict[str, Any]:
    return _json_bytes(_read_file(path, minimum=1, maximum=_MAX_JSON_BYTES))


def _hash_file(path: Path, *, maximum: int) -> tuple[str, int]:
    try:
        digest = hashlib.sha256()
        consumed = 0
        with _VerifiedRegularFile(path, minimum=1, maximum=maximum) as (
            stream,
            expected_size,
        ):
            while chunk := stream.read(1024 * 1024):
                consumed += len(chunk)
                if consumed > maximum:
                    _reject()
                digest.update(chunk)
    except ControllerReleaseAuthorityError:
        raise
    except OSError as error:
        raise ControllerReleaseAuthorityError(
            "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
        ) from error
    if consumed != expected_size:
        _reject()
    return "sha256:" + digest.hexdigest(), consumed


def _verify_checksums(root: Path) -> None:
    names = (
        "release-manifest.json",
        "deployment-contract.json",
        "installer-materials.tar",
    )
    try:
        lines = (
            _read_file(root / "checksums.txt", minimum=1, maximum=4096)
            .decode("utf-8", errors="strict")
            .splitlines()
        )
    except (OSError, UnicodeError) as error:
        raise ControllerReleaseAuthorityError(
            "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
        ) from error
    declared: dict[str, str] = {}
    for line in lines:
        digest, separator, name = line.partition("  ")
        if (
            separator != "  "
            or re.fullmatch(r"[0-9a-f]{64}", digest, re.ASCII) is None
            or name not in names
            or name in declared
        ):
            _reject()
        declared[name] = digest
    if set(declared) != set(names) or any(
        _hash_file(root / name, maximum=MAX_PORTABLE_TOTAL_BYTES)[0]
        != "sha256:" + declared[name]
        for name in names
    ):
        _reject()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url


class _GitHubReadOnlyObservationBoundary:
    """Perform only fixed-repository read and cryptographic verification calls."""

    def __init__(
        self,
        *,
        gh_executable: str | None = None,
        gh_sha256: str | None = None,
        opener: Any | None = None,
    ) -> None:
        executable_value = (
            os.environ.get("ANIMEMO_GH_EXECUTABLE")
            if gh_executable is None
            else gh_executable
        )
        digest_value = (
            os.environ.get("ANIMEMO_GH_SHA256") if gh_sha256 is None else gh_sha256
        )
        if (
            not isinstance(executable_value, str)
            or not executable_value
            or "\x00" in executable_value
            or not isinstance(digest_value, str)
            or _SHA256.fullmatch(digest_value) is None
        ):
            _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
        executable = Path(executable_value)
        if not executable.is_absolute() or executable.suffix.lower() != ".exe":
            _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
        try:
            observed_digest, _size = _hash_file(
                executable, maximum=_MAX_GH_EXECUTABLE_BYTES
            )
        except ControllerReleaseAuthorityError as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if observed_digest != digest_value:
            _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
        self._gh_executable = executable
        self._gh_sha256 = digest_value
        self._opener = (
            opener if opener is not None else urllib.request.build_opener(_NoRedirect())
        )

    @staticmethod
    def _read_only_gh_command(command: tuple[str, ...]) -> bool:
        if not command or any(
            type(item) is not str or "\x00" in item for item in command
        ):
            return False
        if len(command) == 5 and command[:4] == ("gh", "api", "--method", "GET"):
            endpoint = command[4]
            return not endpoint.startswith(("/", "-")) and endpoint.startswith(
                f"repos/{_REPOSITORY}/"
            )
        if (
            len(command) == 10
            and command[:3] == ("gh", "run", "download")
            and command[3].isascii()
            and command[3].isdigit()
            and int(command[3]) > 0
            and command[4:6] == ("--repo", _REPOSITORY)
            and command[6] == "--name"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", command[7], re.ASCII)
            is not None
            and command[8] == "--dir"
        ):
            return Path(command[9]).is_absolute()
        if (
            len(command) == 6
            and command[:3] == ("gh", "release", "verify")
            and command[4:] == ("--repo", _REPOSITORY)
        ):
            return _RC_TAG.fullmatch(command[3]) is not None
        if (
            len(command) == 7
            and command[:3] == ("gh", "release", "verify-asset")
            and command[5:] == ("--repo", _REPOSITORY)
            and _RC_TAG.fullmatch(command[3]) is not None
        ):
            asset = Path(command[4])
            return asset.is_absolute() and asset.name in mirror_release_assets(
                command[3]
            )
        if (
            len(command) == 10
            and command[:3] == ("gh", "attestation", "verify")
            and command[4:6] == ("--repo", _REPOSITORY)
            and command[6:8]
            == ("--signer-workflow", f"{_REPOSITORY}/{_RELEASE_WORKFLOW}")
            and command[8] == "--source-digest"
            and _COMMIT.fullmatch(command[9]) is not None
        ):
            subject = command[3]
            return (
                Path(subject).is_absolute()
                or re.fullmatch(
                    r"oci://ghcr\.io/yanyuhanyue/animemo-(?:api|web)@sha256:[0-9a-f]{64}",
                    subject,
                    re.ASCII,
                )
                is not None
            )
        return False

    def _run(
        self,
        command: tuple[str, ...],
        *,
        timeout: int = 120,
        maximum: int = _MAX_COMMAND_BYTES,
    ) -> bytes:
        if not _GitHubReadOnlyObservationBoundary._read_only_gh_command(command):
            _reject()
        try:
            with _VerifiedRegularFile(
                self._gh_executable,
                minimum=1,
                maximum=_MAX_GH_EXECUTABLE_BYTES,
            ) as (stream, expected_size):
                digest = hashlib.sha256()
                consumed = 0
                while chunk := stream.read(1024 * 1024):
                    consumed += len(chunk)
                    if consumed > _MAX_GH_EXECUTABLE_BYTES:
                        _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                    digest.update(chunk)
                if (
                    consumed != expected_size
                    or "sha256:" + digest.hexdigest() != self._gh_sha256
                ):
                    _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                completed = subprocess.run(
                    ("gh.exe", *command[1:]),
                    executable=str(self._gh_executable),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=timeout,
                )
        except ControllerReleaseAuthorityError as error:
            if str(error) == "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE":
                raise
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if completed.returncode != 0 or len(completed.stdout) > maximum:
            _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
        return completed.stdout

    def _gh_json(self, endpoint: str) -> dict[str, Any]:
        if (
            not endpoint
            or endpoint.startswith(("/", "-"))
            or "\x00" in endpoint
            or not endpoint.startswith(f"repos/{_REPOSITORY}/")
        ):
            _reject()
        return _json_bytes(self._run(("gh", "api", "--method", "GET", endpoint)))

    def _public_json(self, endpoint: str) -> dict[str, Any]:
        if not endpoint.startswith(f"repos/{_REPOSITORY}/"):
            _reject()
        url = "https://api.github.com/" + endpoint
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=60) as response:
                if response.geturl() != url or getattr(response, "status", 200) != 200:
                    _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                value = response.read(_MAX_JSON_BYTES + 1)
        except ControllerReleaseAuthorityError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        return _json_bytes(value)

    def _download_artifact(self, *, run_id: int, name: str, destination: Path) -> None:
        self._run(
            (
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                _REPOSITORY,
                "--name",
                name,
                "--dir",
                str(destination),
            ),
            timeout=180,
        )

    @staticmethod
    def _validate_run(
        run: dict[str, Any],
        *,
        run_id: int,
        name: str,
        path: str,
        head: str,
        events: frozenset[str],
        head_branches: frozenset[str],
    ) -> None:
        repository = run.get("repository")
        if (
            run.get("id") != run_id
            or run.get("name") != name
            or run.get("path") != path
            or run.get("event") not in events
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("run_attempt") != 1
            or run.get("head_branch") not in head_branches
            or run.get("head_sha") != head
            or not isinstance(repository, dict)
            or repository.get("full_name") != _REPOSITORY
        ):
            _reject()

    @staticmethod
    def _select_artifact(
        listing: dict[str, Any], *, run_id: int, head: str, name: str
    ) -> dict[str, Any]:
        artifacts = listing.get("artifacts")
        if (
            type(artifacts) is not list
            or listing.get("total_count") != len(artifacts)
            or len(artifacts) > 100
        ):
            _reject()
        matches = [
            item
            for item in artifacts
            if type(item) is dict and item.get("name") == name
        ]
        if len(matches) != 1:
            _reject()
        artifact = matches[0]
        workflow_run = artifact.get("workflow_run")
        if (
            type(artifact.get("id")) is not int
            or artifact["id"] < 1
            or artifact.get("expired") is not False
            or not isinstance(artifact.get("digest"), str)
            or _SHA256.fullmatch(artifact["digest"]) is None
            or type(workflow_run) is not dict
            or workflow_run.get("id") != run_id
            or workflow_run.get("head_sha") != head
        ):
            _reject()
        return artifact

    @staticmethod
    def _require_successful_job(listing: dict[str, Any], *, name: str) -> None:
        jobs = listing.get("jobs")
        if (
            type(jobs) is not list
            or listing.get("total_count") != len(jobs)
            or len(jobs) > 100
        ):
            _reject()
        matches = [job for job in jobs if type(job) is dict and job.get("name") == name]
        if (
            len(matches) != 1
            or matches[0].get("status") != "completed"
            or matches[0].get("conclusion") != "success"
        ):
            _reject()

    @staticmethod
    def _closed_directory(path: Path, names: frozenset[str]) -> None:
        try:
            entries = list(path.iterdir())
        except OSError as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            ) from error
        if {entry.name for entry in entries} != names or any(
            entry.is_symlink()
            or bool(getattr(entry, "is_junction", lambda: False)())
            or not entry.is_file()
            for entry in entries
        ):
            _reject()

    def _download_public_asset(
        self, *, url: str, destination: Path, expected_size: int
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            _reject()
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=90)
        except urllib.error.HTTPError as error:
            if error.code not in {301, 302, 303, 307, 308}:
                raise ControllerReleaseAuthorityError(
                    "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
                ) from error
            location = error.headers.get("Location")
            error.close()
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        else:
            response.close()
            _reject()
        if not isinstance(location, str):
            _reject()
        redirected = urllib.parse.urlsplit(location)
        hostname = redirected.hostname
        if (
            redirected.scheme != "https"
            or not isinstance(hostname, str)
            or not hostname.endswith(".githubusercontent.com")
            or redirected.username is not None
            or redirected.password is not None
            or redirected.port not in {None, 443}
            or redirected.fragment
        ):
            _reject()
        final_request = urllib.request.Request(
            location,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="GET",
        )
        try:
            with self._opener.open(final_request, timeout=900) as response:
                if (
                    response.geturl() != location
                    or getattr(response, "status", 200) != 200
                ):
                    _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                consumed = 0
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        while chunk := response.read(1024 * 1024):
                            consumed += len(chunk)
                            if consumed > expected_size:
                                _reject()
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
        except ControllerReleaseAuthorityError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if consumed != expected_size:
            _reject()

    def _verify_metadata(
        self,
        root: Path,
        request: dict[str, object],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        sidecar_name = f"animemo-{request['releaseTag']}-release-attestation.json"
        self._closed_directory(root, _RELEASE_METADATA_FILES | {sidecar_name})
        candidate = validate_candidate_input(_json_file(root / "candidate-input.json"))
        verified = validate_verified_candidate(
            _json_file(root / "verified-candidate.json")
        )
        receipt = validate_aggregate_receipt(
            _json_file(root / "candidate-acceptance-receipt.json")
        )
        candidate_digest = sha256_bytes(canonical_json_bytes(candidate))
        verified_digest = sha256_bytes(canonical_json_bytes(verified))
        receipt_digest = aggregate_receipt_digest(receipt)
        candidate_plan = _json_file(root / "publish-candidate-plan.json")
        candidate_authority = _candidate_authority(
            candidate_plan, str(request["finalRepoTree"])
        )
        if (
            candidate_digest != request["candidateInputSha256"]
            or verified_digest != request["verifiedCandidateIdentity"]
            or receipt_digest != request["candidateAggregateReceiptSha256"]
            or verified.get("candidate_input_sha256") != candidate_digest
            or receipt.get("candidate_input_digest") != candidate_digest
            or receipt.get("verified_candidate_digest") != verified_digest
            or candidate_plan.get("candidate_input_digest") != candidate_digest
            or candidate_plan.get("verified_candidate_digest") != verified_digest
            or candidate_plan.get("candidate_acceptance_receipt_digest")
            != receipt_digest
            or candidate.get("source_sha") != request["finalRepoHead"]
            or candidate.get("source_tree") != request["finalRepoTree"]
            or candidate.get("qualification_run_id") != request["qualificationRunId"]
            or candidate.get("qualification_run_attempt") != 1
            or candidate.get("candidate_version") != request["releaseTag"]
            or candidate_authority["api_digest"] != request["apiDigest"]
            or candidate_authority["web_digest"] != request["webDigest"]
            or candidate_authority["source_sha"] != request["finalRepoHead"]
            or candidate_authority["tag"] != request["releaseTag"]
        ):
            _reject()
        publication_plan = validate_publication_plan(
            _json_file(root / "publication-plan.json")
        )
        normalized_plan = _normalize_authority_plan(
            publication_plan, str(request["finalRepoTree"])
        )
        if (
            publication_plan["schema"] != "animemo.release-publication-plan/v2"
            or publication_plan["repository"] != _REPOSITORY
            or publication_plan["channel"] != request["releaseChannel"]
            or publication_plan["tag"] != request["releaseTag"]
            or publication_plan["commit"] != request["finalRepoHead"]
            or publication_plan["api_digest"] != request["apiDigest"]
            or publication_plan["web_digest"] != request["webDigest"]
        ):
            _reject()
        ledger = validate_ledger(
            _json_file(root / "publication-transaction-ledger.json")
        )
        if (
            ledger["planSchema"] != normalized_plan["schema"]
            or ledger["planDigest"] != normalized_plan["plan_digest"]
            or ledger["planIdentity"] != normalized_plan["plan_identity"]
            or ledger["source"]
            != {
                "sha": request["finalRepoHead"],
                "tree": request["finalRepoTree"],
            }
            or ledger["target"]
            != {"tag": request["releaseTag"], "version": request["releaseTag"]}
            or ledger["expected"]["apiDigest"] != request["apiDigest"]
            or ledger["expected"]["webDigest"] != request["webDigest"]
            or ledger["expected"]["assets"] != normalized_plan["assets"]
            or ledger["expected"]["transportAssets"]
            != normalized_plan["transport_assets"]
            or ledger["attemptLimit"] != 10
            or ledger["finalState"] != "COMPLETE"
            or ledger["recoveryStatus"] != "COMPLETE"
        ):
            _reject()
        return candidate, publication_plan, ledger

    def _verify_public_release(
        self,
        *,
        root: Path,
        metadata_root: Path,
        request: dict[str, object],
        publication_plan: dict[str, Any],
    ) -> tuple[int, str, dict[str, dict[str, Any]]]:
        tag = str(request["releaseTag"])
        release = self._public_json(f"repos/{_REPOSITORY}/releases/tags/{tag}")
        raw_assets = release.get("assets")
        if (
            release.get("tag_name") != tag
            or release.get("name") != tag
            or release.get("draft") is not False
            or release.get("prerelease") is not True
            or release.get("immutable") is not True
            or type(release.get("id")) is not int
            or release["id"] < 1
            or type(release.get("body")) is not str
            or type(raw_assets) is not list
        ):
            _reject()
        tag_ref = self._public_json(f"repos/{_REPOSITORY}/git/ref/tags/{tag}")
        target = tag_ref.get("object")
        if (
            type(target) is not dict
            or target.get("type") != "tag"
            or not isinstance(target.get("sha"), str)
            or _COMMIT.fullmatch(target["sha"]) is None
        ):
            _reject()
        tag_object_sha = target["sha"]
        tag_object = self._public_json(f"repos/{_REPOSITORY}/git/tags/{tag_object_sha}")
        peeled = tag_object.get("object")
        if (
            tag_object.get("tag") != tag
            or tag_object.get("message") != tag + "\n"
            or type(peeled) is not dict
            or peeled.get("type") != "commit"
            or peeled.get("sha") != request["finalRepoHead"]
        ):
            _reject()
        expected = declared_publication_assets(publication_plan)
        inventory: dict[str, dict[str, Any]] = {}
        urls: dict[str, str] = {}
        for item in raw_assets:
            if (
                type(item) is not dict
                or type(item.get("name")) is not str
                or item["name"] in inventory
                or type(item.get("size")) is not int
                or item["size"] < 1
                or not isinstance(item.get("digest"), str)
                or _SHA256.fullmatch(item["digest"]) is None
                or type(item.get("browser_download_url")) is not str
            ):
                _reject()
            inventory[item["name"]] = {
                "sha256": item["digest"],
                "size": item["size"],
            }
            urls[item["name"]] = item["browser_download_url"]
        if inventory != expected:
            _reject()
        for name, declared in expected.items():
            expected_url = (
                f"https://github.com/{_REPOSITORY}/releases/download/{tag}/"
                + urllib.parse.quote(name, safe="")
            )
            if urls[name] != expected_url:
                _reject()
            self._download_public_asset(
                url=expected_url,
                destination=root / name,
                expected_size=declared["size"],
            )
            if _hash_file(root / name, maximum=declared["size"]) != (
                declared["sha256"],
                declared["size"],
            ):
                _reject()
        if {item.name for item in root.iterdir()} != set(expected):
            _reject()
        body_digest = (
            "sha256:" + hashlib.sha256(release["body"].encode("utf-8")).hexdigest()
        )
        computed_post_publish = verify_post_publish(
            publication_plan,
            release={
                "tag": tag,
                "target": request["finalRepoHead"],
                "draft": False,
                "prerelease": True,
                "notes_body_sha256": body_digest,
                "public_unauthenticated_assets": True,
            },
            remote_assets=inventory,
            downloaded_assets={name: root / name for name in expected},
            api_digest=str(request["apiDigest"]),
            web_digest=str(request["webDigest"]),
            attestations_verified=True,
        )
        if (
            _json_file(metadata_root / "post-publish-verification.json")
            != computed_post_publish
        ):
            _reject()
        return release["id"], tag_object_sha, inventory

    def _verify_attestations(
        self,
        *,
        metadata_root: Path,
        public_root: Path,
        request: dict[str, object],
    ) -> None:
        tag = str(request["releaseTag"])
        portable = public_root / f"animemo-{tag}-portable.tar"
        sidecar = metadata_root / f"animemo-{tag}-release-attestation.json"
        try:
            envelope = validate_attestation_sidecar(
                sidecar.read_bytes(), payload=portable
            )
        except (OSError, ValueError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            ) from error
        acquisition = _json_file(
            metadata_root / "release-attestation-acquisition-receipt.json"
        )
        if (
            envelope["tag"] != tag
            or envelope["commit"] != request["finalRepoHead"]
            or envelope["workflow"] != _RELEASE_WORKFLOW
            or set(acquisition)
            != {"schema", "tag", "payload", "evidence_count", "authority_role"}
            or acquisition["schema"] != envelope["schema"]
            or acquisition["tag"] != tag
            or acquisition["payload"] != envelope["payload"]
            or acquisition["evidence_count"] != 7
            or acquisition["authority_role"] != "TRANSPORT_ONLY"
        ):
            _reject()
        self._run(("gh", "release", "verify", tag, "--repo", _REPOSITORY))
        for name in mirror_release_assets(tag):
            self._run(
                (
                    "gh",
                    "release",
                    "verify-asset",
                    tag,
                    str(public_root / name),
                    "--repo",
                    _REPOSITORY,
                )
            )
        signer = f"{_REPOSITORY}/{_RELEASE_WORKFLOW}"
        subjects = (
            f"oci://ghcr.io/yanyuhanyue/animemo-api@{request['apiDigest']}",
            f"oci://ghcr.io/yanyuhanyue/animemo-web@{request['webDigest']}",
            str(public_root / "release-manifest.json"),
            str(public_root / "deployment-contract.json"),
            str(public_root / "installer-materials.tar"),
        )
        for subject in subjects:
            self._run(
                (
                    "gh",
                    "attestation",
                    "verify",
                    subject,
                    "--repo",
                    _REPOSITORY,
                    "--signer-workflow",
                    signer,
                    "--source-digest",
                    str(request["finalRepoHead"]),
                )
            )

    @staticmethod
    def _single_http_header(headers: Any, name: str) -> str | None:
        values: list[str] = []
        if hasattr(headers, "get_all"):
            candidates = headers.get_all(name, [])
            if isinstance(candidates, list):
                values = [value for value in candidates if isinstance(value, str)]
        elif hasattr(headers, "items"):
            values = [
                value
                for key, value in headers.items()
                if isinstance(key, str)
                and key.lower() == name.lower()
                and isinstance(value, str)
            ]
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _ghcr_bearer_challenge(value: str | None, *, repository: str) -> str:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not 1 <= len(value) <= 2048
        ):
            _reject()
        scheme, separator, parameters = value.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            _reject()
        parsed: dict[str, str] = {}
        for item in parameters.split(","):
            match = re.fullmatch(r'\s*([a-z]+)="([^"\\]*)"\s*', item, re.ASCII)
            if match is None or match.group(1) in parsed:
                _reject()
            parsed[match.group(1)] = match.group(2)
        expected_scope = f"repository:{repository}:pull"
        if parsed != {
            "realm": _GHCR_TOKEN_URL,
            "service": "ghcr.io",
            "scope": expected_scope,
        }:
            _reject()
        return expected_scope

    def _ghcr_digest(self, *, role: str, tag: str) -> str:
        if role not in {"api", "web"} or _REGISTRY_TAG.fullmatch(tag) is None:
            _reject()
        repository = f"yanyuhanyue/animemo-{role}"
        manifest_url = f"https://ghcr.io/v2/{repository}/manifests/{tag}"
        challenge_request = urllib.request.Request(
            manifest_url,
            headers={
                "Accept": _GHCR_MANIFEST_ACCEPT,
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="HEAD",
        )
        try:
            challenge_response = self._opener.open(challenge_request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 401 or error.geturl() != manifest_url:
                error.close()
                raise ControllerReleaseAuthorityError(
                    "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
                ) from error
            challenge = self._single_http_header(error.headers, "WWW-Authenticate")
            error.close()
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        else:
            challenge_response.close()
            _reject()
        scope = self._ghcr_bearer_challenge(challenge, repository=repository)
        token_url = (
            _GHCR_TOKEN_URL
            + "?"
            + urllib.parse.urlencode((("service", "ghcr.io"), ("scope", scope)))
        )
        token_request = urllib.request.Request(
            token_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="GET",
        )
        try:
            with self._opener.open(token_request, timeout=30) as response:
                if (
                    response.geturl() != token_url
                    or getattr(response, "status", 200) != 200
                ):
                    _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                token_bytes = response.read(_MAX_GHCR_TOKEN_BYTES + 1)
        except ControllerReleaseAuthorityError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if len(token_bytes) > _MAX_GHCR_TOKEN_BYTES:
            _reject()
        token_document = _json_bytes(token_bytes)
        token = token_document.get("token")
        if (
            set(token_document) != {"token"}
            or not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or _BEARER_TOKEN.fullmatch(token) is None
        ):
            _reject()
        manifest_request = urllib.request.Request(
            manifest_url,
            headers={
                "Accept": _GHCR_MANIFEST_ACCEPT,
                "Authorization": f"Bearer {token}",
                "User-Agent": "AniMemo-controller-release-verifier/1",
            },
            method="HEAD",
        )
        try:
            with self._opener.open(manifest_request, timeout=30) as response:
                if (
                    response.geturl() != manifest_url
                    or getattr(response, "status", 200) != 200
                    or response.read(1) != b""
                ):
                    _reject("CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE")
                digest = self._single_http_header(
                    response.headers, "Docker-Content-Digest"
                )
        except ControllerReleaseAuthorityError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            _reject()
        return digest

    def _verify_registry(self, request: dict[str, object]) -> None:
        for role, digest_key in (("api", "apiDigest"), ("web", "webDigest")):
            for tag in (
                str(request["releaseTag"]),
                f"sha-{request['finalRepoHead']}",
            ):
                if self._ghcr_digest(role=role, tag=tag) != request[digest_key]:
                    _reject()

    @staticmethod
    def _asset_content_type(name: str) -> str:
        if name.startswith("animemo-v") and name.endswith("-portable.tar"):
            return "application/x-tar"
        return {
            "checksums.txt": "text/plain; charset=utf-8",
            "deployment-contract.json": "application/json",
            "installer-materials.tar": "application/x-tar",
            "release-manifest.json": "application/json",
        }[name]

    @staticmethod
    def _header(headers: Any, name: str) -> str | None:
        if not hasattr(headers, "items"):
            return None
        values = [
            value
            for key, value in headers.items()
            if isinstance(key, str)
            and key.lower() == name.lower()
            and isinstance(value, str)
        ]
        return ",".join(values) if values else None

    @staticmethod
    def _immutable_cache_control(value: str | None) -> bool:
        if not isinstance(value, str) or not value.isascii():
            return False
        directives = [item.strip(" \t").lower() for item in value.split(",")]
        return len(directives) == 3 and set(directives) == {
            "public",
            "max-age=31536000",
            "immutable",
        }

    def _verify_mirror(
        self,
        *,
        artifact_root: Path,
        public_root: Path,
        scratch: Path,
        request: dict[str, object],
        release_id: int,
        release_assets: dict[str, dict[str, Any]],
    ) -> None:
        mirror_evidence = _json_file(artifact_root / "release-mirror.json")
        required = {
            "schemaVersion",
            "role",
            "repository",
            "releaseTag",
            "releaseId",
            "releaseCommit",
            "mirrorOrigin",
            "mirrorPrefix",
            "assetCount",
            "uploadedObjectCount",
            "existingEqualObjectCount",
            "objectOverwriteCount",
            "existingMismatchCount",
            "rangeStatus",
            "publicReadback",
            "receiptDigest",
        }
        if (
            set(mirror_evidence) != required
            or mirror_evidence["schemaVersion"] != 1
            or mirror_evidence["role"]
            != "NON_AUTHORITY_TRANSPORT_VERIFICATION_EVIDENCE"
            or mirror_evidence["repository"] != _REPOSITORY
            or mirror_evidence["releaseTag"] != request["releaseTag"]
            or mirror_evidence["releaseId"] != release_id
            or mirror_evidence["releaseCommit"] != request["finalRepoHead"]
            or mirror_evidence["mirrorOrigin"] != MIRROR_ORIGIN
            or mirror_evidence["mirrorPrefix"] != MIRROR_PATH_PREFIX
            or mirror_evidence["assetCount"] != 5
            or type(mirror_evidence["uploadedObjectCount"]) is not int
            or type(mirror_evidence["existingEqualObjectCount"]) is not int
            or mirror_evidence["uploadedObjectCount"]
            + mirror_evidence["existingEqualObjectCount"]
            != 6
            or mirror_evidence["objectOverwriteCount"] != 0
            or mirror_evidence["existingMismatchCount"] != 0
            or mirror_evidence["rangeStatus"] != "PASS"
            or mirror_evidence["publicReadback"] != "PASS"
        ):
            _reject()
        reader = OfficialMirrorPublicReader()
        prefix = f"{MIRROR_PATH_PREFIX}/{request['releaseTag']}"
        marker = scratch / "mirror-receipt.json"
        status, headers = reader.read_to(
            f"{MIRROR_ORIGIN}/{prefix}/{MIRROR_RECEIPT_NAME}", marker
        )
        try:
            marker_bytes = marker.read_bytes()
        except OSError as error:
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from error
        if (
            status != 200
            or self._header(headers, "Content-Length") != str(len(marker_bytes))
            or self._header(headers, "Content-Type") != "application/json"
            or not self._immutable_cache_control(self._header(headers, "Cache-Control"))
            or self._header(headers, "Content-Encoding") is not None
        ):
            _reject()
        receipt = load_mirror_receipt_bytes(marker_bytes)
        expected_receipt_assets = [
            {
                "name": name,
                "size": release_assets[name]["size"],
                "sha256": release_assets[name]["sha256"],
            }
            for name in mirror_release_assets(str(request["releaseTag"]))
        ]
        if (
            receipt["releaseTag"] != request["releaseTag"]
            or receipt["releaseId"] != release_id
            or receipt["releaseCommit"] != request["finalRepoHead"]
            or receipt["publisherRunId"] != request["mirrorRunId"]
            or receipt["assets"] != expected_receipt_assets
            or receipt["receiptDigest"] != mirror_evidence["receiptDigest"]
        ):
            _reject()
        for index, declared in enumerate(expected_receipt_assets):
            name = declared["name"]
            target = scratch / f"mirror-{index}"
            status, headers = reader.read_to(f"{MIRROR_ORIGIN}/{prefix}/{name}", target)
            if (
                status != 200
                or self._header(headers, "Content-Length") != str(declared["size"])
                or self._header(headers, "Accept-Ranges") != "bytes"
                or self._header(headers, "Content-Type")
                != self._asset_content_type(name)
                or not self._immutable_cache_control(
                    self._header(headers, "Cache-Control")
                )
                or self._header(headers, "Content-Encoding") is not None
                or _hash_file(target, maximum=declared["size"])
                != (declared["sha256"], declared["size"])
            ):
                _reject()
        portable_name = expected_receipt_assets[-1]["name"]
        range_status, range_headers, first_mib = reader.first_mib(
            f"{MIRROR_ORIGIN}/{prefix}/{portable_name}"
        )
        with (public_root / portable_name).open("rb") as source:
            expected_first_mib = source.read(1024 * 1024)
        if (
            range_status != 206
            or first_mib != expected_first_mib
            or self._header(range_headers, "Content-Range")
            != f"bytes 0-{len(first_mib) - 1}/{release_assets[portable_name]['size']}"
        ):
            _reject()

    def observe(
        self,
        *,
        authority_request: dict[str, object],
        expected_public_identity: dict[str, object],
        candidate_result: dict[str, object],
    ) -> ReleaseAuthorityEvidence:
        del expected_public_identity, candidate_result
        request = authority_request
        publish_run_id = int(request["publishRunId"])
        mirror_run_id = int(request["mirrorRunId"])
        publish_run = self._gh_json(
            f"repos/{_REPOSITORY}/actions/runs/{publish_run_id}"
        )
        self._validate_run(
            publish_run,
            run_id=publish_run_id,
            name="Release Producer",
            path=_RELEASE_WORKFLOW,
            head=str(request["finalRepoHead"]),
            events=frozenset({"workflow_dispatch"}),
            head_branches=frozenset({"main"}),
        )
        self._require_successful_job(
            self._gh_json(
                f"repos/{_REPOSITORY}/actions/runs/{publish_run_id}/jobs?per_page=100"
            ),
            name="publish-immutable-prerelease",
        )
        commit = self._gh_json(
            f"repos/{_REPOSITORY}/git/commits/{request['finalRepoHead']}"
        )
        if (
            type(commit.get("tree")) is not dict
            or commit["tree"].get("sha") != request["finalRepoTree"]
        ):
            _reject()
        publish_artifacts = self._gh_json(
            f"repos/{_REPOSITORY}/actions/runs/{publish_run_id}/artifacts?per_page=100"
        )
        metadata_name = f"release-publication-metadata-{request['releaseTag']}"
        self._select_artifact(
            publish_artifacts,
            run_id=publish_run_id,
            head=str(request["finalRepoHead"]),
            name=metadata_name,
        )
        mirror_run = self._gh_json(f"repos/{_REPOSITORY}/actions/runs/{mirror_run_id}")
        self._validate_run(
            mirror_run,
            run_id=mirror_run_id,
            name="Release Mirror",
            path=_MIRROR_WORKFLOW,
            head=str(request["finalRepoHead"]),
            events=frozenset({"release", "workflow_dispatch"}),
            head_branches=frozenset({"main", str(request["releaseTag"])}),
        )
        self._require_successful_job(
            self._gh_json(
                f"repos/{_REPOSITORY}/actions/runs/{mirror_run_id}/jobs?per_page=100"
            ),
            name="发布不可变发行镜像",
        )
        mirror_artifacts = self._gh_json(
            f"repos/{_REPOSITORY}/actions/runs/{mirror_run_id}/artifacts?per_page=100"
        )
        with tempfile.TemporaryDirectory(
            prefix="animemo-controller-release-readonly-"
        ) as temporary:
            root = Path(temporary)
            metadata_root = root / "metadata"
            public_root = root / "public"
            mirror_root = root / "mirror-artifact"
            mirror_public_root = root / "mirror-public"
            for path in (
                metadata_root,
                public_root,
                mirror_root,
                mirror_public_root,
            ):
                path.mkdir(mode=0o700)
            self._download_artifact(
                run_id=publish_run_id,
                name=metadata_name,
                destination=metadata_root,
            )
            candidate, publication_plan, _ledger = self._verify_metadata(
                metadata_root, request
            )
            release_id, tag_object, release_assets = self._verify_public_release(
                root=public_root,
                metadata_root=metadata_root,
                request=request,
                publication_plan=publication_plan,
            )
            candidate_assets = {
                "checksums.txt": candidate["checksums_sha256"],
                "deployment-contract.json": candidate["deployment_contract_sha256"],
                "installer-materials.tar": candidate["installer_materials_sha256"],
                "release-manifest.json": candidate["release_manifest_sha256"],
            }
            if any(
                release_assets[name]["sha256"] != digest
                for name, digest in candidate_assets.items()
            ):
                _reject()
            manifest = _json_file(public_root / "release-manifest.json")
            deployment = _json_file(public_root / "deployment-contract.json")
            try:
                _verify_checksums(public_root)
                validate_manifest(manifest, updater_version=updater_version)
                validate_deployment_contract(
                    deployment,
                    installer_materials=public_root / "installer-materials.tar",
                )
                portable = inspect_portable_archive(
                    public_root / f"animemo-{request['releaseTag']}-portable.tar"
                )
            except ValueError as error:
                raise ControllerReleaseAuthorityError(
                    "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
                ) from error
            if (
                manifest["release"]["version"] != request["releaseTag"]
                or manifest["release"]["commit"] != request["finalRepoHead"]
                or manifest["images"]["api"]["digest"] != request["apiDigest"]
                or manifest["images"]["web"]["digest"] != request["webDigest"]
                or deployment_contract_digest(deployment)
                != manifest["deployment"]["contractSha256"]
                or portable.archive_sha256
                != release_assets[f"animemo-{request['releaseTag']}-portable.tar"][
                    "sha256"
                ]
                or portable.archive_size
                != release_assets[f"animemo-{request['releaseTag']}-portable.tar"][
                    "size"
                ]
            ):
                _reject()
            self._verify_attestations(
                metadata_root=metadata_root,
                public_root=public_root,
                request=request,
            )
            self._verify_registry(request)
            mirror_name = f"release-mirror-{release_id}"
            self._select_artifact(
                mirror_artifacts,
                run_id=mirror_run_id,
                head=str(request["finalRepoHead"]),
                name=mirror_name,
            )
            self._download_artifact(
                run_id=mirror_run_id,
                name=mirror_name,
                destination=mirror_root,
            )
            self._closed_directory(mirror_root, frozenset({"release-mirror.json"}))
            self._verify_mirror(
                artifact_root=mirror_root,
                public_root=public_root,
                scratch=mirror_public_root,
                request=request,
                release_id=release_id,
                release_assets=release_assets,
            )
        publication = close_github_release_publication(
            {
                "schemaVersion": 1,
                "predicateType": GITHUB_RELEASE_PREDICATE_TYPE,
                "immutable": True,
                "repository": {
                    "name": _REPOSITORY,
                    "repositoryId": REPOSITORY_ID,
                    "ownerId": OWNER_ID,
                },
                "tag": request["releaseTag"],
                "tagCommit": request["finalRepoHead"],
                "tagObject": tag_object,
                "draft": False,
                "prerelease": True,
                "signedAt": "1970-01-01T00:00:00Z",
                "certificate": {
                    "identity": GITHUB_RELEASE_CERTIFICATE_IDENTITY,
                    "issuerOrganization": "GitHub, Inc.",
                },
                "assets": [
                    {"name": name, **release_assets[name]}
                    for name in (
                        "checksums.txt",
                        "deployment-contract.json",
                        "installer-materials.tar",
                        "release-manifest.json",
                    )
                ],
                "transportAssets": [
                    {
                        "name": f"animemo-{request['releaseTag']}-portable.tar",
                        **release_assets[
                            f"animemo-{request['releaseTag']}-portable.tar"
                        ],
                        "role": "PORTABLE_RELEASE_BUNDLE",
                        "authorityRole": "TRANSPORT_ONLY",
                    }
                ],
            }
        )
        if (
            publication.identity != request["publicationIdentity"]
            or candidate["api_oci_digest"] != request["apiDigest"]
            or candidate["web_oci_digest"] != request["webDigest"]
        ):
            _reject()
        return ReleaseAuthorityEvidence(
            final_repo_head=str(request["finalRepoHead"]),
            final_repo_tree=str(request["finalRepoTree"]),
            qualification_run_id=int(request["qualificationRunId"]),
            candidate_input_sha256=str(request["candidateInputSha256"]),
            verified_candidate_identity=str(request["verifiedCandidateIdentity"]),
            candidate_aggregate_receipt_sha256=str(
                request["candidateAggregateReceiptSha256"]
            ),
            release_tag=str(request["releaseTag"]),
            release_version=str(request["releaseVersion"]),
            release_channel=str(request["releaseChannel"]),
            publish_run_id=publish_run_id,
            mirror_run_id=mirror_run_id,
            publication_identity=publication.identity,
            api_digest=str(request["apiDigest"]),
            web_digest=str(request["webDigest"]),
            publish_rebuild_count=0,
            global_mutation_freeze=False,
            publish_result="PASS",
            mirror_result="PASS",
            remote_readback_result="PASS",
            zero_rebuild=True,
        )


class ControllerReleaseAuthorityVerifier:
    def __init__(self, observer: ReleaseAuthorityObserver) -> None:
        self._observer = observer

    def verify_controller_release_authority(
        self,
        *,
        authority_request: dict[str, object],
        expected_public_identity: dict[str, object],
        candidate_result: dict[str, object],
        prohibited_actions: tuple[str, ...],
    ) -> dict[str, Any]:
        if (
            type(authority_request) is not dict
            or set(authority_request) != _REQUEST_FIELDS
            or type(expected_public_identity) is not dict
            or type(candidate_result) is not dict
            or type(prohibited_actions) is not tuple
            or frozenset(prohibited_actions) != _PROHIBITED_ACTIONS
            or len(prohibited_actions) != len(_PROHIBITED_ACTIONS)
            or not _valid_authority_request(authority_request)
        ):
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID"
            )
        public_bindings = {
            "finalRepoHead": "finalRepoHead",
            "finalRepoTree": "finalRepoTree",
            "qualificationRunId": "qualificationRunId",
            "candidateInputSha256": "candidateInputSha256",
            "verifiedCandidateIdentity": "verifiedCandidateIdentity",
            "releaseTag": "releaseTag",
            "releaseVersion": "releaseVersion",
            "releaseChannel": "releaseChannel",
        }
        if (
            set(expected_public_identity) != _PUBLIC_IDENTITY_FIELDS
            or expected_public_identity.get("schema")
            != "animemo.rc19-release-public-identity/v1"
            or expected_public_identity.get("qualificationRunAttempt") != 1
            or any(
                expected_public_identity.get(public_name)
                != authority_request.get(request_name)
                for public_name, request_name in public_bindings.items()
            )
        ):
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID"
            )
        if (
            candidate_result.get("status") != "PASS"
            or candidate_result.get("candidateAggregateReceiptDigest")
            != authority_request["candidateAggregateReceiptSha256"]
        ):
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID"
            )
        evidence = self._observer.observe(
            authority_request=dict(authority_request),
            expected_public_identity=dict(expected_public_identity),
            candidate_result=dict(candidate_result),
        )
        expected = {
            "final_repo_head": authority_request["finalRepoHead"],
            "final_repo_tree": authority_request["finalRepoTree"],
            "qualification_run_id": authority_request["qualificationRunId"],
            "candidate_input_sha256": authority_request["candidateInputSha256"],
            "verified_candidate_identity": authority_request[
                "verifiedCandidateIdentity"
            ],
            "candidate_aggregate_receipt_sha256": authority_request[
                "candidateAggregateReceiptSha256"
            ],
            "release_tag": authority_request["releaseTag"],
            "release_version": authority_request["releaseVersion"],
            "release_channel": authority_request["releaseChannel"],
            "publish_run_id": authority_request["publishRunId"],
            "mirror_run_id": authority_request["mirrorRunId"],
            "publication_identity": authority_request["publicationIdentity"],
            "api_digest": authority_request["apiDigest"],
            "web_digest": authority_request["webDigest"],
            "publish_rebuild_count": 0,
            "global_mutation_freeze": False,
            "publish_result": "PASS",
            "mirror_result": "PASS",
            "remote_readback_result": "PASS",
            "zero_rebuild": True,
        }
        if type(evidence) is not ReleaseAuthorityEvidence or any(
            getattr(evidence, name) != value for name, value in expected.items()
        ):
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            )
        return {
            **{
                key: value
                for key, value in authority_request.items()
                if key != "schema"
            },
            "schema": "animemo.rc19-release-authority-observation/v1",
            "result": "PASS",
            "releaseAuthorityResult": "PASS",
            "mirrorResult": "PASS",
            "remoteReadbackResult": "PASS",
            "zeroRebuild": True,
        }


class ProductionReleaseAuthorityObserver:
    def __init__(self, boundary: ReleaseAuthorityObserver | None = None) -> None:
        self._boundary = (
            boundary if boundary is not None else _GitHubReadOnlyObservationBoundary()
        )

    def observe(
        self,
        *,
        authority_request: dict[str, object],
        expected_public_identity: dict[str, object],
        candidate_result: dict[str, object],
    ) -> ReleaseAuthorityEvidence:
        try:
            evidence = self._boundary.observe(
                authority_request=dict(authority_request),
                expected_public_identity=dict(expected_public_identity),
                candidate_result=dict(candidate_result),
            )
        except ControllerReleaseAuthorityError:
            raise
        except (
            OSError,
            TimeoutError,
            subprocess.SubprocessError,
            urllib.error.URLError,
        ):
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from None
        except Exception:  # noqa: BLE001 - unknown validation failures are terminal
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID"
            ) from None
        except BaseException:  # noqa: BLE001 - fail closed across cancellation paths
            raise ControllerReleaseAuthorityError(
                "CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE"
            ) from None
        if type(evidence) is not ReleaseAuthorityEvidence:
            _reject()
        return evidence


def verify_controller_release_authority(
    *,
    authority_request: dict[str, object],
    expected_public_identity: dict[str, object],
    candidate_result: dict[str, object],
    prohibited_actions: tuple[str, ...],
) -> dict[str, Any]:
    """Verify one controller Release request through the production observer."""

    return ControllerReleaseAuthorityVerifier(
        ProductionReleaseAuthorityObserver()
    ).verify_controller_release_authority(
        authority_request=authority_request,
        expected_public_identity=expected_public_identity,
        candidate_result=candidate_result,
        prohibited_actions=prohibited_actions,
    )


__all__ = [
    "ControllerReleaseAuthorityError",
    "ControllerReleaseAuthorityVerifier",
    "ProductionReleaseAuthorityObserver",
    "ReleaseAuthorityEvidence",
    "ReleaseAuthorityObserver",
    "verify_controller_release_authority",
]
