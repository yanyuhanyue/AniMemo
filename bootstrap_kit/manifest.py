"""Closed kit manifest authenticated by an independently supplied operator pin."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bootstrap_kit.safe_files import safe_relative

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_MANIFEST = 2 * 1024 * 1024
PLATFORMS = {"windows-amd64-cp312", "linux-amd64-cp312"}
ROLES = {"ENTRY", "LIBRARY", "VENDOR", "VERIFIER", "TRUST_ROOT", "LOCK", "SCHEMA"}
INITIAL_ROOT_PINS = {
    "github": "sha256:98cba97be9075bc98b2322de3de85fbd1b70ec7392991dfd2f53d215bede1a8d",
    "sigstore": "sha256:a0dfcc5d51c1ce4a66b541a3fff0afa97225ccc40456b21140bb2e2f113122e2",
}
REQUIRED_TRUST_ROLES = {
    "trust/trust-profile.json": "SCHEMA",
    "trust/offline-release-verifier": "VERIFIER",
    "trust/github-trusted-root.jsonl": "TRUST_ROOT",
    "trust/sigstore-trusted-root.jsonl": "TRUST_ROOT",
    "trust/github-tuf-root.json": "TRUST_ROOT",
    "trust/sigstore-tuf-root.json": "TRUST_ROOT",
}


class ManifestError(ValueError):
    pass


def require(condition, code="BOOTSTRAP_KIT_MANIFEST_INVALID"):
    if not condition:
        raise ManifestError(code)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash(value):
    return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def version(value):
    require(type(value) is str and len(value) <= 32
            and re.fullmatch(r"[1-9][0-9]{0,5}\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", value))
    return tuple(map(int, value.split(".")))


def timestamp(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value))
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        raise ManifestError("BOOTSTRAP_KIT_TIME_INVALID") from None


@dataclass(frozen=True)
class OperatorSelection:
    expected_manifest_sha256: str
    expected_platform: str
    minimum_sequence: int
    minimum_version: str
    expected_entry_sha256: str
    expected_entry_size: int
    expected_source_commit: str
    expected_source_tree: str

    def __post_init__(self):
        require(_hash(self.expected_manifest_sha256) and _hash(self.expected_entry_sha256)
                and self.expected_platform in PLATFORMS
                and type(self.minimum_sequence) is int and 1 <= self.minimum_sequence < 2**53
                and type(self.expected_entry_size) is int and 0 < self.expected_entry_size <= MAX_FILE
                and _sha(self.expected_source_commit) and _sha(self.expected_source_tree),
                "BOOTSTRAP_KIT_OPERATOR_SELECTION_INVALID")
        version(self.minimum_version)


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields))


def _identity(value):
    _fields(value, ("sha256", "size"))
    require(_hash(value["sha256"]) and type(value["size"]) is int and 0 < value["size"] <= MAX_ARCHIVE)


def validate_manifest(raw, selection, *, now, production=False):
    """External pin is checked before parsing; DEV never enters production."""
    require(type(selection) is OperatorSelection, "BOOTSTRAP_KIT_OPERATOR_SELECTION_REQUIRED")
    require(type(raw) is bytes and 0 < len(raw) <= MAX_MANIFEST)
    require(digest(raw) == selection.expected_manifest_sha256, "BOOTSTRAP_KIT_MANIFEST_PIN_MISMATCH")
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value)
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(canonical(value) == raw)
        _validate(value, selection, now=now, production=production)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ManifestError("BOOTSTRAP_KIT_MANIFEST_REJECTED") from None
    return value


def _validate(value, selection, *, now, production):
    _fields(value, ("schema", "kind", "kitVersion", "sequence", "platform", "pythonAbi",
                   "classification", "issuedAt", "expiresAt", "disabled", "minimumKitVersion",
                   "minimumSequence", "updatePolicy", "source", "build", "entry", "archive",
                   "members", "osPrerequisites", "policies", "trustRoots", "distribution"))
    require(value["schema"] == "animemo.bootstrap-trust-kit/v1"
            and value["kind"] == "ANIMEMO_BOOTSTRAP_TRUST_KIT"
            and value["pythonAbi"] == "cp312"
            and value["platform"] == selection.expected_platform
            and value["classification"] in {"DEVELOPMENT_ONLY", "PRODUCTION"})
    require(type(production) is bool and (not production or value["classification"] == "PRODUCTION"),
            "BOOTSTRAP_KIT_DEVELOPMENT_FORBIDDEN")
    require(value["classification"] == ("PRODUCTION" if production else "DEVELOPMENT_ONLY"))
    require(value["disabled"] is False and value["updatePolicy"] == "OPERATOR_RESELECT_ONLY")
    require(type(value["sequence"]) is int and selection.minimum_sequence <= value["sequence"] < 2**53)
    require(type(value["minimumSequence"]) is int and 1 <= value["minimumSequence"] <= value["sequence"])
    require(version(value["kitVersion"]) >= version(selection.minimum_version)
            and version(value["kitVersion"]) >= version(value["minimumKitVersion"]))
    require(type(now) is datetime and now.tzinfo is not None)
    issued, expiry = timestamp(value["issuedAt"]), timestamp(value["expiresAt"])
    require(issued <= now < expiry and expiry - issued <= timedelta(days=90))
    _fields(value["source"], ("repository", "commit", "tree", "locator"))
    source = value["source"]
    require(source["repository"] == "yanyuhanyue/AniMemo"
            and source["commit"] == selection.expected_source_commit
            and source["tree"] == selection.expected_source_tree
            and source["locator"] == "https://github.com/yanyuhanyue/AniMemo/tree/" + source["commit"])
    _fields(value["entry"], ("name", "sha256", "size"))
    require(value["entry"] == {"name": "entry.pyz", "sha256": selection.expected_entry_sha256,
                               "size": selection.expected_entry_size})
    _identity(value["archive"])
    _fields(value["osPrerequisites"], ("python", "stdlib", "tls", "clock"))
    require(value["osPrerequisites"] == {"python": "3.12", "stdlib": "OS_TRUSTED",
                                       "tls": "OS_CA_HOSTNAME_VERIFIED", "clock": "OPERATOR_TRUSTED_UTC"})
    _fields(value["build"], ("identity", "toolInputs"))
    require(_hash(value["build"]["identity"]))
    tools = value["build"]["toolInputs"]
    require(type(tools) is list and 1 <= len(tools) <= 128)
    names = set()
    for item in tools:
        _fields(item, ("name", "sha256", "size"))
        require(type(item["name"]) is str and re.fullmatch(r"[A-Za-z0-9_.+-]{1,100}", item["name"])
                and item["name"] not in names)
        names.add(item["name"])
        _identity({k: item[k] for k in ("sha256", "size")})
    _fields(value["policies"], ("release", "actions"))
    require(all(_hash(item) for item in value["policies"].values()))
    _fields(value["trustRoots"], ("github", "sigstore"))
    for domain, root in value["trustRoots"].items():
        _fields(root, ("initialSha256", "currentSha256", "expiresAt"))
        require(root["initialSha256"] == INITIAL_ROOT_PINS[domain] and _hash(root["currentSha256"])
                and expiry <= timestamp(root["expiresAt"]))
    require(value["trustRoots"]["github"]["initialSha256"] != value["trustRoots"]["sigstore"]["initialSha256"])
    _fields(value["distribution"], ("status", "releaseId", "manifestUrl", "archiveUrl"))
    if production:
        distribution = value["distribution"]
        prefix = "https://github.com/yanyuhanyue/AniMemo/releases/download/bootstrap-trust-kit-v" + value["kitVersion"] + "/"
        asset_base = "bootstrap-trust-kit-" + value["platform"].removesuffix("-cp312")
        require(distribution["status"] == "PUBLISHED_IMMUTABLE"
                and type(distribution["releaseId"]) is int and 0 < distribution["releaseId"] < 2**53
                and distribution["manifestUrl"] == prefix + asset_base + ".manifest.json"
                and distribution["archiveUrl"] == prefix + asset_base + ".tar")
        # This validates externally pinned delivery metadata. It neither
        # publishes those objects nor grants any OS commit authority.
    else:
        require(value["distribution"] == {"status": "NOT_PUBLISHED", "releaseId": None,
                                          "manifestUrl": None, "archiveUrl": None})
    members = value["members"]
    require(type(members) is list and 1 <= len(members) <= MAX_MEMBERS)
    seen, total = set(), 0
    for member in members:
        _fields(member, ("path", "type", "size", "sha256", "role"))
        name = safe_relative(member["path"])
        require(name.casefold() not in seen and member["type"] == "file"
                and member["role"] in ROLES and type(member["size"]) is int
                and 0 <= member["size"] <= MAX_FILE and _hash(member["sha256"]))
        seen.add(name.casefold())
        total += member["size"]
        require(total <= MAX_ARCHIVE)
    require(any(member["path"] == "library/bootstrap_kit/runtime.py" and member["role"] == "ENTRY"
                for member in members))
    by_path = {member["path"]: member for member in members}
    require(all(path in by_path and by_path[path]["role"] == role and by_path[path]["size"] > 0
                for path, role in REQUIRED_TRUST_ROLES.items()))
    require(all(value["trustRoots"][domain]["currentSha256"]
                == by_path["trust/" + domain + "-tuf-root.json"]["sha256"]
                for domain in INITIAL_ROOT_PINS))
    require(value["trustRoots"]["github"]["currentSha256"] != value["trustRoots"]["sigstore"]["currentSha256"])
    require(members == sorted(members, key=lambda member: member["path"]))
