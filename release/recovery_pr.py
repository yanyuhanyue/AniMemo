"""One fixed PR merge-identity contract; no negotiated version or fallback."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .publication_remote import _NoRedirect
from .recovery_contract import RecoveryError, policy, require

PR_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class MergeResponse:
    status: int
    body: bytes
    selected_version: str | None = None
    request_id: str | None = None


def merge_cli_command(number):
    p = policy()
    require(
        type(number) is int
        and number in {p["sourcePr"], p["previousTool"]["sourcePr"]},
        "RECOVERY_PR_TARGET_INVALID",
    )
    return (
        "gh",
        "api",
        "--method",
        "GET",
        "--include",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{p['repository']}/pulls/{number}",
    )


def merge_cli_response(raw):
    try:
        header, body = raw.replace(b"\r\n", b"\n").split(b"\n\n", 1)
        lines = header.decode("ascii").splitlines()
        status = int(lines[0].split()[1])
        headers = {
            k.lower(): v.strip()
            for line in lines[1:]
            for k, _, v in [line.partition(":")]
        }
        return MergeResponse(
            status,
            body,
            headers.get("x-github-api-version-selected"),
            headers.get("x-github-request-id"),
        )
    except (ValueError, IndexError, UnicodeError):
        raise RecoveryError("RECOVERY_PR_RESPONSE_INVALID") from None


def merge_request(number: int) -> MergeResponse:
    p = policy()
    require(
        type(number) is int
        and number in {p["sourcePr"], p["previousTool"]["sourcePr"]},
        "RECOVERY_PR_TARGET_INVALID",
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    require(bool(token), "RECOVERY_PR_CREDENTIAL_MISSING")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{p['repository']}/pulls/{number}",
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": PR_API_VERSION,
        },
    )

    def read(response):
        return MergeResponse(
            response.status,
            response.read(4 * 1024 * 1024 + 1),
            response.headers.get("X-GitHub-Api-Version-Selected"),
            response.headers.get("X-GitHub-Request-Id"),
        )

    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=45
        ) as response:
            return read(response)
    except urllib.error.HTTPError as error:
        return read(error)
    except (OSError, TimeoutError):
        raise RecoveryError("RECOVERY_PR_TRANSPORT_UNKNOWN") from None


def validate_pr(value, *, number, branch):
    p = policy()
    require(type(value) is dict, "RECOVERY_PR_JSON_INVALID")
    require(
        type(number) is int
        and number > 0
        and type(value.get("number")) is int
        and value["number"] == number,
        "RECOVERY_PR_NUMBER_INVALID",
    )
    require(
        value.get("merged") is True and value.get("state") == "closed",
        "RECOVERY_PR_NOT_MERGED",
    )
    for side, ref in (("head", branch), ("base", "main")):
        item = value.get(side)
        require(
            type(item) is dict and type(item.get("repo")) is dict,
            "RECOVERY_PR_SOURCE_INVALID",
        )
        repo = item["repo"]
        require(
            item.get("ref") == ref
            and repo.get("id") == p["repositoryId"]
            and repo.get("full_name") == p["repository"]
            and type(repo.get("owner")) is dict
            and repo["owner"].get("id") == p["ownerId"],
            "RECOVERY_PR_SOURCE_INVALID",
        )
    require("merge_commit_sha" in value, "RECOVERY_PR_API_CONTRACT_MISMATCH")
    for key, sha in (
        ("MERGE", value["merge_commit_sha"]),
        ("HEAD", value["head"].get("sha")),
    ):
        require(
            type(sha) is str and re.fullmatch(r"[0-9a-f]{40}", sha) is not None,
            f"RECOVERY_PR_{key}_SHA_INVALID",
        )
    return value


def read_merge_identity(number, branch, *, request=None, diagnose=None):
    response = (request or merge_request)(number)
    value = None
    try:
        require(response.status == 200, f"RECOVERY_PR_HTTP_{response.status}")
        require(
            response.selected_version in {None, PR_API_VERSION},
            "RECOVERY_PR_API_VERSION_CONFLICT",
        )
        require(len(response.body) <= 4 * 1024 * 1024, "RECOVERY_PR_RESPONSE_TOO_LARGE")
        try:
            value = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise RecoveryError("RECOVERY_PR_JSON_INVALID") from None
        return validate_pr(value, number=number, branch=branch)
    finally:
        if diagnose:
            safe = lambda text: (
                text
                if type(text) is str and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", text)
                else None
            )
            diagnose(
                {
                    "endpointCategory": "PR_MERGE_IDENTITY",
                    "method": "GET",
                    "requestedVersion": PR_API_VERSION,
                    "status": response.status,
                    "mergeShaPresent": type(value) is dict
                    and "merge_commit_sha" in value,
                    "selectedVersion": safe(response.selected_version),
                    "requestId": safe(response.request_id),
                }
            )
