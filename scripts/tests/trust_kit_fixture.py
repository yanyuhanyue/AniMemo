from __future__ import annotations

import hashlib
import json
from pathlib import Path

from updater.offline import (
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    OFFLINE_POLICY_IDENTITY,
    OWNER_ID,
    REPOSITORY_ID,
    TrustProfile,
)

TEST_PRETRUST_PREFIX = "release/release_attestation_verifier/pretrust-v2"


def contract_only_test_pretrust_bytes() -> dict[str, bytes]:
    """Return closed fixture bytes for contracts that do not provision trust.

    These bytes deliberately carry no production authority.  Tests exercising
    provisioning use :func:`create_test_initial_trust_kit` instead.
    """

    return {
        f"{TEST_PRETRUST_PREFIX}/{name}": f"TEST-ONLY:{name}\n".encode()
        for name in (
            "github-trusted-root.jsonl",
            "github-tuf-root.json",
            "initial-trust-bootstrap.json",
            "offline-release-verifier",
            "sigstore-trusted-root.jsonl",
            "sigstore-tuf-root.json",
            "trust-profile.json",
        )
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def create_test_initial_trust_kit(root: Path) -> Path:
    """Create closed non-production bytes for material-profile tests only."""

    kit = Path(root) / "test-only-pretrust-v2"
    kit.mkdir()
    runtime = {
        "github-trusted-root.jsonl": b'{"fixture":"github-trusted-root"}\n',
        "github-tuf-root.json": b'{"fixture":"github-tuf-root"}\n',
        "offline-release-verifier": b"TEST-ONLY-NON-EXECUTABLE-VERIFIER\n",
        "sigstore-trusted-root.jsonl": b'{"fixture":"sigstore-trusted-root"}\n',
        "sigstore-tuf-root.json": b'{"fixture":"sigstore-tuf-root"}\n',
    }
    profile = TrustProfile(
        profile_version=1,
        parent_profile_identity=None,
        repository="yanyuhanyue/AniMemo",
        repository_id=REPOSITORY_ID,
        owner_id=OWNER_ID,
        github_release_certificate_identity=GITHUB_RELEASE_CERTIFICATE_IDENTITY,
        github_trusted_root_sha256=_digest(runtime["github-trusted-root.jsonl"]),
        sigstore_trusted_root_sha256=_digest(runtime["sigstore-trusted-root.jsonl"]),
        github_tuf_root_sha256=_digest(runtime["github-tuf-root.json"]),
        github_tuf_root_version=1,
        github_tuf_timestamp_version=1,
        github_tuf_snapshot_version=1,
        github_tuf_targets_version=1,
        sigstore_tuf_root_sha256=_digest(runtime["sigstore-tuf-root.json"]),
        sigstore_tuf_root_version=1,
        sigstore_tuf_timestamp_version=1,
        sigstore_tuf_snapshot_version=1,
        sigstore_tuf_targets_version=1,
        verifier_id="github-sigstore-offline",
        minimum_verifier_version="2.97.0",
        revocation_epoch=0,
        revocation_snapshot_sha256=_digest(_canonical({"fixture": "empty"})),
        verifier_identity=_digest(runtime["offline-release-verifier"]),
        policy_identity=OFFLINE_POLICY_IDENTITY,
        activation_sequence=1,
    )
    runtime["trust-profile.json"] = _canonical(profile.as_bootstrap_record())
    for name, value in runtime.items():
        (kit / name).write_bytes(value)
    manifest = {
        "authorityRole": "PRODUCTION_PRETRUST_ONLY",
        "files": [
            {
                "mode": "0755" if name == "offline-release-verifier" else "0644",
                "name": name,
                "sha256": _digest(value),
                "size": len(value),
            }
            for name, value in sorted(runtime.items())
        ],
        "profileIdentity": profile.identity,
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
        "schemaVersion": 1,
        "stage0Model": "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY",
    }
    (kit / "initial-trust-bootstrap.json").write_bytes(_canonical(manifest))
    return kit
