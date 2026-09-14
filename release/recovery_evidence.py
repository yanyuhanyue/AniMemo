"""Keep original product signatures and the new recovery execution distinct."""

from __future__ import annotations

import hashlib
import re
import subprocess
import urllib.request
from pathlib import Path

from .acquisition import GitHubAttestationAcquirer
from .candidate import canonical_json_bytes
from .publication import verify_post_publish
from .recovery_contract import identity, instant, policy, require, validate_claim
from .recovery_remote import file_digest


def fixed_proof_runner(material_root: Path):
    p = policy()
    require(
        material_root.is_absolute() and material_root.resolve() == material_root,
        "RECOVERY_PROOF_ROOT_INVALID",
    )
    repository, tag = p["repository"], p["subject"]["release_tag"]
    portable = next(iter(p["plan"]["transport_assets"]))
    allowed = [
        ("gh", "release", "verify", tag, "--repo", repository, "--format", "json"),
        (
            "gh",
            "release",
            "verify-asset",
            tag,
            str(material_root / portable),
            "--repo",
            repository,
            "--format",
            "json",
        ),
    ]
    subjects = [
        "oci://ghcr.io/yanyuhanyue/animemo-" + role + "@" + p["plan"][role + "_digest"]
        for role in ("api", "web")
    ]
    subjects.extend(
        str(material_root / name)
        for name in (
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
        )
    )
    allowed.extend(
        (
            "gh",
            "attestation",
            "verify",
            subject,
            "--repo",
            repository,
            "--signer-workflow",
            repository + "/.github/workflows/release.yml",
            "--source-digest",
            p["subject"]["sha"],
            "--format",
            "json",
        )
        for subject in subjects
    )

    def run(argv):
        matches = [command for command in allowed if argv == command]
        require(len(matches) == 1, "RECOVERY_PROOF_COMMAND_INVALID")
        # Execute the command constructed above, never the callback's argv.
        result = subprocess.run(
            matches[0], capture_output=True, timeout=180, check=False
        )
        require(result.returncode == 0, "RECOVERY_PLATFORM_PROOF_UNAVAILABLE")
        return result.stdout

    return run


def collect_evidence(remote, *, material_root: Path, output: Path, execution: dict):
    p = policy()
    release = remote.draft(published_allowed=True)
    require(
        release["draft"] is False
        and release["immutable"] is True
        and len(release["assets"]) == 5,
        "RECOVERY_RELEASE_NOT_IMMUTABLE",
    )
    tag_ref = remote.get(f"{remote.base}/git/ref/tags/{p['subject']['release_tag']}")
    tag_object = remote.get(f"{remote.base}/git/tags/{p['tagObject']}")
    require(
        tag_ref.get("object", {}).get("sha") == p["tagObject"]
        and tag_object.get("object", {}).get("sha") == p["subject"]["sha"],
        "RECOVERY_TAG_CHANGED",
    )
    public = output / "anonymous-assets"
    public.mkdir(mode=0o700)
    declared = {**p["plan"]["assets"], **p["plan"]["transport_assets"]}
    observed = {}
    for asset in release["assets"]:
        name = asset["name"]
        expected_url = f"https://github.com/{p['repository']}/releases/download/{p['subject']['release_tag']}/{name}"
        require(
            asset.get("browser_download_url") == expected_url,
            "RECOVERY_PUBLIC_URL_INVALID",
        )
        # No Authorization header exists on this transport or its redirects.
        with (
            urllib.request.urlopen(expected_url, timeout=180) as stream,
            (public / name).open("xb") as target,
        ):
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                require(size <= declared[name]["size"], "RECOVERY_PUBLIC_BYTES_INVALID")
                digest.update(chunk)
                target.write(chunk)
        require(
            size == declared[name]["size"]
            and "sha256:" + digest.hexdigest() == declared[name]["sha256"],
            "RECOVERY_PUBLIC_BYTES_INVALID",
        )
        observed[name] = {"sha256": "sha256:" + digest.hexdigest(), "size": size}
    proofs = remote.verify_proofs(material_root)
    public_result = verify_post_publish(
        p["plan"],
        release={
            "tag": release["tag_name"],
            "target": p["subject"]["sha"],
            "draft": release["draft"],
            "prerelease": release["prerelease"],
            "notes_body_sha256": "sha256:"
            + hashlib.sha256(release["body"].encode()).hexdigest(),
            "public_unauthenticated_assets": True,
        },
        remote_assets=observed,
        downloaded_assets={name: public / name for name in declared},
        api_digest=p["plan"]["api_digest"],
        web_digest=p["plan"]["web_digest"],
        attestations_verified=len(proofs) == 5,
    )
    (material_root / "post-publish-verification.json").write_bytes(
        canonical_json_bytes(public_result)
    )

    portable = next(iter(p["plan"]["transport_assets"]))
    subjects = {
        "api-image": "oci://ghcr.io/yanyuhanyue/animemo-api@" + p["plan"]["api_digest"],
        "web-image": "oci://ghcr.io/yanyuhanyue/animemo-web@" + p["plan"]["web_digest"],
        "release-manifest": str(material_root / "release-manifest.json"),
        "deployment-contract": str(material_root / "deployment-contract.json"),
        "installer-materials": str(material_root / "installer-materials.tar"),
    }
    envelope = GitHubAttestationAcquirer(
        runner=fixed_proof_runner(material_root)
    ).acquire_and_export(
        repository=p["repository"],
        tag=p["subject"]["release_tag"],
        commit=p["subject"]["sha"],
        workflow=".github/workflows/release.yml",
        payload=material_root / portable,
        actions_subjects=subjects,
        destination=material_root
        / f"animemo-{p['subject']['release_tag']}-release-attestation.json",
    )
    acquisition = {
        "schema": envelope["schema"],
        "tag": envelope["tag"],
        "payload": envelope["payload"],
        "evidence_count": len(envelope["evidence"]),
        "authority_role": envelope["authorityRole"],
    }
    (material_root / "release-attestation-acquisition-receipt.json").write_bytes(
        canonical_json_bytes(acquisition)
    )
    record = {
        **execution,
        "status": "EVIDENCE_VERIFIED",
        "releaseId": release["id"],
        "releaseTag": release["tag_name"],
        "releasePublishedAt": release["published_at"],
        "publicAssetBytes": observed,
        "originalProofs": proofs,
        "sidecarSha256": file_digest(
            material_root
            / f"animemo-{p['subject']['release_tag']}-release-attestation.json"
        )[0],
        "workflowConclusion": "READ_FROM_PLATFORM_AFTER_RUN_TERMINATES",
    }
    record["recordIdentity"] = identity(record)
    validate_execution_record(record)
    return record


def validate_execution_record(record):
    """Structural binding only; readers must also verify run and journal via API."""
    p = policy()
    required = {
        "schema",
        "status",
        "claim",
        "originalPublishRun",
        "initialJournalHead",
        "finalJournalHead",
        "finalLedgerIdentity",
        "finalRevision",
        "journalAppendCount",
        "writeRequests",
        "originalDraftPostCount",
        "runtimeRebuildCount",
        "completedAt",
        "releaseId",
        "releaseTag",
        "releasePublishedAt",
        "publicAssetBytes",
        "originalProofs",
        "sidecarSha256",
        "workflowConclusion",
        "recordIdentity",
    }
    require(
        isinstance(record, dict) and set(record) == required,
        "RECOVERY_EXECUTION_RECORD_INVALID",
    )
    claim = validate_claim(record["claim"])
    require(
        claim["mode"] == "execute"
        and record["schema"] == "animemo.existing-rc-recovery-execution/v2"
        and record["status"] == "EVIDENCE_VERIFIED",
        "RECOVERY_EXECUTION_RECORD_INVALID",
    )
    require(
        record["originalPublishRun"] == p["originalPublishRun"]
        and record["initialJournalHead"] == p["transaction"]["observed_head"]
        and record["releaseId"] == p["transaction"]["draft_id"]
        and record["releaseTag"] == p["subject"]["release_tag"],
        "RECOVERY_EXECUTION_SUBJECT_INVALID",
    )
    for key in (
        "originalDraftPostCount",
        "runtimeRebuildCount",
        "finalRevision",
        "journalAppendCount",
        "releaseId",
        "originalPublishRun",
    ):
        require(type(record[key]) is int, "RECOVERY_EXECUTION_RECORD_INVALID")
    require(
        record["originalDraftPostCount"] == 0
        and record["runtimeRebuildCount"] == 0
        and record["finalRevision"] > 31
        and record["journalAppendCount"] == record["finalRevision"] - 30
        and record["workflowConclusion"] == "READ_FROM_PLATFORM_AFTER_RUN_TERMINATES",
        "RECOVERY_EXECUTION_RECORD_INVALID",
    )
    require(
        isinstance(record["finalJournalHead"], str)
        and re.fullmatch(r"[0-9a-f]{40}", record["finalJournalHead"]) is not None,
        "RECOVERY_EXECUTION_RECORD_INVALID",
    )
    for key in ("finalLedgerIdentity", "sidecarSha256", "recordIdentity"):
        require(
            isinstance(record[key], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", record[key]) is not None,
            "RECOVERY_EXECUTION_RECORD_INVALID",
        )
    require(
        instant(claim["issuedAt"])
        <= instant(record["releasePublishedAt"])
        <= instant(record["completedAt"])
        < instant(claim["expiresAt"]),
        "RECOVERY_EXECUTION_TIME_INVALID",
    )
    expected = {
        name: {"sha256": item["sha256"], "size": item["size"]}
        for name, item in {
            **p["plan"]["assets"],
            **p["plan"]["transport_assets"],
        }.items()
    }
    proofs = record["originalProofs"]
    require(
        record["publicAssetBytes"] == expected
        and isinstance(proofs, list)
        and len(proofs) == 5,
        "RECOVERY_EXECUTION_PROOF_INVALID",
    )
    require(
        all(
            isinstance(x, dict)
            and set(x) == {"name", "verifiedProofDigest", "source", "signer"}
            and x["source"] == p["subject"]["sha"]
            and x["signer"] == ".github/workflows/release.yml"
            and isinstance(x["verifiedProofDigest"], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", x["verifiedProofDigest"])
            is not None
            for x in proofs
        ),
        "RECOVERY_EXECUTION_PROOF_INVALID",
    )
    require(
        {x["name"] for x in proofs}
        == {
            "api",
            "web",
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
        },
        "RECOVERY_EXECUTION_PROOF_INVALID",
    )
    writes = record["writeRequests"]
    allowed = [
        {
            "method": "POST",
            "kind": "ASSET",
            "name": s["remoteKey"].split(":asset:", 1)[1],
            "draftId": record["releaseId"],
        }
        for s in p["steps"]
        if s["name"].startswith("release-asset-")
    ]
    allowed.append(
        {"method": "PATCH", "kind": "PUBLISH", "draftId": record["releaseId"]}
    )
    require(
        isinstance(writes, list) and len(writes) <= 6,
        "RECOVERY_EXECUTION_WRITES_INVALID",
    )
    cursor = -1
    for write in writes:
        require(write in allowed, "RECOVERY_EXECUTION_WRITES_INVALID")
        index = allowed.index(write)
        require(index > cursor, "RECOVERY_EXECUTION_WRITES_INVALID")
        cursor = index
    require(
        record["recordIdentity"]
        == identity({k: v for k, v in record.items() if k != "recordIdentity"}),
        "RECOVERY_EXECUTION_RECORD_DIGEST_INVALID",
    )
    return record
