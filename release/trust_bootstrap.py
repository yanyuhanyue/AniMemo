from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from release.publication_evidence import (
    GITHUB_RELEASE_CERTIFICATE_IDENTITY,
    OWNER_ID,
    REPOSITORY_ID,
)
from updater.offline import OFFLINE_POLICY_IDENTITY, TrustProfile, _canonical_json_bytes

_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_VERIFIER_BYTES = 64 * 1024 * 1024
_VERIFIER_VERSION = "2.97.0+animemo.1"
_MINIMUM_VERIFIER_VERSION = "2.97.0"
_REPOSITORY = "yanyuhanyue/AniMemo"
_AUTHORITY_MODEL = "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY"
INITIAL_TRUST_KIT_FILES = frozenset(
    {
        "github-trusted-root.jsonl",
        "github-tuf-root.json",
        "initial-trust-bootstrap.json",
        "offline-release-verifier",
        "sigstore-trusted-root.jsonl",
        "sigstore-tuf-root.json",
        "trust-profile.json",
    }
)


class TUFMetadataNotFound(FileNotFoundError):
    pass


class TrustBootstrapError(ValueError):
    pass


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


_TRACKS = {
    "github": {
        "repository": "https://tuf-repo.github.com",
        "bootstrap": (
            "https://raw.githubusercontent.com/cli/cli/v2.97.0/"
            "pkg/cmd/attestation/verification/embed/tuf-repo.github.com/root.json"
        ),
        "bootstrapSha256": (
            "sha256:98cba97be9075bc98b2322de3de85fbd1b70ec7392991dfd2f53d215bede1a8d"
        ),
        "bootstrapVersion": 3,
    },
    "sigstore": {
        "repository": "https://tuf-repo-cdn.sigstore.dev",
        "bootstrap": (
            "https://raw.githubusercontent.com/sigstore/sigstore-go/v1.2.2/"
            "pkg/tuf/repository/root.json"
        ),
        "bootstrapSha256": (
            "sha256:a0dfcc5d51c1ce4a66b541a3fff0afa97225ccc40456b21140bb2e2f113122e2"
        ),
        "bootstrapVersion": 14,
    },
}


def _production_fetch(url: str, maximum: int) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise TrustBootstrapError("官方 TUF URL 无效")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AniMemo/1.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != parsed.hostname:
                raise TrustBootstrapError("官方 TUF 下载发生跨主机重定向")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum:
                raise TrustBootstrapError("官方 TUF metadata 超出上限")
            value = response.read(maximum + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise TUFMetadataNotFound(url) from error
        raise TrustBootstrapError("官方 TUF metadata 下载失败") from error
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise TrustBootstrapError("官方 TUF metadata 下载失败") from error
    if not 1 <= len(value) <= maximum:
        raise TrustBootstrapError("官方 TUF metadata 大小无效")
    return value


def _json_object(value: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustBootstrapError(f"{label} 不是 JSON") from error
    if type(decoded) is not dict:
        raise TrustBootstrapError(f"{label} 不是对象")
    return decoded


def _positive_version(value: object, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TrustBootstrapError(f"{label} 版本无效")
    return value


def _metadata_version(value: bytes, *, label: str) -> int:
    decoded = _json_object(value, label=label)
    try:
        signed = decoded["signed"]
        version = signed["version"]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise TrustBootstrapError(f"{label} 版本缺失") from error
    return _positive_version(version, label=label)


def _meta_version(value: bytes, name: str, *, label: str) -> int:
    decoded = _json_object(value, label=label)
    try:
        signed = decoded["signed"]
        metadata = signed["meta"]  # type: ignore[index]
        version = metadata[name]["version"]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise TrustBootstrapError(f"{label} 未关闭 {name}") from error
    return _positive_version(version, label=f"{label}/{name}")


def _trusted_root_identity(targets: bytes) -> tuple[str, int]:
    decoded = _json_object(targets, label="TUF targets")
    try:
        signed = decoded["signed"]
        target = signed["targets"]["trusted_root.json"]  # type: ignore[index]
        hashes = target["hashes"]
        digest = hashes["sha256"]
        length = target["length"]
    except (KeyError, TypeError) as error:
        raise TrustBootstrapError("TUF targets 未授权 trusted_root.json") from error
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(length) is not int
        or not 1 <= length <= _MAX_METADATA_BYTES
    ):
        raise TrustBootstrapError("TUF trusted_root target 身份无效")
    return digest, length


def _acquire_track(
    role: str,
    *,
    fetcher: Callable[[str, int], bytes],
) -> tuple[bytes, list[bytes], bytes, bytes, bytes, bytes]:
    config = _TRACKS[role]
    bootstrap = fetcher(str(config["bootstrap"]), _MAX_METADATA_BYTES)
    if (
        _digest(bootstrap) != config["bootstrapSha256"]
        or _metadata_version(bootstrap, label=f"{role} bootstrap root")
        != config["bootstrapVersion"]
    ):
        raise TrustBootstrapError(f"{role} bootstrap root 身份无效")
    repository = str(config["repository"])
    roots: list[bytes] = []
    for version in range(int(config["bootstrapVersion"]) + 1, int(config["bootstrapVersion"]) + 33):
        try:
            candidate = fetcher(f"{repository}/{version}.root.json", _MAX_METADATA_BYTES)
        except TUFMetadataNotFound:
            break
        if _metadata_version(candidate, label=f"{role} root") != version:
            raise TrustBootstrapError(f"{role} root chain 不连续")
        roots.append(candidate)
    timestamp = fetcher(f"{repository}/timestamp.json", _MAX_METADATA_BYTES)
    snapshot_version = _meta_version(timestamp, "snapshot.json", label=f"{role} timestamp")
    snapshot = fetcher(
        f"{repository}/{snapshot_version}.snapshot.json",
        _MAX_METADATA_BYTES,
    )
    targets_version = _meta_version(snapshot, "targets.json", label=f"{role} snapshot")
    targets = fetcher(
        f"{repository}/{targets_version}.targets.json",
        _MAX_METADATA_BYTES,
    )
    target_digest, target_size = _trusted_root_identity(targets)
    trusted_root = fetcher(
        f"{repository}/targets/{target_digest}.trusted_root.json",
        _MAX_METADATA_BYTES,
    )
    if len(trusted_root) != target_size or _digest(trusted_root) != "sha256:" + target_digest:
        raise TrustBootstrapError(f"{role} trusted_root transport 身份无效")
    return bootstrap, roots, timestamp, snapshot, targets, trusted_root


def _read_verifier(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TrustBootstrapError("离线验证器不可用") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= _MAX_VERIFIER_BYTES
    ):
        raise TrustBootstrapError("离线验证器文件类型无效")
    value = path.read_bytes()
    if len(value) != metadata.st_size:
        raise TrustBootstrapError("离线验证器读取期间发生变化")
    return value


def validate_initial_trust_kit(root: Path) -> TrustProfile:
    """验证 build-time kit 的封闭字节集合及 profile/manifest 交叉绑定。"""

    root = Path(root)
    try:
        metadata = root.lstat()
        names = {item.name for item in root.iterdir()}
    except OSError as error:
        raise TrustBootstrapError("初始 pretrust kit 不可用") from error
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or names != INITIAL_TRUST_KIT_FILES:
        raise TrustBootstrapError("初始 pretrust kit 文件集合未关闭")
    files: dict[str, bytes] = {}
    for name in sorted(INITIAL_TRUST_KIT_FILES):
        path = root / name
        try:
            item = path.lstat()
        except OSError as error:
            raise TrustBootstrapError("初始 pretrust kit 文件不可用") from error
        if (
            path.is_symlink()
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or not 1 <= item.st_size <= _MAX_VERIFIER_BYTES
        ):
            raise TrustBootstrapError("初始 pretrust kit 文件类型无效")
        files[name] = path.read_bytes()
        if len(files[name]) != item.st_size:
            raise TrustBootstrapError("初始 pretrust kit 文件读取期间变化")
    try:
        profile_record = json.loads(files["trust-profile.json"].decode("utf-8"))
        manifest = json.loads(files["initial-trust-bootstrap.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustBootstrapError("初始 pretrust kit JSON 不可解析") from error
    if (
        _canonical_json_bytes(profile_record) != files["trust-profile.json"]
        or _canonical_json_bytes(manifest) != files["initial-trust-bootstrap.json"]
    ):
        raise TrustBootstrapError("初始 pretrust kit JSON 不是 canonical")
    try:
        profile = TrustProfile.from_bootstrap_record(profile_record)
    except Exception as error:
        raise TrustBootstrapError("初始 pretrust profile 无效") from error
    runtime_files = {name: value for name, value in files.items() if name != "initial-trust-bootstrap.json"}
    expected_manifest = {
        "authorityRole": "PRODUCTION_PRETRUST_ONLY",
        "files": [
            {
                "mode": "0755" if name == "offline-release-verifier" else "0644",
                "name": name,
                "sha256": _digest(value),
                "size": len(value),
            }
            for name, value in sorted(runtime_files.items())
        ],
        "profileIdentity": profile.identity,
        "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
        "schemaVersion": 1,
        "stage0Model": _AUTHORITY_MODEL,
    }
    if manifest != expected_manifest:
        raise TrustBootstrapError("初始 pretrust manifest 绑定无效")
    if (
        profile.github_trusted_root_sha256 != _digest(files["github-trusted-root.jsonl"])
        or profile.sigstore_trusted_root_sha256 != _digest(files["sigstore-trusted-root.jsonl"])
        or profile.github_tuf_root_sha256 != _digest(files["github-tuf-root.json"])
        or profile.sigstore_tuf_root_sha256 != _digest(files["sigstore-tuf-root.json"])
        or profile.verifier_identity != _digest(files["offline-release-verifier"])
    ):
        raise TrustBootstrapError("初始 pretrust profile 与材料身份不一致")
    return profile


def _run_verifier(
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
    command: tuple[str, ...],
) -> bytes:
    environment = {"LANG": "C", "LC_ALL": "C"}
    if os.name == "nt" and "SystemRoot" in os.environ:
        environment["SystemRoot"] = os.environ["SystemRoot"]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            env=environment,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TrustBootstrapError("离线验证器执行失败") from error
    if completed.returncode != 0 or completed.stderr or not completed.stdout:
        raise TrustBootstrapError("离线验证器拒绝 TUF bootstrap")
    return completed.stdout


def _close_claim(
    value: object,
    *,
    bootstrap_identity: str,
    tracks: Mapping[str, tuple[bytes, list[bytes], bytes, bytes, bytes, bytes]],
) -> dict[str, Mapping[str, object]]:
    if type(value) is not dict or set(value) != {
        "authorityRole",
        "fromProfileIdentity",
        "github",
        "schemaVersion",
        "sigstore",
    }:
        raise TrustBootstrapError("TUF bootstrap claim 字段未关闭")
    if (
        value["schemaVersion"] != 1
        or value["authorityRole"] != "TRUST_METADATA_ONLY"
        or value["fromProfileIdentity"] != bootstrap_identity
    ):
        raise TrustBootstrapError("TUF bootstrap claim authority binding 无效")
    closed: dict[str, Mapping[str, object]] = {}
    for role in ("github", "sigstore"):
        item = value[role]
        if type(item) is not dict or set(item) != {
            "revokedSignerKeyIds",
            "snapshotVersion",
            "supersededMaterialIdentities",
            "targetsVersion",
            "timestampVersion",
            "trustedRootSha256",
            "tufRootSha256",
            "tufRootVersion",
        }:
            raise TrustBootstrapError("TUF bootstrap track claim 字段未关闭")
        bootstrap, roots, timestamp, snapshot, targets, trusted_root = tracks[role]
        final_root = roots[-1] if roots else bootstrap
        if (
            item["revokedSignerKeyIds"] != []
            or item["supersededMaterialIdentities"] != []
            or item["tufRootSha256"] != _digest(final_root)
            or item["tufRootVersion"] != _metadata_version(final_root, label=f"{role} root")
            or item["timestampVersion"] != _metadata_version(timestamp, label=f"{role} timestamp")
            or item["snapshotVersion"] != _metadata_version(snapshot, label=f"{role} snapshot")
            or item["targetsVersion"] != _metadata_version(targets, label=f"{role} targets")
            or item["trustedRootSha256"] != _digest(trusted_root)
        ):
            raise TrustBootstrapError("TUF bootstrap track claim 与取得材料不一致")
        closed[role] = item
    return closed


def build_initial_trust_kit(
    *,
    verifier: Path,
    output: Path,
    fetcher: Callable[[str, int], bytes] = _production_fetch,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, object]:
    """取得并验证两套官方 TUF 元数据，生成不可自授权的初始 pretrust kit。"""

    verifier = Path(verifier)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise TrustBootstrapError("初始 pretrust 输出必须不存在")
    verifier_bytes = _read_verifier(verifier)
    version_output = _run_verifier(runner, (str(verifier), "--version"))
    if version_output != (_VERIFIER_VERSION + "\n").encode("ascii"):
        raise TrustBootstrapError("离线验证器版本不合格")

    tracks = {
        role: _acquire_track(role, fetcher=fetcher)
        for role in ("github", "sigstore")
    }
    bootstrap_identity = _digest(
        _canonical_json_bytes(
            {
                "authorityModel": _AUTHORITY_MODEL,
                "githubBootstrapRoot": _digest(tracks["github"][0]),
                "schemaVersion": 1,
                "sigstoreBootstrapRoot": _digest(tracks["sigstore"][0]),
            }
        )
    )

    def package_track(role: str) -> dict[str, object]:
        _, roots, timestamp, snapshot, targets, trusted_root = tracks[role]
        return {
            "rootChain": [base64.b64encode(item).decode("ascii") for item in roots],
            "snapshot": base64.b64encode(snapshot).decode("ascii"),
            "targets": base64.b64encode(targets).decode("ascii"),
            "timestamp": base64.b64encode(timestamp).decode("ascii"),
            "trustedRoot": base64.b64encode(trusted_root).decode("ascii"),
        }

    package = {
        "authorityRole": "TRUST_METADATA_ONLY",
        "fromProfileIdentity": bootstrap_identity,
        "github": package_track("github"),
        "schemaVersion": 1,
        "sigstore": package_track("sigstore"),
    }

    def request_track(role: str) -> dict[str, object]:
        bootstrap = tracks[role][0]
        return {
            "snapshotVersion": 0,
            "targetsVersion": 0,
            "timestampVersion": 0,
            "trustedRootSha256": _digest(bootstrap),
            "tufRootSha256": _digest(bootstrap),
            "tufRootVersion": _metadata_version(bootstrap, label=f"{role} bootstrap root"),
        }

    request = {
        "authorityRole": "TRUST_METADATA_ONLY",
        "fromProfileIdentity": bootstrap_identity,
        "github": request_track("github"),
        "mode": "tuf-trust-bootstrap",
        "schemaVersion": 1,
        "sigstore": request_track("sigstore"),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pretrust-v2-", dir=output.parent))
    try:
        package_path = staging / ".tuf-bootstrap-package.json"
        request_path = staging / ".tuf-bootstrap-request.json"
        github_root_path = staging / ".github-bootstrap-root.json"
        sigstore_root_path = staging / ".sigstore-bootstrap-root.json"
        package_path.write_bytes(_canonical_json_bytes(package))
        request_path.write_bytes(_canonical_json_bytes(request))
        github_root_path.write_bytes(tracks["github"][0])
        sigstore_root_path.write_bytes(tracks["sigstore"][0])
        claim_bytes = _run_verifier(
            runner,
            (
                str(verifier),
                "--trust-update",
                str(package_path),
                "--github-tuf-root",
                str(github_root_path),
                "--sigstore-tuf-root",
                str(sigstore_root_path),
                "--request",
                str(request_path),
            ),
        )
        try:
            claim_value = json.loads(claim_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrustBootstrapError("TUF bootstrap claim 不可解析") from error
        if claim_bytes != _canonical_json_bytes(claim_value) + b"\n":
            raise TrustBootstrapError("TUF bootstrap claim 不是 canonical JSON")
        claims = _close_claim(
            claim_value,
            bootstrap_identity=bootstrap_identity,
            tracks=tracks,
        )

        github_final_root = tracks["github"][1][-1] if tracks["github"][1] else tracks["github"][0]
        sigstore_final_root = tracks["sigstore"][1][-1] if tracks["sigstore"][1] else tracks["sigstore"][0]
        revocation_snapshot = _digest(
            _canonical_json_bytes(
                {
                    "revokedMaterialIdentities": [],
                    "schemaVersion": 1,
                    "sequence": 1,
                    "source": "OFFICIAL_TUF_BOOTSTRAP",
                }
            )
        )
        profile = TrustProfile(
            profile_version=1,
            parent_profile_identity=None,
            repository=_REPOSITORY,
            repository_id=REPOSITORY_ID,
            owner_id=OWNER_ID,
            github_release_certificate_identity=GITHUB_RELEASE_CERTIFICATE_IDENTITY,
            github_trusted_root_sha256=claims["github"]["trustedRootSha256"],  # type: ignore[arg-type]
            sigstore_trusted_root_sha256=claims["sigstore"]["trustedRootSha256"],  # type: ignore[arg-type]
            github_tuf_root_sha256=claims["github"]["tufRootSha256"],  # type: ignore[arg-type]
            github_tuf_root_version=claims["github"]["tufRootVersion"],  # type: ignore[arg-type]
            github_tuf_timestamp_version=claims["github"]["timestampVersion"],  # type: ignore[arg-type]
            github_tuf_snapshot_version=claims["github"]["snapshotVersion"],  # type: ignore[arg-type]
            github_tuf_targets_version=claims["github"]["targetsVersion"],  # type: ignore[arg-type]
            sigstore_tuf_root_sha256=claims["sigstore"]["tufRootSha256"],  # type: ignore[arg-type]
            sigstore_tuf_root_version=claims["sigstore"]["tufRootVersion"],  # type: ignore[arg-type]
            sigstore_tuf_timestamp_version=claims["sigstore"]["timestampVersion"],  # type: ignore[arg-type]
            sigstore_tuf_snapshot_version=claims["sigstore"]["snapshotVersion"],  # type: ignore[arg-type]
            sigstore_tuf_targets_version=claims["sigstore"]["targetsVersion"],  # type: ignore[arg-type]
            verifier_id="github-sigstore-offline",
            minimum_verifier_version=_MINIMUM_VERIFIER_VERSION,
            revocation_epoch=1,
            revocation_snapshot_sha256=revocation_snapshot,
            verifier_identity=_digest(verifier_bytes),
            policy_identity=OFFLINE_POLICY_IDENTITY,
            activation_sequence=1,
        )
        files = {
            "github-trusted-root.jsonl": tracks["github"][5],
            "github-tuf-root.json": github_final_root,
            "offline-release-verifier": verifier_bytes,
            "sigstore-trusted-root.jsonl": tracks["sigstore"][5],
            "sigstore-tuf-root.json": sigstore_final_root,
            "trust-profile.json": _canonical_json_bytes(profile.as_bootstrap_record()),
        }
        manifest = {
            "authorityRole": "PRODUCTION_PRETRUST_ONLY",
            "files": [
                {
                    "mode": "0755" if name == "offline-release-verifier" else "0644",
                    "name": name,
                    "sha256": _digest(value),
                    "size": len(value),
                }
                for name, value in sorted(files.items())
            ],
            "profileIdentity": profile.identity,
            "releaseAuthority": "GITHUB_IMMUTABLE_RELEASE",
            "schemaVersion": 1,
            "stage0Model": _AUTHORITY_MODEL,
        }
        files["initial-trust-bootstrap.json"] = _canonical_json_bytes(manifest)
        for path in staging.iterdir():
            if path.name.startswith("."):
                path.unlink()
        for name, value in files.items():
            target = staging / name
            target.write_bytes(value)
            target.chmod(0o755 if name == "offline-release-verifier" else 0o644)
        validate_initial_trust_kit(staging)
        os.rename(staging, output)
        return {
            "authorityModel": _AUTHORITY_MODEL,
            "files": len(files),
            "githubTufRootVersion": profile.github_tuf_root_version,
            "profileIdentity": profile.identity,
            "sigstoreTufRootVersion": profile.sigstore_tuf_root_version,
            "status": "PASS",
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
