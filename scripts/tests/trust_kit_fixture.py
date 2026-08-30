from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from release.formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES,
    FORMAL_WINDOWS_PRETRUST_PREFIX,
)
from updater.offline import (
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    OFFLINE_POLICY_IDENTITY,
    OWNER_ID,
    REPOSITORY_ID,
    PretrustedTrustMaterial,
    TrustProfile,
)

TEST_PRETRUST_PREFIX = "release/release_attestation_verifier/pretrust-v2"


def _validate_test_namespace_owner(
    path: Path,
    metadata: os.stat_result,
    *,
    directory: bool,
) -> None:
    """Mirror production shape/mode checks under the current test UID."""

    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise AssertionError(f"测试 authority root 不是目录：{path}")
    elif not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"测试 authority material 不是普通文件：{path}")
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022
    ):
        raise AssertionError(f"测试 authority namespace 所有权/权限无效：{path}")
    if not directory and metadata.st_nlink != 1:
        raise AssertionError(f"测试 authority material 链接计数无效：{path}")


@contextmanager
def authority_test_namespace(root: Path):
    """Use a current-UID namespace without adding a production bypass."""

    root = Path(root)
    root.chmod(0o700)
    for item in root.iterdir():
        if item.is_file() and not item.is_symlink():
            item.chmod(0o600)
    with (
        mock.patch("installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT", root),
        mock.patch(
            "installer.bootstrap._validate_mode_owner",
            side_effect=_validate_test_namespace_owner,
        ),
    ):
        yield


def safe_test_state_root(path: Path) -> Path:
    """Validate an isolated current-UID trust state root for tests only."""

    path = Path(path)
    if not path.is_absolute():
        raise AssertionError("测试 trust state root 必须为绝对路径")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (
            os.name == "posix"
            and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022)
        )
    ):
        raise AssertionError("测试 trust state root 所有权/权限无效")
    return path


def load_test_pretrusted_material(root: Path) -> PretrustedTrustMaterial:
    """Load closed trust bytes under the current UID for qualification tests."""

    root = safe_test_state_root(Path(root))
    expected = {
        "github-trusted-root.jsonl",
        "github-tuf-root.json",
        "offline-release-verifier",
        "sigstore-trusted-root.jsonl",
        "sigstore-tuf-root.json",
        "trust-profile.json",
    }
    if {item.name for item in root.iterdir()} != expected:
        raise AssertionError("测试预置信任目录未关闭")
    paths = {name: root / name for name in expected}
    for name, path in paths.items():
        metadata = path.lstat()
        _validate_test_namespace_owner(path, metadata, directory=False)
        if path.is_symlink():
            raise AssertionError(f"测试预置信任文件不得为符号链接：{name}")
    profile_bytes = paths["trust-profile.json"].read_bytes()
    record = json.loads(profile_bytes.decode("utf-8"))
    if _canonical(record) != profile_bytes:
        raise AssertionError("测试 trust profile 必须为 canonical JSON")
    profile = TrustProfile.from_bootstrap_record(record)
    identities = {
        "github": _digest(paths["github-trusted-root.jsonl"].read_bytes()),
        "githubTuf": _digest(paths["github-tuf-root.json"].read_bytes()),
        "sigstore": _digest(paths["sigstore-trusted-root.jsonl"].read_bytes()),
        "sigstoreTuf": _digest(paths["sigstore-tuf-root.json"].read_bytes()),
        "verifier": _digest(paths["offline-release-verifier"].read_bytes()),
    }
    if identities != {
        "github": profile.github_trusted_root_sha256,
        "githubTuf": profile.github_tuf_root_sha256,
        "sigstore": profile.sigstore_trusted_root_sha256,
        "sigstoreTuf": profile.sigstore_tuf_root_sha256,
        "verifier": profile.verifier_identity,
    }:
        raise AssertionError("测试预置信任材料身份不一致")
    return PretrustedTrustMaterial(
        root=root,
        profile=profile,
        github_trusted_root_path=paths["github-trusted-root.jsonl"],
        sigstore_trusted_root_path=paths["sigstore-trusted-root.jsonl"],
        github_tuf_root_path=paths["github-tuf-root.json"],
        sigstore_tuf_root_path=paths["sigstore-tuf-root.json"],
        verifier_path=paths["offline-release-verifier"],
    )


def contract_only_test_pretrust_bytes() -> dict[str, bytes]:
    """Return closed fixture bytes for contracts that do not provision trust.

    These bytes deliberately carry no production authority.  Tests exercising
    provisioning use :func:`create_test_initial_trust_kit` instead.
    """

    initial = {
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
    formal = {
        f"{FORMAL_WINDOWS_PRETRUST_PREFIX}/{name}": (
            f"TEST-ONLY-FORMAL-WINDOWS:{name}\n".encode()
        )
        for name in FORMAL_WINDOWS_PRETRUST_FILES
    }
    return {**initial, **formal}


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
        "offline-release-verifier": (
            b"\x7fELF\x02\x01\x01" + bytes(9) + b"\x02\x00\x3e\x00" + bytes(44)
        ),
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
