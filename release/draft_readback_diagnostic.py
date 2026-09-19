"""Temporary, explicitly scoped hosted GET diagnostic. Not a publication entry."""
from __future__ import annotations

import datetime
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .publication_remote import (
    GitHubAssetAdapter, GitHubDraftAdapter, GitHubPublishAdapter,
    GitHubReadError, GitHubReleaseAdapterBase, _NoRedirect, read_github_response,
)
from .publication_transaction import MutationIntent

REPOSITORY = "yanyuhanyue/AniMemo"
PREFIX = "repos/" + REPOSITORY
TAG = "v2.0.0-rc.2"
CONTROL = 373784357
MAX_BODY = 4 * 1024 * 1024
OUTPUT_ROOT = Path("/tmp/animemo-rc2-draft-readback")
ASSETS = {
    "checksums.txt": (269, "a5d39f3584761a13ebd051e6789ff7339b2c61d7774a460b79abf3d9b998d421"),
    "deployment-contract.json": (48335, "e59488431dfc4349ecbdabb70085d241481e98ce2bf982097e5179a52471d6fd"),
    "installer-materials.tar": (133898240, "762f049da6ff4bed7363d0e2309afba1827a70346633d392c9967e26a2d60293"),
    "release-manifest.json": (3305, "362354439ed5bf46f53784c12ed39415f5cb47290beb8f9e7dde8b2a2476be07"),
    "animemo-v2.0.0-rc.2-portable.tar": (410081280, "a84452882342253bc00e05b53280fa0e5b2fdc056d3a94f67d2fa39eb6dbf6dc"),
}
CONTROL_FIELDS = {
    "id": CONTROL, "tag_name": "v2.0.0", "draft": True, "prerelease": False,
    "immutable": False, "published_at": None, "updated_at": "2026-09-16T05:07:36Z",
    "assets": [],
}


def shape(value):
    return {dict: "object", list: "array", str: "string", bool: "boolean",
            int: "integer", float: "number", type(None): "null"}.get(type(value), "other")


def field_shape(obj, key):
    present = isinstance(obj, dict) and key in obj
    value = obj[key] if present else None
    return {"present": present, "type": shape(value) if present else "missing",
            "boolean": value if type(value) is bool else None}


def safe_header(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value, re.ASCII) else None


def number(value):
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value) else None


class ReadOnlyProbe:
    """Capability is restricted in reviewed code, not by the token or OS sandbox.

    No request accepts a URL. Unique target IDs are allowed only after a complete,
    valid collection read. Server links are checked, never followed.
    """
    def __init__(self, token, *, opener=None):
        self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self.events = []
        self.target_id = None
        self._ids = set()
        self._matches = []
        self._next_page = 1
        self.collection_complete = False
        self.control_in_list = False
        self.control_by_id = False
        self.control_exact = False
        self.control_in_list_exact = False
        self.collection_signature = None
        self._collection_rows = []

    def endpoint(self, method, endpoint, payload):
        if method != "GET" or payload is not None or not isinstance(endpoint, str):
            raise ConnectionError("DIAGNOSTIC_ENDPOINT_DENIED")
        fixed = {
            PREFIX: ("REPOSITORY", None),
            PREFIX + "/releases/tags/" + TAG: ("BY_TAG", None),
            PREFIX + "/releases/" + str(CONTROL): ("CONTROL_ID", None),
        }
        if endpoint in fixed:
            return fixed[endpoint]
        match = re.fullmatch(re.escape(PREFIX) + r"/releases\?per_page=100&page=([1-9][0-9]{0,2})", endpoint)
        if match and int(match[1]) <= 100:
            return "LIST", int(match[1])
        if self.target_id is not None:
            root = PREFIX + "/releases/" + str(self.target_id)
            if endpoint == root:
                return "UNIQUE_ID_READBACK", None
            match = re.fullmatch(re.escape(root) + r"/assets\?per_page=100&page=([1-9][0-9]{0,2})", endpoint)
            if match and int(match[1]) <= 100:
                return "ASSETS", int(match[1])
        raise ConnectionError("DIAGNOSTIC_ENDPOINT_DENIED")

    def __call__(self, method, endpoint, payload):
        kind, page = self.endpoint(method, endpoint, payload)
        event = {"ordinal": len(self.events) + 1,
                 "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "endpoint_class": kind, "credential_role": "CURRENT_JOB_GITHUB_TOKEN",
                 "requested_api_version": "2026-03-10", "status": None,
                 "selected_version": None, "request_id": None,
                 "rate_remaining": None, "rate_reset": None, "retry_after_seconds": None,
                 "page": page, "item_count": None, "has_next": None,
                 "json_type": "unread", "body_bytes": None, "transport": "NO_RESPONSE"}
        self.events.append(event)
        request = urllib.request.Request("https://api.github.com/" + endpoint, method="GET", headers={
            "Accept": "application/vnd.github+json", "Authorization": "Bearer " + self._token,
            "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "AniMemo-publication-transaction/1",
        })
        try:
            try:
                response = self._opener.open(request, timeout=45)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                length = response.headers.get("Content-Length")
                result = read_github_response(response, MAX_BODY + 1)
            self._headers(event, result)
            event["body_bytes"] = len(result.body)
            if len(result.body) > MAX_BODY or number(length) is not None and number(length) > MAX_BODY:
                event["transport"] = "BODY_LIMIT_EXCEEDED"
                raise ConnectionError("DIAGNOSTIC_BODY_LIMIT_EXCEEDED")
            # HTTPResponse.read(amount) does not always raise on early EOF.
            # Check advertised framing even when the prefix is valid JSON.
            if length is not None and (
                number(length) is None or number(length) != len(result.body)
                or hasattr(response.headers, "get_all")
                and len(response.headers.get_all("Content-Length")) != 1
            ):
                raise GitHubReadError(result)
            event["transport"] = "COMPLETE"
        except GitHubReadError as error:
            self._headers(event, error.response)
            event["transport"] = "BODY_INTERRUPTED"
            raise ConnectionError("DIAGNOSTIC_BODY_INTERRUPTED") from None
        except (OSError, http.client.HTTPException):
            raise ConnectionError("DIAGNOSTIC_TRANSPORT_UNKNOWN") from None
        try:
            value = json.loads(result.body.decode("utf-8", errors="strict"))
            event["json_type"] = shape(value)
        except (ValueError, UnicodeError):
            event["json_type"] = "invalid"
            return result
        if kind == "REPOSITORY":
            event["permissions"] = field_shape(value, "permissions")
            event["push"] = field_shape(value.get("permissions") if isinstance(value, dict) else None, "push")
            event["repository_identity_matches"] = isinstance(value, dict) and type(value.get("id")) is int and value["id"] == 1327429673 and value.get("full_name") == REPOSITORY
        if kind == "LIST":
            self._collection(event, result, value, page)
        if kind == "CONTROL_ID":
            self.control_by_id = result.status == 200 and isinstance(value, dict) and type(value.get("id")) is int and value["id"] == CONTROL and value.get("tag_name") == "v2.0.0" and value.get("draft") is True
            self.control_exact = result.status == 200 and exact_control(value)
            event["control_visible"] = self.control_by_id
            event["frozen_control_matches"] = self.control_exact
        if kind == "UNIQUE_ID_READBACK":
            event["identity_matches"] = result.status == 200 and isinstance(value, dict) and type(value.get("id")) is int and value["id"] == self.target_id and value.get("tag_name") == TAG
        return result

    @staticmethod
    def _headers(event, result):
        event.update(status=result.status if type(result.status) is int else None,
                     selected_version=safe_header(result.selected_version, r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
                     request_id=safe_header(result.request_id, r"[0-9A-Fa-f:]{1,128}"),
                     rate_remaining=number(result.rate_remaining), rate_reset=number(result.rate_reset),
                     retry_after_seconds=number(result.retry_after))

    def _collection(self, event, result, value, page):
        if page == 1:
            self._ids, self._matches = set(), []
            self._next_page, self.target_id = 1, None
            self.collection_complete, self.control_in_list = False, False
            self.control_in_list_exact, self.collection_signature = False, None
            self._collection_rows = []
        event["item_count"] = len(value) if isinstance(value, list) else None
        try:
            has_next = GitHubReleaseAdapterBase(repository=REPOSITORY, tag=TAG)._next_release_page(result, page)
            event["has_next"] = has_next
            if result.status != 200 or page != self._next_page or not isinstance(value, list) or len(value) > 100:
                raise ValueError
            for item in value:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0 or item["id"] in self._ids or not isinstance(item.get("tag_name"), str):
                    raise ValueError
                self._ids.add(item["id"])
                self._collection_rows.append((item["id"], item["tag_name"], item.get("draft"), item.get("updated_at")))
                if item["tag_name"] == TAG:
                    self._matches.append(item["id"])
                if item["id"] == CONTROL and item["tag_name"] == "v2.0.0" and item.get("draft") is True:
                    self.control_in_list = True
                    self.control_in_list_exact = exact_control(item)
            if has_next and not value:
                raise ValueError
            self.collection_complete = not has_next and len(value) < 100
            self._next_page = page + 1
            if self.collection_complete and len(self._matches) == 1:
                self.target_id = self._matches[0]
            if self.collection_complete:
                self.collection_signature = tuple(sorted(self._collection_rows))
            event.update(target_match_count=len(self._matches), control_visible=self.control_in_list,
                         collection_complete=self.collection_complete)
        except (ConnectionError, ValueError):
            self.target_id = None
            self._next_page = -1
            self.collection_complete = False
            event["collection_complete"] = False


def exact_control(value):
    """All frozen control fields must be explicitly present with exact types."""
    return isinstance(value, dict) and all(
        key in value and type(value[key]) is type(expected) and value[key] == expected
        for key, expected in CONTROL_FIELDS.items()
    )


def witnessed_absence(probe, original):
    """Non-authoritative prototype, never wired into a publication controller.

    A false/missing push metadata field is not granted a default. Instead require
    actual visibility of the protected draft in two complete, unchanged listings
    and two exact ID readbacks, plus exact repository identity, on this guarded
    job's single request transport. Any missing, partial or drifting evidence is
    UNKNOWN. This proves this read window only, not future mutation permission.
    """
    def sufficient(result):
        return (result["classification"] == "UNKNOWN" and result["diagnostic_code"] in {
            "GITHUB_RELEASE_DRAFT_VISIBILITY_UNVERIFIED", "GITHUB_RELEASE_PERMISSIONS_SHAPE_UNVERIFIED"
        } and probe.collection_complete and probe.target_id is None and not probe._matches
            and probe.control_in_list_exact and probe.control_exact
            and probe.collection_signature is not None
            and all(event["selected_version"] == "2026-03-10" and event["transport"] == "COMPLETE"
                    for event in probe.events)
            and all(type(row[2]) is bool and isinstance(row[3], str) for row in probe.collection_signature)
            and any(row.get("endpoint_class") == "REPOSITORY" and row.get("status") == 200
                    and row.get("repository_identity_matches") is True for row in probe.events[-2:]))
    result = {"authority": "NON_AUTHORITATIVE_RECOVERY_PROTOTYPE", "classification": "UNKNOWN",
              "code": "CONTROL_WITNESS_UNVERIFIED", "production_classifier_changed": False}
    if not sufficient(original):
        return result
    signature = probe.collection_signature
    probe.control_exact = False
    repeated = observe_draft(probe)
    try:
        probe("GET", PREFIX + "/releases/" + str(CONTROL), None)
    except ConnectionError:
        return result
    if sufficient(repeated) and signature == probe.collection_signature:
        result.update(classification="ABSENT", code="EXACT_CONTROL_AND_COMPLETE_STABLE_COLLECTIONS")
    return result


def observe_draft(probe):
    notes = Path(__file__).with_name("diagnostic-rc2-notes.txt").read_bytes()
    if hashlib.sha256(notes).hexdigest() != "1678476e51b67548225bed983840c806421bfe23f746e2d66ce234cb41d13dd2":
        raise ValueError("DIAGNOSTIC_NOTES_IDENTITY_INVALID")
    adapter = GitHubDraftAdapter(repository=REPOSITORY, tag=TAG, title=TAG,
                                 body=notes, prerelease=True, request=probe)
    result = adapter.observe(MutationIntent("release-draft", "GITHUB_RELEASE_DRAFT", "diagnostic", adapter.identity))
    return {"classification": result.classification.value, "diagnostic_code": result.diagnostic_code}


def run_probe(probe):
    original = observe_draft(probe)
    # Observe positive control separately; it never changes the classifier.
    try:
        probe("GET", PREFIX + "/releases/" + str(CONTROL), None)
        control_status = "VISIBLE" if probe.control_by_id else "UNVERIFIED"
    except ConnectionError:
        control_status = "UNKNOWN"
    alternative = witnessed_absence(probe, original)
    assets_status = "NOT_OBSERVED_NO_UNIQUE_TARGET"
    if probe.target_id is not None:
        adapter = GitHubReleaseAdapterBase(repository=REPOSITORY, tag=TAG, request=probe)
        endpoint = PREFIX + "/releases/" + str(probe.target_id) + "/assets"
        ids = set()
        try:
            for page in range(1, 101):
                response = probe("GET", endpoint + "?per_page=100&page=" + str(page), None)
                if response.status != 200:
                    raise ValueError
                items = json.loads(response.body.decode("utf-8"))
                if not isinstance(items, list) or len(items) > 100:
                    raise ValueError
                for item in items:
                    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0 or item["id"] in ids:
                        raise ValueError
                    ids.add(item["id"])
                more = adapter._next_release_page(response, page, endpoint=endpoint)
                if more and not items:
                    raise ValueError
                if len(items) < 100 and not more:
                    assets_status = "COMPLETE"
                    break
            else:
                raise ValueError
        except (ConnectionError, ValueError, UnicodeError):
            assets_status = "UNKNOWN"
    return {"schema": "animemo.hosted-draft-readback-diagnostic/v1",
            "original_classifier": original, "control_by_id": control_status,
            "alternative_read_contract": alternative,
            "control_in_list": probe.control_in_list, "collection_complete": probe.collection_complete,
            "canonical_assets": assets_status, "requests": probe.events,
            "GET_count": len(probe.events), "business_mutations": 0,
            "historical_P_root_cause": "UNKNOWN_NO_ORIGINAL_RESPONSES"}


def main():
    token = os.environ.pop("GH_TOKEN", None)
    if not token or os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError("DIAGNOSTIC_CREDENTIAL_INVALID")
    root = OUTPUT_ROOT
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("DIAGNOSTIC_OUTPUT_DIRECTORY_INVALID")
    # Bootstrap before checkout has independently verified the live PR and run.
    identity_path = root / "identity.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise RuntimeError("DIAGNOSTIC_IDENTITY_FILE_INVALID")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity["checkout_sha"] != os.environ["CHECKOUT_SHA"]:
        raise RuntimeError("DIAGNOSTIC_CHECKOUT_INVALID")
    probe = ReadOnlyProbe(token)
    try:
        result = run_probe(probe)
        result["runtime"] = {"python_version": list(sys.version_info[:3]),
                             "implementation": sys.implementation.name,
                             "runner_python": "/usr/bin/python3", "dependency_lock": "release/requirements.lock"}
    except Exception:
        # Never print raw exceptions or server content. Preserve safe events.
        result = {"status": "DIAGNOSTIC_INTERNAL_FAILURE", "requests": probe.events}
        with (root / "diagnostic.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
        raise RuntimeError("DIAGNOSTIC_INTERNAL_FAILURE") from None
    with (root / "diagnostic.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"classification": result["original_classifier"], "GET_count": result["GET_count"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("DIAGNOSTIC_FAILED_CLOSED")
        raise SystemExit(2) from None
