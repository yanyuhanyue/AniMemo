"""Fixed GitHub surfaces for the one existing RC recovery.

There is no Draft creation, asset deletion, registry push or signing transport.
HTTP mutations are single attempts and never follow redirects or retry.
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .materials import read_bounded_release_file
from .publication_remote import (
    GitHubDraftAdapter,
    GitHubResponse,
    MutationResponse,
    _NoRedirect,
    _open_github_asset_stream,
    github_request,
)
from .publication_transaction import _next_snapshot
from .recovery_contract import (
    RecoveryError,
    identity,
    instant,
    policy,
    require,
    validate_bound_ledger,
)


def file_digest(path: Path) -> tuple[str, int]:
    require(
        path.is_file()
        and not path.is_symlink()
        and not getattr(path, "is_junction", lambda: False)(),
        "RECOVERY_FILE_INVALID",
    )
    before = path.stat()
    require(before.st_nlink == 1, "RECOVERY_FILE_HARDLINK")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    require(
        (before.st_size, before.st_mtime_ns, before.st_ino)
        == (after.st_size, after.st_mtime_ns, after.st_ino),
        "RECOVERY_FILE_CHANGED",
    )
    return "sha256:" + digest, after.st_size


class GitHubRecoveryRemote:
    def __init__(self, *, output: Path, before_send=None, read_only=False) -> None:
        self.read_only = read_only
        self.p = policy()
        self.output = output
        self.verified_assets: dict[int, str] = {}
        self.write_requests: list[dict[str, Any]] = []
        self.base = "repos/" + self.p["repository"]
        self.before_send = before_send
        self.read_sequence = 0
        self.last_read_diagnostic = None

    def _record_read(self, value):
        self.last_read_diagnostic = dict(value)
        with (self.output / "http-read-diagnostics.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(value, sort_keys=True) + "\n")

    def merge_identity(self, number, branch):
        from .recovery_pr import read_merge_identity

        self.read_sequence += 1
        initial = {
            "sequence": self.read_sequence,
            "phase": "REQUEST",
            "endpointCategory": "PR_MERGE_IDENTITY",
            "credentialRole": "GITHUB_TOKEN",
            "method": "GET",
            "requestedVersion": "2022-11-28",
            "status": None,
            "requestId": None,
            "selectedVersion": None,
        }
        self._record_read(initial)

        def diagnose(value):
            self._record_read({**initial, **value, "phase": "RESPONSE"})
            with (self.output / "pr-api-diagnostics.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")

        try:
            return read_merge_identity(number, branch, diagnose=diagnose)
        except Exception:
            if self.last_read_diagnostic["phase"] == "REQUEST":
                self._record_read({**initial, "phase": "NO_RESPONSE"})
            raise

    def _final_send_check(self):
        require(not self.read_only, "RECOVERY_READ_ONLY")
        require(callable(self.before_send), "RECOVERY_SEND_GUARD_MISSING")
        self.before_send()

    def get_response(
        self, endpoint: str, *, administration: bool = False
    ) -> GitHubResponse:
        path = endpoint.removeprefix(self.base).split("?", 1)[0]
        category = {
            "": "REPOSITORY",
            "/git/ref/heads/main": "CURRENT_MAIN",
            "/immutable-releases": "IMMUTABLE_SETTING",
            "/branches/main/protection": "BRANCH_PROTECTION",
            f"/releases/{self.p['ordinaryDraft']}": "ORDINARY_DRAFT",
            f"/releases/{self.p['transaction']['draft_id']}": "RC_DRAFT",
            "/releases": "RELEASE_LIST",
        }.get(path)
        if category is None:
            category = next(
                (
                    label
                    for prefix, label in (
                        ("/git/commits/", "GIT_COMMIT"),
                        ("/actions/runs/", "ACTIONS_RUN_OR_ARTIFACTS"),
                        (
                            "/actions/workflows/",
                            "WORKFLOW_HISTORY"
                            if "?" in endpoint
                            else "WORKFLOW_DEFINITION",
                        ),
                        ("/contents/", "FROZEN_DRAFTER_FILE"),
                        ("/releases/tags/", "RC_BY_TAG"),
                        ("/releases/", "RELEASE_ASSETS"),
                        ("/git/", "TAG_IDENTITY"),
                    )
                    if path.startswith(prefix)
                ),
                "OTHER_READ",
            )
        self.read_sequence += 1
        diagnostic = {
            "sequence": self.read_sequence,
            "phase": "REQUEST",
            "endpointCategory": category,
            "credentialRole": "ADMIN_READ" if administration else "GITHUB_TOKEN",
            "method": "GET",
            "requestedVersion": "2026-03-10",
            "status": None,
            "requestId": None,
            "selectedVersion": None,
        }

        self._record_read(diagnostic)
        try:
            response = self._get_response(endpoint, administration=administration)
        except Exception:
            self._record_read({**diagnostic, "phase": "NO_RESPONSE"})
            raise
        safe = lambda value: (
            value
            if type(value) is str and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value)
            else None
        )
        self._record_read(
            {
                **diagnostic,
                "phase": "RESPONSE",
                "status": response.status,
                "requestId": safe(response.request_id),
                "selectedVersion": safe(response.selected_version),
            }
        )
        return response

    def _get_response(
        self, endpoint: str, *, administration: bool = False
    ) -> GitHubResponse:
        require(
            endpoint == self.base or endpoint.startswith(self.base + "/"),
            "RECOVERY_API_TARGET_INVALID",
        )
        if not administration:
            return github_request("GET", endpoint, None)
        require(
            endpoint
            in {
                self.base + "/immutable-releases",
                self.base + "/branches/main/protection",
            },
            "RECOVERY_ADMIN_TARGET_INVALID",
        )
        token = os.environ.get("ANIMEMO_RELEASE_ADMIN_READ_TOKEN")
        require(bool(token), "RECOVERY_ADMIN_READ_CREDENTIAL_MISSING")
        request = urllib.request.Request(
            "https://api.github.com/" + endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=45
            ) as response:
                return GitHubResponse(
                    response.status,
                    response.read(),
                    response.headers.get("Link"),
                    response.headers.get("X-GitHub-Request-Id"),
                    response.headers.get("X-GitHub-Api-Version-Selected"),
                )
        except urllib.error.HTTPError as error:
            return GitHubResponse(
                error.code,
                error.read(),
                request_id=error.headers.get("X-GitHub-Request-Id"),
                selected_version=error.headers.get("X-GitHub-Api-Version-Selected"),
            )

    def get(self, endpoint: str, *, administration: bool = False) -> Any:
        response = self.get_response(endpoint, administration=administration)
        require(response.status == 200, "RECOVERY_REMOTE_READ_UNKNOWN")
        try:
            return json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise RecoveryError("RECOVERY_REMOTE_JSON_INVALID") from None

    def listed(self, endpoint: str, key: str | None = None) -> Any:
        rows: list[Any] = []
        seen: set[int] = set()
        total: int | None = None
        pagination = GitHubDraftAdapter(
            repository=self.p["repository"],
            tag=self.p["subject"]["release_tag"],
            title="",
            body=b"",
            prerelease=True,
            request=self.readonly_request,
        )
        for page in range(1, 101):
            separator = "&" if "?" in endpoint else "?"
            response = self.get_response(
                endpoint + f"{separator}per_page=100&page={page}"
            )
            require(response.status == 200, "RECOVERY_REMOTE_READ_UNKNOWN")
            try:
                value = json.loads(response.body)
                has_next = pagination._next_release_page(
                    response, page, endpoint=endpoint
                )
            except (ValueError, UnicodeDecodeError, ConnectionError):
                raise RecoveryError("RECOVERY_PAGINATION_INVALID") from None
            items = value[key] if key else value
            require(
                isinstance(items, list) and len(items) <= 100,
                "RECOVERY_PAGINATION_INVALID",
            )
            if key:
                count = value.get("total_count")
                require(
                    type(count) is int
                    and count >= 0
                    and (total is None or total == count),
                    "RECOVERY_PAGINATION_CHANGED",
                )
                total = count
            for item in items:
                require(
                    isinstance(item, dict)
                    and type(item.get("id")) is int
                    and item["id"] not in seen,
                    "RECOVERY_PAGINATION_DUPLICATE",
                )
                seen.add(item["id"])
                rows.append(item)
            require(not has_next or bool(items), "RECOVERY_PAGINATION_INCOMPLETE")
            if len(items) < 100 and not has_next:
                require(
                    total is None or total == len(rows),
                    "RECOVERY_PAGINATION_INCOMPLETE",
                )
                return {key: rows, "total_count": len(rows)} if key else rows
        raise RecoveryError("RECOVERY_PAGINATION_LIMIT")

    def readonly_request(self, method, endpoint, payload):
        require(method == "GET" and payload is None, "RECOVERY_READONLY_BOUNDARY")
        return self.get_response(endpoint)

    def draft(self, *, published_allowed: bool = False) -> dict[str, Any]:
        expected = self.p["transaction"]
        discover = GitHubDraftAdapter(
            repository=self.p["repository"],
            tag=self.p["subject"]["release_tag"],
            title=self.p["subject"]["release_tag"],
            body=b"",
            prerelease=True,
            request=self.readonly_request,
        )
        found = discover._release()
        require(
            found is not None and found.get("id") == expected["draft_id"],
            "RECOVERY_DRAFT_ID_INVALID",
        )
        value = self.get(f"{self.base}/releases/{expected['draft_id']}")
        body = value.get("body")
        require(isinstance(body, str), "RECOVERY_DRAFT_BODY_INVALID")
        require(
            value.get("id") == expected["draft_id"]
            and value.get("tag_name")
            == value.get("name")
            == self.p["subject"]["release_tag"]
            and value.get("prerelease") is True
            and value.get("target_commitish") == expected["draft_target_commitish"]
            and "sha256:" + hashlib.sha256(body.encode()).hexdigest()
            == expected["draft_body_sha256"],
            "RECOVERY_DRAFT_IDENTITY_CONFLICT",
        )
        require(
            value.get("draft") is True
            or (
                published_allowed
                and value.get("draft") is False
                and value.get("immutable") is True
                and isinstance(value.get("published_at"), str)
            ),
            "RECOVERY_DRAFT_STATE_INVALID",
        )
        assets = self.listed(f"{self.base}/releases/{expected['draft_id']}/assets")
        declared = {**self.p["plan"]["assets"], **self.p["plan"]["transport_assets"]}
        names: set[str] = set()
        for asset in assets:
            name = asset.get("name")
            require(
                name in declared and name not in names, "RECOVERY_ASSET_SET_CONFLICT"
            )
            names.add(name)
            item = declared[name]
            require(
                asset.get("state") == "uploaded"
                and type(asset.get("size")) is int
                and asset["size"] == item["size"],
                "RECOVERY_ASSET_INCOMPLETE",
            )
            require(
                asset.get("digest") is None or asset.get("digest") == item["sha256"],
                "RECOVERY_ASSET_DIGEST_CONFLICT",
            )
            require(
                asset.get("url")
                == f"https://api.github.com/{self.base}/releases/assets/{asset['id']}",
                "RECOVERY_ASSET_URL_INVALID",
            )
            fingerprint = identity(
                {
                    k: asset.get(k)
                    for k in ("id", "name", "size", "digest", "state", "updated_at")
                }
            )
            if self.verified_assets.get(asset["id"]) != fingerprint:
                token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                require(bool(token), "RECOVERY_CREDENTIAL_MISSING")
                with _open_github_asset_stream(asset["url"], token) as stream:
                    self._verify_stream(stream, item)
                self.verified_assets[asset["id"]] = fingerprint
        value["assets"] = assets
        return value

    @staticmethod
    def _verify_stream(stream, expected):
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            require(size <= expected["size"], "RECOVERY_ASSET_BYTES_CONFLICT")
            digest.update(chunk)
        require(
            size == expected["size"]
            and "sha256:" + digest.hexdigest() == expected["sha256"],
            "RECOVERY_ASSET_BYTES_CONFLICT",
        )

    def upload(self, path: Path) -> MutationResponse:
        draft = self.draft()
        declared = {**self.p["plan"]["assets"], **self.p["plan"]["transport_assets"]}
        require(
            path.name in declared
            and not any(a["name"] == path.name for a in draft["assets"]),
            "RECOVERY_UPLOAD_NOT_ABSENT",
        )
        expected = declared[path.name]
        # Snapshot the exact authorised bytes before the first network write.
        content = read_bounded_release_file(
            path, maximum=expected["size"], subject="Recovery asset"
        )
        require(
            len(content) == expected["size"]
            and "sha256:" + hashlib.sha256(content).hexdigest() == expected["sha256"],
            "RECOVERY_UPLOAD_BYTES_INVALID",
        )
        template = draft.get("upload_url")
        expected_url = f"https://uploads.github.com/{self.base}/releases/{draft['id']}/assets{{?name,label}}"
        require(template == expected_url, "RECOVERY_UPLOAD_URL_INVALID")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        require(bool(token), "RECOVERY_CREDENTIAL_MISSING")
        target = (
            f"/{self.base}/releases/{draft['id']}/assets?name="
            + urllib.parse.quote(path.name, safe="")
        )
        self._final_send_check()
        self.write_requests.append(
            {
                "method": "POST",
                "kind": "ASSET",
                "name": path.name,
                "draftId": draft["id"],
            }
        )
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=300)
        try:
            connection.request(
                "POST",
                target,
                body=content,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/octet-stream",
                    "Accept": "application/vnd.github+json",
                    "Content-Length": str(len(content)),
                },
            )
            response = connection.getresponse()
            response.read(1024 * 1024)
            return (
                MutationResponse.acknowledged()
                if response.status == 201
                else MutationResponse.ambiguous("RECOVERY_UPLOAD_RESPONSE_UNKNOWN")
            )
        except (OSError, http.client.HTTPException):
            return MutationResponse.ambiguous("RECOVERY_UPLOAD_RESPONSE_UNKNOWN")
        finally:
            connection.close()

    def publish(self) -> MutationResponse:
        draft = self.draft()
        require(len(draft["assets"]) == 5, "RECOVERY_ASSETS_INCOMPLETE")
        self._final_send_check()
        self.write_requests.append(
            {"method": "PATCH", "kind": "PUBLISH", "draftId": draft["id"]}
        )
        response = github_request(
            "PATCH",
            f"{self.base}/releases/{draft['id']}",
            {"draft": False, "prerelease": True, "make_latest": "false"},
        )
        return (
            MutationResponse.acknowledged()
            if response.status == 200
            else MutationResponse.ambiguous("RECOVERY_PUBLISH_RESPONSE_UNKNOWN")
        )

    def download_artifact(self, artifact: dict[str, Any], path: Path) -> None:
        require(not path.exists(), "RECOVERY_OUTPUT_EXISTS")
        with path.open("xb") as stream:
            result = subprocess.run(
                (
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    "-H",
                    "X-GitHub-Api-Version: 2026-03-10",
                    f"{self.base}/actions/artifacts/{artifact['id']}/zip",
                ),
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=600,
                check=False,
            )
        require(result.returncode == 0, "RECOVERY_ARTIFACT_DOWNLOAD_FAILED")
        require(
            file_digest(path) == (artifact["digest"], artifact["size_in_bytes"]),
            "RECOVERY_ARTIFACT_DIGEST_MISMATCH",
        )

    def verify_proofs(self, asset_root: Path) -> list[dict[str, Any]]:
        subjects = [
            (
                role,
                f"oci://ghcr.io/yanyuhanyue/animemo-{role}@{self.p['plan'][role + '_digest']}",
            )
            for role in ("api", "web")
        ]
        subjects += [
            (name, str(asset_root / name))
            for name in (
                "release-manifest.json",
                "deployment-contract.json",
                "installer-materials.tar",
            )
        ]
        results = []
        for name, locator in subjects:
            result = subprocess.run(
                (
                    "gh",
                    "attestation",
                    "verify",
                    locator,
                    "--repo",
                    self.p["repository"],
                    "--signer-workflow",
                    self.p["repository"] + "/.github/workflows/release.yml",
                    "--source-digest",
                    self.p["subject"]["sha"],
                    "--format",
                    "json",
                ),
                capture_output=True,
                timeout=180,
                check=False,
            )
            require(result.returncode == 0, "RECOVERY_ORIGINAL_PROOF_UNKNOWN")
            proof = json.loads(result.stdout)
            (self.output / ("original-proof-" + name + ".json")).write_bytes(
                result.stdout
            )
            results.append(
                {
                    "name": name,
                    "verifiedProofDigest": identity(proof),
                    "source": self.p["subject"]["sha"],
                    "signer": ".github/workflows/release.yml",
                }
            )
        return results


class ReadOnlyRecoveryJournal:
    """Original journal reader with an allowlist at the lowest Git transport."""

    def __init__(self, repository):
        from .publication_transaction import (
            GitRemoteAppendOnlyJournal,
            _run_git_command,
        )

        def read(command, timeout, input_bytes, environment):
            require(
                command[3]
                in {
                    "ls-remote",
                    "fetch",
                    "rev-parse",
                    "rev-list",
                    "ls-tree",
                    "show",
                    "cat-file",
                },
                "RECOVERY_READ_ONLY",
            )
            return _run_git_command(command, timeout, input_bytes, environment)

        self.__reader = GitRemoteAppendOnlyJournal(repository, run_git=read)

    def load(self, operation_id):
        return self.__reader.load(operation_id)

    def _remote_head(self, operation_id):
        return self.__reader._remote_head(operation_id)

    def append(self, value):
        raise RecoveryError("RECOVERY_READ_ONLY")


class GuardedRecoveryJournal:
    """An existing-only journal whose cursor never follows another writer."""

    def __init__(self, backend, claim, *, head_reader, clock, before_write):
        self.backend, self.claim = backend, copy.deepcopy(claim)
        self.head_reader, self.clock, self.before_write = (
            head_reader,
            clock,
            before_write,
        )
        self.recovery_binding_identity = claim["bindingDigest"]
        self.cursor = None
        self.head = None
        self.append_count = 0

    def _alive(self):
        now = self.clock()
        require(
            instant(self.claim["issuedAt"]) <= now < instant(self.claim["expiresAt"]),
            "RECOVERY_AUTHORIZATION_EXPIRED",
        )

    def claim_once(self):
        require(self.claim["mode"] == "execute", "RECOVERY_EXECUTION_NOT_AUTHORIZED")
        self._alive()
        current = self.backend.load(self.claim["operationId"])
        require(current is not None, "RECOVERY_EXISTING_TRANSACTION_MISSING")
        validate_bound_ledger(current)
        require(
            "sourceBoundRecovery" not in current
            and current["revision"] == 30
            and current["ledgerIdentity"] == self.claim["initialLedgerIdentity"]
            and self.head_reader() == self.claim["initialHead"],
            "RECOVERY_INITIAL_STATE_CHANGED",
        )
        self.cursor, self.head = current, self.claim["initialHead"]
        value = copy.deepcopy(current)
        value["sourceBoundRecovery"] = self.claim
        return self.append(_next_snapshot(value))

    def load(self, operation_id):
        require(
            operation_id == self.claim["operationId"] and self.cursor is not None,
            "RECOVERY_JOURNAL_SCOPE_INVALID",
        )
        self._alive()
        require(self.head_reader() == self.head, "RECOVERY_EXTERNAL_JOURNAL_CHANGE")
        current = self.backend.load(operation_id)
        require(
            current == self.cursor and self.head_reader() == self.head,
            "RECOVERY_EXTERNAL_JOURNAL_CHANGE",
        )
        return copy.deepcopy(current)

    def append(self, value):
        self._alive()
        self.before_write()
        current = self.load(self.claim["operationId"])
        require(
            value["sourceBoundRecovery"] == self.claim
            and value["previousLedgerIdentity"] == current["ledgerIdentity"]
            and value["revision"] == current["revision"] + 1,
            "RECOVERY_JOURNAL_CURSOR_INVALID",
        )
        accepted = self.backend.append(value)
        # backend append already requires exact commit readback. Do not silently
        # adopt a head supplied by a later independent append.
        observed_head = self.backend.last_written_head
        require(observed_head == self.head_reader(), "RECOVERY_JOURNAL_APPEND_UNKNOWN")
        require(
            self.backend.load(self.claim["operationId"]) == accepted
            and self.head_reader() == observed_head,
            "RECOVERY_JOURNAL_APPEND_UNKNOWN",
        )
        self.cursor, self.head = accepted, observed_head
        self.append_count += 1
        return copy.deepcopy(accepted)
