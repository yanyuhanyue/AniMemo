"""Production remote adapters for the durable publication transaction.

The adapters use fixed argv or HTTP operations. Publication-plan command arrays
are validation evidence only and are never executed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from updater.oci import OCIContractError, OCIImageExpectation, verify_oci_image

from .publication import declared_publication_assets, validate_publication_plan
from .publication_transaction import (
    MutationIntent,
    MutationResponse,
    PublicationTransactionError,
    RemoteObservation,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_ABSENT_REGISTRY = re.compile(
    rb"manifest unknown|not found|name unknown", re.ASCII | re.IGNORECASE
)


def canonical_identity(value: Any) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


CommandRunner = Callable[[tuple[str, ...], int], CommandResult]


def run_command(argv: tuple[str, ...], timeout_seconds: int) -> CommandResult:
    completed = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    body: bytes


GitHubRequester = Callable[[str, str, Mapping[str, Any] | None], GitHubResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def github_request(
    method: str, endpoint: str, payload: Mapping[str, Any] | None
) -> GitHubResponse:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PublicationTransactionError("TRANSACTION_GITHUB_CREDENTIAL_MISSING")
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "AniMemo-publication-transaction/1",
    }
    if payload is not None:
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "https://api.github.com/" + endpoint.lstrip("/"),
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=45
        ) as response:
            return GitHubResponse(response.status, response.read())
    except urllib.error.HTTPError as error:
        # The response body can contain transport diagnostics. It stays in
        # memory and is never copied to the ledger or exception text.
        return GitHubResponse(error.code, error.read())
    except (OSError, TimeoutError) as error:
        raise ConnectionError("GitHub remote state is unknown") from error


def _json_object(response: GitHubResponse) -> dict[str, Any] | None:
    if response.status == 404:
        return None
    if response.status != 200:
        raise ConnectionError("GitHub remote state is unknown")
    try:
        value = json.loads(response.body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConnectionError("GitHub remote state is unknown") from error
    if not isinstance(value, dict):
        raise ConnectionError("GitHub remote state is unknown")
    return value


def _open_github_asset_stream(url: str, token: str):
    authenticated = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "AniMemo-publication-transaction/1",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        return opener.open(authenticated, timeout=90)
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        error.close()
    if not isinstance(location, str):
        raise OSError("GitHub asset redirect is invalid")
    parsed = urllib.parse.urlsplit(location)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not isinstance(hostname, str)
        or not hostname.endswith(".githubusercontent.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise OSError("GitHub asset redirect is invalid")
    unauthenticated = urllib.request.Request(
        location,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "AniMemo-publication-transaction/1",
        },
    )
    return urllib.request.build_opener(_NoRedirect()).open(
        unauthenticated, timeout=90
    )


class RegistryAdapter:
    def __init__(
        self,
        *,
        target: str,
        expected_digest: str,
        source_layout: Path | None = None,
        source_role: str | None = None,
        source_repository: str | None = None,
        source_reference: str | None = None,
        run: CommandRunner = run_command,
    ) -> None:
        if not _SHA256.fullmatch(expected_digest):
            raise PublicationTransactionError("TRANSACTION_REGISTRY_DIGEST_INVALID")
        if (source_layout is None) == (source_reference is None):
            raise PublicationTransactionError("TRANSACTION_REGISTRY_SOURCE_INVALID")
        if source_layout is not None and (
            source_role not in {"api", "web"}
            or not isinstance(source_repository, str)
            or source_repository != target.rpartition(":")[0]
        ):
            raise PublicationTransactionError("TRANSACTION_REGISTRY_SOURCE_INVALID")
        if source_layout is None and (
            source_role is not None or source_repository is not None
        ):
            raise PublicationTransactionError("TRANSACTION_REGISTRY_SOURCE_INVALID")
        self.target = target
        self.expected_digest = expected_digest
        self.source_layout = source_layout
        self.source_role = source_role
        self.source_repository = source_repository
        self.source_reference = source_reference
        self.run = run

    def _local_layout_exact(self) -> bool:
        if self.source_layout is None:
            return True
        try:
            verified = verify_oci_image(
                self.source_layout,
                OCIImageExpectation(
                    role=str(self.source_role),
                    repository=str(self.source_repository),
                    digest=self.expected_digest,
                    platform="linux/amd64",
                    layout_path=f"oci/{self.source_role}",
                ),
            )
        except (OCIContractError, OSError, ValueError):
            return False
        return (
            verified.digest == self.expected_digest
            and verified.repository == self.source_repository
            and verified.role == self.source_role
            and verified.platform == "linux/amd64"
        )

    def _digest_observation(self, reference: str) -> tuple[str, str | None]:
        try:
            result = self.run(("crane", "digest", reference), 45)
        except subprocess.TimeoutExpired:
            return "UNKNOWN", None
        if result.returncode == 0:
            try:
                digest = result.stdout.decode("ascii", errors="strict").strip()
            except UnicodeDecodeError:
                return "UNKNOWN", None
            return ("PRESENT", digest) if _SHA256.fullmatch(digest) else ("UNKNOWN", None)
        if _ABSENT_REGISTRY.search(result.stderr):
            return "ABSENT", None
        return "UNKNOWN", None

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        if not self._local_layout_exact():
            return RemoteObservation.different(
                canonical_identity({"candidateLayout": "invalid"})
            )
        state, digest = self._digest_observation(self.target)
        if state == "UNKNOWN":
            return RemoteObservation.unknown("REGISTRY_READBACK_UNKNOWN")
        if state == "PRESENT" and digest is not None:
            if digest == self.expected_digest:
                return RemoteObservation.same(intent.expected_identity)
            return RemoteObservation.different(digest)
        if self.source_reference is not None:
            source_state, source_digest = self._digest_observation(self.source_reference)
            if source_state == "PRESENT" and source_digest == self.expected_digest:
                return RemoteObservation.absent()
            if source_state == "PRESENT" and source_digest is not None:
                return RemoteObservation.different(source_digest)
            return RemoteObservation.unknown("REGISTRY_SOURCE_READBACK_UNKNOWN")
        return RemoteObservation.absent()

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        try:
            if self.source_layout is not None:
                if not self._local_layout_exact():
                    return MutationResponse.terminal("REGISTRY_LAYOUT_INVALID")
                argv = ("crane", "push", str(self.source_layout), self.target)
            else:
                repository, separator, tag = self.target.rpartition(":")
                if not separator or not tag:
                    return MutationResponse.terminal("REGISTRY_TARGET_INVALID")
                argv = ("crane", "tag", str(self.source_reference), tag)
                if not self.source_reference.startswith(repository + "@"):
                    return MutationResponse.terminal("REGISTRY_SOURCE_INVALID")
            result = self.run(argv, 180)
        except subprocess.TimeoutExpired:
            return MutationResponse.ambiguous("REGISTRY_REQUEST_TIMEOUT")
        return (
            MutationResponse.acknowledged()
            if result.returncode == 0
            else MutationResponse.ambiguous("REGISTRY_REQUEST_FAILED")
        )


class GitTagAdapter:
    def __init__(
        self,
        *,
        repository: Path,
        remote: str,
        tag: str,
        commit: str,
        subject: str,
        run: CommandRunner = run_command,
    ) -> None:
        if not _COMMIT.fullmatch(commit) or subject != tag:
            raise PublicationTransactionError("TRANSACTION_GIT_TAG_IDENTITY_INVALID")
        self.repository = Path(repository)
        self.remote = remote
        self.tag = tag
        self.commit = commit
        self.subject = subject
        self.run = run
        self.identity = canonical_identity(
            {"tag": tag, "commit": commit, "subject": subject, "body": ""}
        )

    def _git(self, *arguments: str, timeout: int = 45) -> CommandResult:
        return self.run(("git", "-C", str(self.repository), *arguments), timeout)

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        try:
            remote = self._git(
                "ls-remote", "--refs", self.remote, f"refs/tags/{self.tag}"
            )
        except subprocess.TimeoutExpired:
            return RemoteObservation.unknown("GIT_TAG_READBACK_TIMEOUT")
        if remote.returncode != 0:
            return RemoteObservation.unknown("GIT_TAG_READBACK_UNKNOWN")
        rows = [row for row in remote.stdout.splitlines() if row]
        if not rows:
            return RemoteObservation.absent()
        if len(rows) != 1:
            return RemoteObservation.unknown("GIT_TAG_READBACK_AMBIGUOUS")
        object_sha = rows[0].split(b"\t", 1)[0]
        if not re.fullmatch(rb"[0-9a-f]{40}", object_sha):
            return RemoteObservation.unknown("GIT_TAG_READBACK_INVALID")
        namespace = f"refs/transaction-readback/{self.tag}"
        fetched = self._git(
            "fetch",
            "--force",
            "--no-tags",
            self.remote,
            f"refs/tags/{self.tag}:{namespace}",
            timeout=90,
        )
        if fetched.returncode != 0:
            return RemoteObservation.unknown("GIT_TAG_READBACK_UNKNOWN")
        kind = self._git("cat-file", "-t", namespace)
        raw = self._git("cat-file", "-p", namespace)
        peeled = self._git("rev-parse", f"{namespace}^{{commit}}")
        if kind.returncode != 0 or raw.returncode != 0 or peeled.returncode != 0:
            return RemoteObservation.unknown("GIT_TAG_READBACK_UNKNOWN")
        if kind.stdout != b"tag\n":
            return RemoteObservation.different(
                canonical_identity({"tagObject": object_sha.decode("ascii")})
            )
        headers, separator, message = raw.stdout.partition(b"\n\n")
        fields: dict[bytes, bytes] = {}
        for line in headers.splitlines():
            key, space, value = line.partition(b" ")
            if not space or key in fields:
                return RemoteObservation.unknown("GIT_TAG_READBACK_INVALID")
            fields[key] = value
        actual = canonical_identity(
            {
                "tag": fields.get(b"tag", b"").decode("utf-8", errors="replace"),
                "commit": peeled.stdout.decode("ascii", errors="replace").strip(),
                "subject": message.decode("utf-8", errors="replace").rstrip("\n"),
                "body": "",
            }
        )
        if (
            fields.get(b"type") == b"commit"
            and fields.get(b"tag") == self.tag.encode("ascii")
            and peeled.stdout.decode("ascii", errors="replace").strip() == self.commit
            and message == (self.subject + "\n").encode("ascii")
        ):
            return RemoteObservation.same(intent.expected_identity)
        return RemoteObservation.different(actual)

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        local_ref = f"refs/tags/{self.tag}"
        local = self._git("show-ref", "--verify", "--quiet", local_ref)
        if local.returncode != 0:
            created = self._git(
                "-c",
                "user.name=github-actions[bot]",
                "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "tag",
                "--annotate",
                self.tag,
                self.commit,
                "--message",
                self.subject,
            )
            if created.returncode != 0:
                return MutationResponse.terminal("GIT_TAG_LOCAL_CREATE_FAILED")
        kind = self._git("cat-file", "-t", local_ref)
        raw = self._git("cat-file", "-p", local_ref)
        peeled = self._git("rev-parse", f"{local_ref}^{{commit}}")
        if (
            kind.returncode != 0
            or raw.returncode != 0
            or peeled.returncode != 0
            or kind.stdout != b"tag\n"
            or peeled.stdout.decode("ascii", errors="replace").strip() != self.commit
        ):
            return MutationResponse.terminal("GIT_TAG_LOCAL_IDENTITY_INVALID")
        headers, separator, message = raw.stdout.partition(b"\n\n")
        fields: dict[bytes, bytes] = {}
        for line in headers.splitlines():
            key, space, value = line.partition(b" ")
            if not space or key in fields:
                return MutationResponse.terminal("GIT_TAG_LOCAL_IDENTITY_INVALID")
            fields[key] = value
        if (
            separator != b"\n\n"
            or fields.get(b"type") != b"commit"
            or fields.get(b"tag") != self.tag.encode("ascii")
            or message != (self.subject + "\n").encode("ascii")
        ):
            return MutationResponse.terminal("GIT_TAG_LOCAL_IDENTITY_INVALID")
        try:
            pushed = self._git(
                "push", self.remote, f"refs/tags/{self.tag}", timeout=180
            )
        except subprocess.TimeoutExpired:
            return MutationResponse.ambiguous("GIT_TAG_PUSH_TIMEOUT")
        return (
            MutationResponse.acknowledged()
            if pushed.returncode == 0
            else MutationResponse.ambiguous("GIT_TAG_PUSH_FAILED")
        )


class GitHubReleaseAdapterBase:
    def __init__(
        self,
        *,
        repository: str,
        tag: str,
        request: GitHubRequester = github_request,
    ) -> None:
        self.repository = repository
        self.tag = tag
        self.request = request

    def _release(self) -> dict[str, Any] | None:
        encoded = urllib.parse.quote(self.tag, safe="")
        return _json_object(
            self.request("GET", f"repos/{self.repository}/releases/tags/{encoded}", None)
        )


class GitHubDraftAdapter(GitHubReleaseAdapterBase):
    def __init__(
        self,
        *,
        repository: str,
        tag: str,
        title: str,
        body: bytes,
        prerelease: bool,
        request: GitHubRequester = github_request,
    ) -> None:
        super().__init__(repository=repository, tag=tag, request=request)
        self.title = title
        self.body = body
        self.prerelease = prerelease
        self.body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        self.identity = canonical_identity(
            {
                "tag": tag,
                "title": title,
                "bodyDigest": self.body_digest,
                "prerelease": prerelease,
            }
        )

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        try:
            release = self._release()
        except ConnectionError:
            return RemoteObservation.unknown("GITHUB_DRAFT_READBACK_UNKNOWN")
        if release is None:
            return RemoteObservation.absent()
        body = release.get("body")
        actual = canonical_identity(
            {
                "tag": release.get("tag_name"),
                "title": release.get("name"),
                "bodyDigest": (
                    "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
                    if isinstance(body, str)
                    else None
                ),
                "prerelease": release.get("prerelease"),
            }
        )
        if actual == self.identity:
            return RemoteObservation.same(intent.expected_identity)
        return RemoteObservation.different(actual)

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        try:
            body = self.body.decode("utf-8", errors="strict")
            response = self.request(
                "POST",
                f"repos/{self.repository}/releases",
                {
                    "tag_name": self.tag,
                    "name": self.title,
                    "body": body,
                    "draft": True,
                    "prerelease": self.prerelease,
                    "make_latest": "false",
                },
            )
        except (UnicodeDecodeError, ConnectionError):
            return MutationResponse.ambiguous("GITHUB_DRAFT_REQUEST_UNKNOWN")
        if response.status == 201:
            return MutationResponse.acknowledged()
        return MutationResponse.ambiguous("GITHUB_DRAFT_REQUEST_FAILED")


class GitHubAssetAdapter(GitHubReleaseAdapterBase):
    def __init__(
        self,
        *,
        repository: str,
        tag: str,
        path: Path,
        expected_digest: str,
        expected_size: int,
        request: GitHubRequester = github_request,
        run: CommandRunner = run_command,
    ) -> None:
        super().__init__(repository=repository, tag=tag, request=request)
        self.path = Path(path)
        self.expected_digest = expected_digest
        self.expected_size = expected_size
        self.run = run

    def _local_exact(self) -> bool:
        if not self.path.is_file() or self.path.is_symlink():
            return False
        try:
            if self.path.stat().st_size != self.expected_size:
                return False
            digest = hashlib.sha256()
            consumed = 0
            with self.path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    consumed += len(chunk)
                    if consumed > self.expected_size:
                        return False
                    digest.update(chunk)
        except OSError:
            return False
        return consumed == self.expected_size and (
            "sha256:" + digest.hexdigest() == self.expected_digest
        )

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        try:
            release = self._release()
        except ConnectionError:
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_UNKNOWN")
        if release is None:
            return RemoteObservation.absent()
        assets = release.get("assets")
        if not isinstance(assets, list):
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_INVALID")
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == self.path.name]
        if not matches:
            return RemoteObservation.absent()
        if len(matches) != 1:
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_AMBIGUOUS")
        item = matches[0]
        digest = item.get("digest")
        size = item.get("size")
        if isinstance(digest, str) and _SHA256.fullmatch(digest):
            actual = canonical_identity({"sha256": digest, "size": size})
            if digest == self.expected_digest and size == self.expected_size:
                return RemoteObservation.same(intent.expected_identity)
            return RemoteObservation.different(actual)
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("https://api.github.com/"):
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_INVALID")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_UNKNOWN")
        try:
            with _open_github_asset_stream(url, token) as response:
                digest = hashlib.sha256()
                consumed = 0
                while chunk := response.read(1024 * 1024):
                    consumed += len(chunk)
                    if consumed > self.expected_size:
                        break
                    digest.update(chunk)
        except (OSError, TimeoutError):
            return RemoteObservation.unknown("GITHUB_ASSET_READBACK_UNKNOWN")
        actual_digest = "sha256:" + digest.hexdigest()
        actual = canonical_identity({"sha256": actual_digest, "size": consumed})
        if consumed == self.expected_size and actual_digest == self.expected_digest:
            return RemoteObservation.same(intent.expected_identity)
        return RemoteObservation.different(actual)

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        if not self._local_exact():
            return MutationResponse.terminal("GITHUB_ASSET_LOCAL_IDENTITY_INVALID")
        try:
            result = self.run(
                (
                    "gh",
                    "release",
                    "upload",
                    self.tag,
                    str(self.path),
                    "--repo",
                    self.repository,
                ),
                300,
            )
        except subprocess.TimeoutExpired:
            return MutationResponse.ambiguous("GITHUB_ASSET_UPLOAD_TIMEOUT")
        return (
            MutationResponse.acknowledged()
            if result.returncode == 0
            else MutationResponse.ambiguous("GITHUB_ASSET_UPLOAD_FAILED")
        )


class GitHubPublishAdapter(GitHubReleaseAdapterBase):
    def __init__(
        self,
        *,
        repository: str,
        tag: str,
        prerelease: bool,
        expected_assets: Mapping[str, Mapping[str, Any]],
        request: GitHubRequester = github_request,
    ) -> None:
        super().__init__(repository=repository, tag=tag, request=request)
        self.prerelease = prerelease
        self.expected_assets = {
            name: {"sha256": item["sha256"], "size": item["size"]}
            for name, item in expected_assets.items()
        }
        self.identity = canonical_identity(
            {
                "tag": tag,
                "draft": False,
                "prerelease": prerelease,
                "assets": self.expected_assets,
            }
        )

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        try:
            release = self._release()
        except ConnectionError:
            return RemoteObservation.unknown("GITHUB_PUBLISH_READBACK_UNKNOWN")
        if release is None:
            return RemoteObservation.absent()
        assets = release.get("assets")
        if not isinstance(assets, list):
            return RemoteObservation.unknown("GITHUB_PUBLISH_READBACK_INVALID")
        actual_assets: dict[str, dict[str, Any]] = {}
        for item in assets:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                return RemoteObservation.unknown("GITHUB_PUBLISH_READBACK_INVALID")
            digest = item.get("digest")
            name = item["name"]
            expected = self.expected_assets.get(name)
            if digest is None and expected is not None and item.get("size") == expected["size"]:
                # Individual asset transaction steps independently download and
                # hash every null-digest API response before Publish can run.
                digest = expected["sha256"]
            actual_assets[name] = {"sha256": digest, "size": item.get("size")}
        actual = canonical_identity(
            {
                "tag": release.get("tag_name"),
                "draft": release.get("draft"),
                "prerelease": release.get("prerelease"),
                "immutable": release.get("immutable"),
                "assets": actual_assets,
            }
        )
        if release.get("draft") is True:
            exact_subset = set(actual_assets).issubset(self.expected_assets) and all(
                actual_assets[name] == self.expected_assets[name]
                for name in actual_assets
            )
            if (
                release.get("tag_name") == self.tag
                and release.get("prerelease") is self.prerelease
                and exact_subset
            ):
                return RemoteObservation.absent()
            return RemoteObservation.different(actual)
        release_exact = (
            release.get("tag_name") == self.tag
            and release.get("draft") is False
            and release.get("prerelease") is self.prerelease
            and release.get("immutable") is True
            and actual_assets == self.expected_assets
        )
        if release_exact and not self.prerelease:
            try:
                latest = _json_object(
                    self.request(
                        "GET", f"repos/{self.repository}/releases/latest", None
                    )
                )
            except ConnectionError:
                return RemoteObservation.unknown("GITHUB_LATEST_READBACK_UNKNOWN")
            if latest is None or latest.get("tag_name") != self.tag:
                return RemoteObservation.absent()
        if release_exact:
            return RemoteObservation.same(intent.expected_identity)
        return RemoteObservation.different(actual)

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        try:
            release = self._release()
            if release is None or not isinstance(release.get("id"), int):
                return MutationResponse.terminal("GITHUB_RELEASE_ID_MISSING")
            response = self.request(
                "PATCH",
                f"repos/{self.repository}/releases/{release['id']}",
                {
                    "draft": False,
                    "prerelease": self.prerelease,
                    "make_latest": "false" if self.prerelease else "true",
                },
            )
        except ConnectionError:
            return MutationResponse.ambiguous("GITHUB_PUBLISH_REQUEST_UNKNOWN")
        return (
            MutationResponse.acknowledged()
            if response.status == 200
            else MutationResponse.ambiguous("GITHUB_PUBLISH_REQUEST_FAILED")
        )


class AttestationAdapter:
    """Observe an action-owned mutation while the controller owns its intent."""

    external_action = True

    def __init__(
        self,
        *,
        repository: str,
        workflow: str,
        source_sha: str,
        subjects: Sequence[tuple[str, str]],
        request: GitHubRequester = github_request,
        run: CommandRunner = run_command,
    ) -> None:
        self.repository = repository
        self.workflow = workflow
        self.source_sha = source_sha
        self.subjects = tuple(subjects)
        self.request = request
        self.run = run
        self.identity = canonical_identity(
            {
                "repository": repository,
                "workflow": workflow,
                "sourceSha": source_sha,
                "subjects": [
                    {"locator": locator, "digest": digest}
                    for locator, digest in self.subjects
                ],
            }
        )

    @staticmethod
    def _file_subject_digest(locator: str) -> str | None:
        path = Path(locator)
        try:
            metadata = path.lstat()
            if (
                path.is_symlink()
                or bool(getattr(path, "is_junction", lambda: False)())
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 0
                or metadata.st_size > 34359738368
            ):
                return None
            digest = hashlib.sha256()
            consumed = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    consumed += len(chunk)
                    if consumed > metadata.st_size:
                        return None
                    digest.update(chunk)
            if consumed != metadata.st_size:
                return None
        except OSError:
            return None
        return "sha256:" + digest.hexdigest()

    def observe(self, intent: MutationIntent) -> RemoteObservation:
        absence_count = 0
        for locator, digest in self.subjects:
            if not locator.startswith("oci://"):
                actual_digest = self._file_subject_digest(locator)
                if actual_digest is None:
                    return RemoteObservation.different(
                        canonical_identity({"attestationSubject": "invalid"})
                    )
                if actual_digest != digest:
                    return RemoteObservation.different(actual_digest)
            try:
                verified = self.run(
                    (
                        "gh",
                        "attestation",
                        "verify",
                        locator,
                        "--repo",
                        self.repository,
                        "--signer-workflow",
                        f"{self.repository}/.github/workflows/{self.workflow}",
                        "--source-digest",
                        self.source_sha,
                    ),
                    90,
                )
            except subprocess.TimeoutExpired:
                return RemoteObservation.unknown("ATTESTATION_READBACK_TIMEOUT")
            if verified.returncode == 0:
                continue
            encoded = urllib.parse.quote(digest, safe="")
            try:
                response = self.request(
                    "GET",
                    f"repos/{self.repository}/attestations/{encoded}",
                    None,
                )
            except ConnectionError:
                return RemoteObservation.unknown("ATTESTATION_READBACK_UNKNOWN")
            if response.status == 404:
                absence_count += 1
                continue
            if response.status != 200:
                return RemoteObservation.unknown("ATTESTATION_READBACK_UNKNOWN")
            try:
                value = json.loads(response.body.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return RemoteObservation.unknown("ATTESTATION_READBACK_INVALID")
            rows = value.get("attestations") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                return RemoteObservation.unknown("ATTESTATION_READBACK_INVALID")
            # Attestations are additive and keyed by subject + signer workflow +
            # source digest. Existing bundles from another workflow/source are
            # not a conflicting value for this intent; exact verification above
            # is the sole SAME classification.
            absence_count += 1
            continue
        if absence_count == len(self.subjects):
            return RemoteObservation.absent()
        if absence_count:
            return RemoteObservation.unknown("ATTESTATION_SET_PARTIAL")
        return RemoteObservation.same(intent.expected_identity)

    def mutate(self, intent: MutationIntent) -> MutationResponse:
        return MutationResponse.terminal("ATTESTATION_REQUIRES_EXTERNAL_ACTION")


@dataclass(frozen=True)
class PublicationRuntime:
    intents: tuple[MutationIntent, ...]
    adapters: Mapping[str, Any]
    registry_steps: tuple[str, ...]
    external_steps: tuple[str, ...]
    publication_steps: tuple[str, ...]


def build_publication_runtime(
    plan_value: Mapping[str, Any],
    *,
    source_tree: str,
    asset_root: Path,
    candidate_root: Path | None,
    repository_path: Path,
    remote: str = "origin",
    request: GitHubRequester = github_request,
    run: CommandRunner = run_command,
) -> PublicationRuntime:
    plan = validate_publication_plan(plan_value)
    if not _COMMIT.fullmatch(source_tree):
        raise PublicationTransactionError("TRANSACTION_SOURCE_TREE_INVALID")
    repository = plan["repository"]
    tag = plan["tag"]
    source_sha = plan["commit"]
    prerelease = plan["channel"] != "stable"
    asset_root = Path(asset_root)
    notes = asset_root / "release-notes.md"
    if not notes.is_file() or notes.is_symlink():
        raise PublicationTransactionError("TRANSACTION_RELEASE_NOTES_MISSING")
    body = notes.read_bytes()
    if "sha256:" + hashlib.sha256(body).hexdigest() != plan["release_notes_markdown_sha256"]:
        raise PublicationTransactionError("TRANSACTION_RELEASE_NOTES_MISMATCH")

    intents: list[MutationIntent] = []
    adapters: dict[str, Any] = {}
    registry_steps: list[str] = []
    external_steps: list[str] = []
    publication_steps: list[str] = []

    def add(name: str, kind: str, key: str, identity: str, adapter: Any, group: list[str]) -> None:
        intents.append(MutationIntent(name, kind, key, identity))
        adapters[name] = adapter
        group.append(name)

    for role in ("api", "web"):
        image_repository = f"ghcr.io/yanyuhanyue/animemo-{role}"
        digest = plan[f"{role}_digest"]
        if prerelease:
            if candidate_root is None:
                raise PublicationTransactionError("TRANSACTION_CANDIDATE_ROOT_MISSING")
            layout = Path(candidate_root) / "candidate-runtime" / "oci" / role
            targets = (("version", tag), ("source", f"sha-{source_sha}"))
            for suffix, target_tag in targets:
                name = f"registry-{role}-{suffix}"
                target = f"{image_repository}:{target_tag}"
                add(
                    name,
                    "REGISTRY_PUSH",
                    target,
                    digest,
                    RegistryAdapter(
                        target=target,
                        expected_digest=digest,
                        source_layout=layout,
                        source_role=role,
                        source_repository=image_repository,
                        run=run,
                    ),
                    registry_steps,
                )
        else:
            name = f"registry-{role}-stable"
            target = f"{image_repository}:{tag}"
            add(
                name,
                "REGISTRY_TAG",
                target,
                digest,
                RegistryAdapter(
                    target=target,
                    expected_digest=digest,
                    source_reference=f"{image_repository}@{digest}",
                    run=run,
                ),
                registry_steps,
            )

    workflow = "release.yml" if prerelease else "promote-release.yml"
    if prerelease:
        for role in ("api", "web"):
            digest = plan[f"{role}_digest"]
            locator = f"oci://ghcr.io/yanyuhanyue/animemo-{role}@{digest}"
            adapter = AttestationAdapter(
                repository=repository,
                workflow=workflow,
                source_sha=source_sha,
                subjects=((locator, digest),),
                request=request,
                run=run,
            )
            add(
                f"attestation-{role}",
                "ATTESTATION",
                locator,
                adapter.identity,
                adapter,
                external_steps,
            )

    checksum_subjects: list[tuple[str, str]] = []
    checksums = asset_root / "checksums.txt"
    if not checksums.is_file() or checksums.is_symlink():
        raise PublicationTransactionError("TRANSACTION_CHECKSUMS_MISSING")
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest_hex, separator, name = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest_hex, re.ASCII):
            raise PublicationTransactionError("TRANSACTION_CHECKSUMS_INVALID")
        path = asset_root / name
        if path.parent.resolve() != asset_root.resolve() or not path.is_file() or path.is_symlink():
            raise PublicationTransactionError("TRANSACTION_CHECKSUMS_INVALID")
        checksum_subjects.append((str(path), "sha256:" + digest_hex))
    attestation_source = (
        source_sha if prerelease else os.environ.get("GITHUB_SHA", source_sha)
    )
    for index, subject in enumerate(checksum_subjects, start=1):
        file_attestation = AttestationAdapter(
            repository=repository,
            workflow=workflow,
            source_sha=attestation_source,
            subjects=(subject,),
            request=request,
            run=run,
        )
        add(
            f"attestation-file-{index:02d}",
            "ATTESTATION",
            f"github:{repository}:attestation:{tag}:{subject[1]}",
            file_attestation.identity,
            file_attestation,
            external_steps,
        )

    tag_adapter = GitTagAdapter(
        repository=repository_path,
        remote=remote,
        tag=tag,
        commit=source_sha,
        subject=tag,
        run=run,
    )
    add(
        "git-tag",
        "GIT_TAG",
        f"git:{repository}:refs/tags/{tag}",
        tag_adapter.identity,
        tag_adapter,
        publication_steps,
    )
    draft_adapter = GitHubDraftAdapter(
        repository=repository,
        tag=tag,
        title=tag,
        body=body,
        prerelease=prerelease,
        request=request,
    )
    add(
        "release-draft",
        "GITHUB_RELEASE_DRAFT",
        f"github:{repository}:release:{tag}:draft",
        draft_adapter.identity,
        draft_adapter,
        publication_steps,
    )
    assets = declared_publication_assets(plan)
    for index, (name, item) in enumerate(assets.items(), start=1):
        adapter = GitHubAssetAdapter(
            repository=repository,
            tag=tag,
            path=asset_root / name,
            expected_digest=item["sha256"],
            expected_size=item["size"],
            request=request,
            run=run,
        )
        add(
            f"release-asset-{index:02d}",
            "GITHUB_RELEASE_ASSET",
            f"github:{repository}:release:{tag}:asset:{name}",
            canonical_identity(item),
            adapter,
            publication_steps,
        )
    publish_adapter = GitHubPublishAdapter(
        repository=repository,
        tag=tag,
        prerelease=prerelease,
        expected_assets=assets,
        request=request,
    )
    add(
        "release-publish",
        "GITHUB_RELEASE_PUBLISH",
        f"github:{repository}:release:{tag}:publish",
        publish_adapter.identity,
        publish_adapter,
        publication_steps,
    )
    return PublicationRuntime(
        intents=tuple(intents),
        adapters=adapters,
        registry_steps=tuple(registry_steps),
        external_steps=tuple(external_steps),
        publication_steps=tuple(publication_steps),
    )
