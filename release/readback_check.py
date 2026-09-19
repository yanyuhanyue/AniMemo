"""Scoped GET-only check of the production read component and seven observers."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .github_release_read import HostedGitHubReadClient, GitHubReleaseDiscovery, REPOSITORY, require
from .publication_remote import GitHubDraftAdapter, GitHubAssetAdapter, GitHubPublishAdapter
from .publication_transaction import MutationIntent

AUTHORIZATION = "ANIMEMO_V2_DRAFT_READ_AUTHORITY_FROZEN_RC2_SUCCESSOR_V1"
START = datetime(2026, 9, 19, 11, 27, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 21, 11, 27, 1, tzinfo=timezone.utc)
OUTPUT_ROOT = Path("/tmp/animemo-release-read-contract")
TARGET = "v2.0.0-rc.3"


def observe_contract(client):
    discovery = GitHubReleaseDiscovery(REPOSITORY, TARGET, client.request)
    common = dict(repository=REPOSITORY, tag=TARGET, request=client.request, discovery=discovery)
    # Expected bytes are explicit test-only comparisons. They never authorize
    # publication. In this check the target must be absent at all seven observers.
    digest = "sha256:" + hashlib.sha256(b"GET-only comparison fixture").hexdigest()
    names = ["checksums.txt", "deployment-contract.json", "installer-materials.tar",
             "release-manifest.json", f"animemo-{TARGET}-portable.tar"]
    assets = {name: {"sha256": digest, "size": 27} for name in names}
    draft = GitHubDraftAdapter(**common, title=TARGET, body=b"GET-only comparison fixture", prerelease=True)
    adapters = [("release-draft", "GITHUB_RELEASE_DRAFT", draft)]
    adapters += [("release-asset-"+str(n), "GITHUB_RELEASE_ASSET",
                  GitHubAssetAdapter(**common, path=Path(name), expected_digest=digest, expected_size=27))
                 for n, name in enumerate(names, 1)]
    publish = GitHubPublishAdapter(**common, prerelease=True, expected_assets=assets)
    adapters.append(("release-publish", "GITHUB_RELEASE_PUBLISH", publish))
    rows = []
    for name, kind, adapter in adapters:
        observation = adapter.observe(MutationIntent(name, kind, "read-check", digest))
        rows.append({"observer": name, "classification": observation.classification.value,
                     "diagnostic_code": observation.diagnostic_code})
    return {"schema": "animemo.production-release-read-check/v1", "authorization": AUTHORIZATION,
            "target": TARGET, "status": "PASS" if all(row["classification"]=="ABSENT" for row in rows) else "FAIL",
            "identity": client.identity, "observers": rows,
            "comparison_material": "SYNTHETIC_NON_AUTHORITATIVE_EXPECTATIONS_ONLY",
            "requests": client.events, "GET_count": len(client.events), "business_mutations": 0,
            "historical_P_root_cause": "UNKNOWN_NO_ORIGINAL_RESPONSES"}


def main():
    require(START <= datetime.now(timezone.utc) < END, "READ_CHECK_AUTHORIZATION_EXPIRED")
    require(os.environ.get("GITHUB_JOB")=="read-contract")
    # The fixed private root is deliberately independent of RUNNER_TEMP and all
    # caller paths. Exclusive creation rejects prior files and links.
    root = OUTPUT_ROOT
    root.mkdir(mode=0o700, exist_ok=False)
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and not root.is_symlink()
            and not bool(getattr(root, "is_junction", lambda: False)()), "READ_CHECK_OUTPUT_INVALID")
    client = None
    events = []
    result = {"status": "FAIL", "stage": "HOSTED_IDENTITY_UNVERIFIED"}
    try:
        client = HostedGitHubReadClient.from_current_job(audit_events=events)
        result = observe_contract(client)
    except Exception:
        # Exceptions may contain response bodies, URLs or credentials.
        result = {"status": "FAIL", "stage": "READ_CONTRACT_UNVERIFIED",
                  "requests": events}
    require(root.lstat().st_ino==info.st_ino and not root.is_symlink(), "READ_CHECK_OUTPUT_REPLACED")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(root / "result.json", flags, 0o600), "w", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=True, indent=2)+"\n")
    print(json.dumps({"status": result["status"], "GET_count": len(events)}))
    return 0 if result["status"]=="PASS" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("READ_CONTRACT_CHECK_FAILED_CLOSED")
        raise SystemExit(2) from None
