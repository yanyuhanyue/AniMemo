from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from installer.bootstrap import AuthorizedBootstrap

from .offline import (
    PretrustedTrustMaterial,
    SigstoreGoEvidenceVerifier,
    TrustProfile,
    _canonical_json_bytes,
)
from .state import UpdateLock

TRUST_STATE_ROOT = Path("/var/lib/animemo/offline-trust/v2")
_PREFIX = PurePosixPath("release/release_attestation_verifier/pretrust-v2")
_RUNTIME_FILES = frozenset(
    {
        "github-trusted-root.jsonl",
        "github-tuf-root.json",
        "offline-release-verifier",
        "sigstore-trusted-root.jsonl",
        "sigstore-tuf-root.json",
        "trust-profile.json",
    }
)
_ARCHIVE_FILES = _RUNTIME_FILES | {"initial-trust-bootstrap.json"}


class TrustLifecycleError(RuntimeError):
    pass


def _reject(code: str) -> None:
    raise TrustLifecycleError(code)


class TrustUpdateVerifier(Protocol):
    def verify_tuf_update_package(
        self,
        *,
        package: bytes,
        current_profile: TrustProfile,
    ) -> Mapping[str, object]: ...


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _load_canonical(data: bytes, *, code: str) -> object:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject(code)
    if _canonical_json_bytes(value) != data:
        _reject(code)
    return value


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_state_root(path: Path) -> Path:
    if not path.is_absolute():
        _reject("TRUST_STATE_ROOT_INVALID")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        _reject("TRUST_STATE_ROOT_INVALID")
    try:
        metadata = path.lstat()
    except OSError:
        _reject("TRUST_STATE_ROOT_INVALID")
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        _reject("TRUST_STATE_ROOT_INVALID")
    if os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
        _reject("TRUST_STATE_ROOT_UNSAFE")
    try:
        os.chmod(path, 0o700)
    except OSError:
        _reject("TRUST_STATE_ROOT_INVALID")
    return path


def _extract_initial_kit(archive_bytes: bytes) -> dict[str, bytes]:
    observed: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as bundle:
            for member in bundle:
                pure = PurePosixPath(member.name)
                if pure.parent != _PREFIX:
                    continue
                name = pure.name
                if (
                    name not in _ARCHIVE_FILES
                    or name in observed
                    or not member.isfile()
                    or member.size < 1
                    or member.size > 256 * 1024 * 1024
                ):
                    _reject("TRUST_INITIAL_KIT_ARCHIVE_INVALID")
                stream = bundle.extractfile(member)
                if stream is None:
                    _reject("TRUST_INITIAL_KIT_ARCHIVE_INVALID")
                data = stream.read(member.size + 1)
                if len(data) != member.size:
                    _reject("TRUST_INITIAL_KIT_ARCHIVE_INVALID")
                observed[name] = data
    except TrustLifecycleError:
        raise
    except (OSError, tarfile.TarError):
        _reject("TRUST_INITIAL_KIT_ARCHIVE_INVALID")
    if frozenset(observed) != _ARCHIVE_FILES:
        _reject("TRUST_INITIAL_KIT_INCOMPLETE")
    return observed


def _close_initial_manifest(value: object, files: Mapping[str, bytes]) -> str:
    if type(value) is not dict or set(value) != {
        "authorityRole",
        "files",
        "profileIdentity",
        "releaseAuthority",
        "schemaVersion",
        "stage0Model",
    }:
        _reject("TRUST_INITIAL_MANIFEST_INVALID")
    if (
        value["schemaVersion"] != 1
        or value["authorityRole"] != "PRODUCTION_PRETRUST_ONLY"
        or value["releaseAuthority"] != "GITHUB_IMMUTABLE_RELEASE"
        or value["stage0Model"]
        != "GITHUB_IMMUTABLE_RELEASE_SIGSTORE_TUF_SINGLE_AUTHORITY"
        or type(value["profileIdentity"]) is not str
        or type(value["files"]) is not list
    ):
        _reject("TRUST_INITIAL_MANIFEST_INVALID")
    expected = []
    for name, data in sorted(files.items()):
        if name == "initial-trust-bootstrap.json":
            continue
        expected.append(
            {
                "mode": "0755" if name == "offline-release-verifier" else "0644",
                "name": name,
                "sha256": _digest(data),
                "size": len(data),
            }
        )
    if value["files"] != expected:
        _reject("TRUST_INITIAL_MANIFEST_FILE_BINDING_INVALID")
    return value["profileIdentity"]


def _read_update_package(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= 64 * 1024 * 1024
        ):
            _reject("TRUST_UPDATE_PACKAGE_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except TrustLifecycleError:
        raise
    except OSError:
        _reject("TRUST_UPDATE_PACKAGE_INVALID")
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            _reject("TRUST_UPDATE_PACKAGE_RACE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 64 * 1024 * 1024:
                _reject("TRUST_UPDATE_PACKAGE_INVALID")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            total != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            _reject("TRUST_UPDATE_PACKAGE_RACE")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _close_update_package(
    raw: bytes,
    current: TrustProfile,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    value = _load_canonical(raw, code="TRUST_UPDATE_PACKAGE_INVALID")
    if type(value) is not dict or set(value) != {
        "authorityRole",
        "fromProfileIdentity",
        "github",
        "schemaVersion",
        "sigstore",
    }:
        _reject("TRUST_UPDATE_PACKAGE_INVALID")
    if (
        value["schemaVersion"] != 1
        or value["authorityRole"] != "TRUST_METADATA_ONLY"
        or value["fromProfileIdentity"] != current.identity
    ):
        _reject("TRUST_UPDATE_PACKAGE_BINDING_INVALID")
    decoded: dict[str, dict[str, object]] = {}
    for role in ("github", "sigstore"):
        track = value[role]
        if type(track) is not dict or set(track) != {
            "rootChain",
            "snapshot",
            "targets",
            "timestamp",
            "trustedRoot",
        }:
            _reject("TRUST_UPDATE_PACKAGE_INVALID")
        if (
            type(track["rootChain"]) is not list
            or not 0 <= len(track["rootChain"]) <= 32
        ):
            _reject("TRUST_UPDATE_PACKAGE_INVALID")
        closed: dict[str, object] = {}
        for field in ("timestamp", "snapshot", "targets", "trustedRoot"):
            try:
                data = base64.b64decode(track[field], validate=True)
            except (TypeError, ValueError):
                _reject("TRUST_UPDATE_PACKAGE_INVALID")
            if not 1 <= len(data) <= 16 * 1024 * 1024:
                _reject("TRUST_UPDATE_PACKAGE_INVALID")
            closed[field] = data
        roots: list[bytes] = []
        for encoded in track["rootChain"]:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (TypeError, ValueError):
                _reject("TRUST_UPDATE_PACKAGE_INVALID")
            if not 1 <= len(data) <= 16 * 1024 * 1024:
                _reject("TRUST_UPDATE_PACKAGE_INVALID")
            roots.append(data)
        closed["rootChain"] = roots
        decoded[role] = closed
    return decoded, value


def _close_update_claim(
    claim: object,
    *,
    current: TrustProfile,
    decoded: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, Mapping[str, object]], list[str], list[str]]:
    if type(claim) is not dict or set(claim) != {
        "authorityRole",
        "fromProfileIdentity",
        "github",
        "schemaVersion",
        "sigstore",
    }:
        _reject("TRUST_UPDATE_CLAIM_INVALID")
    if (
        claim["schemaVersion"] != 1
        or claim["authorityRole"] != "TRUST_METADATA_ONLY"
        or claim["fromProfileIdentity"] != current.identity
    ):
        _reject("TRUST_UPDATE_CLAIM_INVALID")
    roles: dict[str, Mapping[str, object]] = {}
    superseded: list[str] = []
    revoked_signers: list[str] = []
    for role in ("github", "sigstore"):
        item = claim[role]
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
            _reject("TRUST_UPDATE_CLAIM_INVALID")
        roots = decoded[role]["rootChain"]
        assert isinstance(roots, list)
        current_root = (
            current.github_tuf_root_sha256
            if role == "github"
            else current.sigstore_tuf_root_sha256
        )
        final_root_identity = _digest(roots[-1]) if roots else current_root
        current_versions = (
            (
                current.github_tuf_root_version,
                current.github_tuf_timestamp_version,
                current.github_tuf_snapshot_version,
                current.github_tuf_targets_version,
            )
            if role == "github"
            else (
                current.sigstore_tuf_root_version,
                current.sigstore_tuf_timestamp_version,
                current.sigstore_tuf_snapshot_version,
                current.sigstore_tuf_targets_version,
            )
        )
        current_trusted_root = (
            current.github_trusted_root_sha256
            if role == "github"
            else current.sigstore_trusted_root_sha256
        )
        superseded_values = item["supersededMaterialIdentities"]
        revoked_values = item["revokedSignerKeyIds"]
        if (
            item["tufRootSha256"] != final_root_identity
            or item["trustedRootSha256"]
            != _digest(decoded[role]["trustedRoot"])  # type: ignore[arg-type]
            or any(
                type(item[field]) is not int
                for field in (
                    "tufRootVersion",
                    "timestampVersion",
                    "snapshotVersion",
                    "targetsVersion",
                )
            )
            or item["tufRootVersion"] < current_versions[0]
            or item["timestampVersion"] <= current_versions[1]
            or item["snapshotVersion"] <= current_versions[2]
            or item["targetsVersion"] <= current_versions[3]
            or type(superseded_values) is not list
            or superseded_values != sorted(set(superseded_values))
            or any(
                type(value) is not str
                or not value.startswith("sha256:")
                or len(value) != 71
                for value in superseded_values
            )
            or type(revoked_values) is not list
            or revoked_values != sorted(set(revoked_values))
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in revoked_values
            )
        ):
            _reject("TRUST_UPDATE_CLAIM_INVALID")
        required_superseded = set()
        if item["tufRootSha256"] != current_root:
            required_superseded.add(current_root)
        if item["trustedRootSha256"] != current_trusted_root:
            required_superseded.add(current_trusted_root)
        if not required_superseded.issubset(superseded_values):
            _reject("TRUST_UPDATE_SUPERSESSION_INCOMPLETE")
        roles[role] = item
        superseded.extend(superseded_values)
        revoked_signers.extend(revoked_values)
    return roles, sorted(set(superseded)), sorted(set(revoked_signers))


def _tuf_root_signer_key_ids_bytes(raw: bytes) -> frozenset[str]:
    try:
        value = json.loads(raw)
        roles = value["signed"]["roles"]
    except (ValueError, KeyError, TypeError):
        _reject("TRUST_ACTIVE_TUF_ROOT_INVALID")
    if type(roles) is not dict:
        _reject("TRUST_ACTIVE_TUF_ROOT_INVALID")
    observed: set[str] = set()
    for role in roles.values():
        if type(role) is not dict or type(role.get("keyids")) is not list:
            _reject("TRUST_ACTIVE_TUF_ROOT_INVALID")
        for key_id in role["keyids"]:
            if (
                type(key_id) is not str
                or len(key_id) != 64
                or any(character not in "0123456789abcdef" for character in key_id)
            ):
                _reject("TRUST_ACTIVE_TUF_ROOT_INVALID")
            observed.add(key_id)
    return frozenset(observed)


def _tuf_root_signer_key_ids(path: Path) -> frozenset[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        _reject("TRUST_ACTIVE_TUF_ROOT_INVALID")
    return _tuf_root_signer_key_ids_bytes(raw)


@dataclass(frozen=True)
class TrustCommitReceipt:
    commit_identity: str
    profile_identity: str
    generation: int
    authorization_identity: str


@dataclass(frozen=True)
class ActiveTrustSnapshot:
    generation: int
    generation_root: Path
    material: PretrustedTrustMaterial
    superseded_material_identities: frozenset[str]
    revoked_signer_key_ids: frozenset[str]

    @property
    def profile(self) -> TrustProfile:
        return self.material.profile


class ProductionTrustLifecycle:
    def __init__(
        self,
        *,
        _root: Path | None = None,
        _test: bool = False,
        _verifier_factory: Callable[[PretrustedTrustMaterial], TrustUpdateVerifier]
        | None = None,
    ) -> None:
        if _test is False and _root is not None:
            _reject("TRUST_STATE_ROOT_OVERRIDE_FORBIDDEN")
        self._root = TRUST_STATE_ROOT if _root is None else Path(_root)
        self._verifier_factory = _verifier_factory or SigstoreGoEvidenceVerifier

    @classmethod
    def _for_test(
        cls,
        root: Path,
        *,
        verifier_factory: Callable[[PretrustedTrustMaterial], TrustUpdateVerifier]
        | None = None,
    ) -> ProductionTrustLifecycle:
        return cls(
            _root=Path(root),
            _test=True,
            _verifier_factory=verifier_factory,
        )

    @classmethod
    def production(cls) -> ProductionTrustLifecycle:
        return cls()

    def provision_initial(self, authorization: object) -> TrustCommitReceipt:
        if type(authorization) is not AuthorizedBootstrap:
            _reject("TRUST_INITIAL_BOOTSTRAP_AUTHORIZATION_INVALID")
        assert isinstance(authorization, AuthorizedBootstrap)
        archive = authorization.materials_path
        try:
            archive_bytes = archive.read_bytes()
        except OSError:
            _reject("TRUST_INITIAL_KIT_ARCHIVE_INVALID")
        if _digest(archive_bytes) != authorization.materials_sha256:
            _reject("TRUST_INITIAL_BOOTSTRAP_BINDING_INVALID")
        files = _extract_initial_kit(archive_bytes)
        manifest = _load_canonical(
            files["initial-trust-bootstrap.json"],
            code="TRUST_INITIAL_MANIFEST_INVALID",
        )
        profile_identity = _close_initial_manifest(manifest, files)
        profile_record = _load_canonical(
            files["trust-profile.json"],
            code="TRUST_INITIAL_PROFILE_INVALID",
        )
        if not isinstance(profile_record, dict):
            _reject("TRUST_INITIAL_PROFILE_INVALID")
        try:
            profile = TrustProfile.from_bootstrap_record(profile_record)
        except Exception:  # noqa: BLE001 - normalize untrusted profile failures
            _reject("TRUST_INITIAL_PROFILE_INVALID")
        if (
            profile.profile_version != 1
            or profile.parent_profile_identity is not None
            or profile.identity != profile_identity
        ):
            _reject("TRUST_INITIAL_PROFILE_INVALID")

        root = _safe_state_root(self._root)
        with UpdateLock(root / "trust-lifecycle.lock"):
            active_path = root / "active-state.json"
            if active_path.exists():
                active = self.load_active(_lock=False)
                if active.profile.identity != profile.identity:
                    _reject("TRUST_INITIAL_PROVISIONING_CONFLICT")
                return self._receipt_from_active(active, authorization)

            generations = root / "generations"
            generations.mkdir(mode=0o700, exist_ok=True)
            generation_name = profile.identity.removeprefix("sha256:")
            final = generations / generation_name
            staging = root / f".generation-{secrets.token_hex(12)}"
            try:
                staging.mkdir(mode=0o700)
                for name in sorted(_RUNTIME_FILES):
                    target = staging / name
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    descriptor = os.open(
                        target,
                        flags,
                        0o755 if name == "offline-release-verifier" else 0o644,
                    )
                    with os.fdopen(descriptor, "wb", closefd=True) as stream:
                        stream.write(files[name])
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(
                        target,
                        0o755 if name == "offline-release-verifier" else 0o644,
                    )
                _fsync_directory(staging)
                material = PretrustedTrustMaterial.load(staging)
                if material.profile.identity != profile.identity:
                    _reject("TRUST_INITIAL_GENERATION_INVALID")
                try:
                    os.rename(staging, final)
                except FileExistsError:
                    material = PretrustedTrustMaterial.load(final)
                    if material.profile.identity != profile.identity:
                        _reject("TRUST_INITIAL_PROVISIONING_CONFLICT")
                _fsync_directory(generations)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

            record = {
                "activationSequence": profile.activation_sequence,
                "authorizationIdentity": authorization.authorization_identity,
                "generation": 1,
                "generationName": generation_name,
                "lastUpdateIdentity": None,
                "profileIdentity": profile.identity,
                "revokedSignerKeyIds": [],
                "schemaVersion": 3,
                "supersededMaterialIdentities": [],
            }
            record["commitIdentity"] = _digest(_canonical_json_bytes(record))
            self._commit_active(record)
            return TrustCommitReceipt(
                commit_identity=record["commitIdentity"],
                profile_identity=profile.identity,
                generation=1,
                authorization_identity=authorization.authorization_identity,
            )

    def load_active(self, *, _lock: bool = True) -> ActiveTrustSnapshot:
        root = _safe_state_root(self._root)
        if _lock:
            with UpdateLock(root / "trust-lifecycle.lock"):
                return self.load_active(_lock=False)
        path = root / "active-state.json"
        try:
            raw = path.read_bytes()
        except OSError:
            _reject("TRUST_ACTIVE_STATE_UNAVAILABLE")
        value = _load_canonical(raw, code="TRUST_ACTIVE_STATE_INVALID")
        if type(value) is not dict or set(value) != {
            "activationSequence",
            "authorizationIdentity",
            "commitIdentity",
            "generation",
            "generationName",
            "lastUpdateIdentity",
            "profileIdentity",
            "revokedSignerKeyIds",
            "schemaVersion",
            "supersededMaterialIdentities",
        }:
            _reject("TRUST_ACTIVE_STATE_INVALID")
        unsigned = dict(value)
        commit_identity = unsigned.pop("commitIdentity")
        if (
            value["schemaVersion"] != 3
            or type(value["generation"]) is not int
            or value["generation"] < 1
            or type(value["generationName"]) is not str
            or value["generationName"] != str(value["profileIdentity"]).removeprefix("sha256:")
            or (
                value["lastUpdateIdentity"] is not None
                and (
                    type(value["lastUpdateIdentity"]) is not str
                    or not value["lastUpdateIdentity"].startswith("sha256:")
                )
            )
            or type(value["supersededMaterialIdentities"]) is not list
            or value["supersededMaterialIdentities"]
            != sorted(set(value["supersededMaterialIdentities"]))
            or any(
                type(identity) is not str
                or not identity.startswith("sha256:")
                or len(identity) != 71
                for identity in value["supersededMaterialIdentities"]
            )
            or type(value["revokedSignerKeyIds"]) is not list
            or value["revokedSignerKeyIds"]
            != sorted(set(value["revokedSignerKeyIds"]))
            or any(
                type(key_id) is not str
                or len(key_id) != 64
                or any(character not in "0123456789abcdef" for character in key_id)
                for key_id in value["revokedSignerKeyIds"]
            )
            or commit_identity != _digest(_canonical_json_bytes(unsigned))
        ):
            _reject("TRUST_ACTIVE_STATE_INVALID")
        generation_root = root / "generations" / value["generationName"]
        try:
            material = PretrustedTrustMaterial.load(generation_root)
        except Exception:  # noqa: BLE001 - active-state boundary stays redacted
            _reject("TRUST_ACTIVE_GENERATION_INVALID")
        if (
            material.profile.identity != value["profileIdentity"]
            or material.profile.activation_sequence != value["activationSequence"]
            or any(
                identity
                in value["supersededMaterialIdentities"]
                for identity in (
                    material.profile.github_trusted_root_sha256,
                    material.profile.github_tuf_root_sha256,
                    material.profile.sigstore_trusted_root_sha256,
                    material.profile.sigstore_tuf_root_sha256,
                    material.profile.verifier_identity,
                )
            )
        ):
            _reject("TRUST_ACTIVE_STATE_INVALID")
        if value["revokedSignerKeyIds"]:
            active_signers = _tuf_root_signer_key_ids(
                material.github_tuf_root_path
            ) | _tuf_root_signer_key_ids(material.sigstore_tuf_root_path)
            if active_signers & set(value["revokedSignerKeyIds"]):
                _reject("TRUST_ACTIVE_REVOKED_SIGNER")
        return ActiveTrustSnapshot(
            generation=value["generation"],
            generation_root=generation_root,
            material=material,
            superseded_material_identities=frozenset(
                value["supersededMaterialIdentities"]
            ),
            revoked_signer_key_ids=frozenset(value["revokedSignerKeyIds"]),
        )

    def load_profile_lineage(
        self,
        active: ActiveTrustSnapshot,
    ) -> frozenset[str]:
        """从不可变 generations 重建并验证到初始 profile 的连续父链。"""

        if type(active) is not ActiveTrustSnapshot:
            _reject("TRUST_PROFILE_LINEAGE_INVALID")
        generations = active.generation_root.parent
        observed: set[str] = set()
        current = active.profile
        while True:
            if current.identity in observed:
                _reject("TRUST_PROFILE_LINEAGE_INVALID")
            observed.add(current.identity)
            parent_identity = current.parent_profile_identity
            if parent_identity is None:
                if current.profile_version != 1:
                    _reject("TRUST_PROFILE_LINEAGE_INVALID")
                break
            parent_root = generations / parent_identity.removeprefix("sha256:")
            try:
                parent = PretrustedTrustMaterial.load(parent_root).profile
            except Exception:  # noqa: BLE001 - lineage boundary stays redacted
                _reject("TRUST_PROFILE_LINEAGE_INVALID")
            if (
                parent.identity != parent_identity
                or parent.profile_version != current.profile_version - 1
                or parent.activation_sequence != current.activation_sequence - 1  # type: ignore[operator]
                or parent.repository != current.repository
                or parent.repository_id != current.repository_id
                or parent.owner_id != current.owner_id
                or parent.github_release_certificate_identity
                != current.github_release_certificate_identity
                or parent.verifier_id != current.verifier_id
                or parent.policy_identity != current.policy_identity
            ):
                _reject("TRUST_PROFILE_LINEAGE_INVALID")
            current = parent
        return frozenset(observed)

    def import_update(self, package: Path) -> TrustCommitReceipt:
        raw = _read_update_package(Path(package))
        package_identity = _digest(raw)
        root = _safe_state_root(self._root)
        with UpdateLock(root / "trust-lifecycle.lock"):
            active = self.load_active(_lock=False)
            active_record = _load_canonical(
                (root / "active-state.json").read_bytes(),
                code="TRUST_ACTIVE_STATE_INVALID",
            )
            assert isinstance(active_record, dict)
            if active_record["lastUpdateIdentity"] == package_identity:
                return TrustCommitReceipt(
                    commit_identity=active_record["commitIdentity"],
                    profile_identity=active_record["profileIdentity"],
                    generation=active_record["generation"],
                    authorization_identity=active_record["authorizationIdentity"],
                )

            decoded, _ = _close_update_package(raw, active.profile)
            verifier = self._verifier_factory(active.material)
            try:
                claim = verifier.verify_tuf_update_package(
                    package=raw,
                    current_profile=active.profile,
                )
            except Exception:  # noqa: BLE001 - external verifier stays fail-closed
                _reject("TRUST_UPDATE_CRYPTOGRAPHIC_VERIFICATION_FAILED")
            roles, superseded, revoked_signers = _close_update_claim(
                claim,
                current=active.profile,
                decoded=decoded,
            )
            github = roles["github"]
            sigstore = roles["sigstore"]
            next_revocation_epoch = active.profile.revocation_epoch
            revocation_snapshot = active.profile.revocation_snapshot_sha256
            if revoked_signers:
                next_revocation_epoch += 1
                revocation_snapshot = _digest(
                    _canonical_json_bytes(
                        {
                            "parentSnapshotIdentity": revocation_snapshot,
                            "revokedSignerKeyIds": revoked_signers,
                            "schemaVersion": 2,
                            "sequence": next_revocation_epoch,
                            "tufClaims": {
                                "github": dict(github),
                                "sigstore": dict(sigstore),
                            },
                        }
                    )
                )
            successor = TrustProfile(
                profile_version=active.profile.profile_version + 1,
                parent_profile_identity=active.profile.identity,
                repository=active.profile.repository,
                repository_id=active.profile.repository_id,
                owner_id=active.profile.owner_id,
                github_release_certificate_identity=(
                    active.profile.github_release_certificate_identity
                ),
                github_trusted_root_sha256=github["trustedRootSha256"],  # type: ignore[arg-type]
                sigstore_trusted_root_sha256=sigstore["trustedRootSha256"],  # type: ignore[arg-type]
                github_tuf_root_sha256=github["tufRootSha256"],  # type: ignore[arg-type]
                github_tuf_root_version=github["tufRootVersion"],  # type: ignore[arg-type]
                github_tuf_timestamp_version=github["timestampVersion"],  # type: ignore[arg-type]
                github_tuf_snapshot_version=github["snapshotVersion"],  # type: ignore[arg-type]
                github_tuf_targets_version=github["targetsVersion"],  # type: ignore[arg-type]
                sigstore_tuf_root_sha256=sigstore["tufRootSha256"],  # type: ignore[arg-type]
                sigstore_tuf_root_version=sigstore["tufRootVersion"],  # type: ignore[arg-type]
                sigstore_tuf_timestamp_version=sigstore["timestampVersion"],  # type: ignore[arg-type]
                sigstore_tuf_snapshot_version=sigstore["snapshotVersion"],  # type: ignore[arg-type]
                sigstore_tuf_targets_version=sigstore["targetsVersion"],  # type: ignore[arg-type]
                verifier_id=active.profile.verifier_id,
                minimum_verifier_version=active.profile.minimum_verifier_version,
                revocation_epoch=next_revocation_epoch,
                revocation_snapshot_sha256=revocation_snapshot,
                verifier_identity=active.profile.verifier_identity,
                policy_identity=active.profile.policy_identity,
                activation_sequence=active.profile.activation_sequence + 1,  # type: ignore[operator]
            )
            github_roots = decoded["github"]["rootChain"]
            sigstore_roots = decoded["sigstore"]["rootChain"]
            assert isinstance(github_roots, list) and isinstance(sigstore_roots, list)
            files = {
                "github-trusted-root.jsonl": decoded["github"]["trustedRoot"],
                "sigstore-trusted-root.jsonl": decoded["sigstore"]["trustedRoot"],
                "github-tuf-root.json": (
                    github_roots[-1]
                    if github_roots
                    else active.material.github_tuf_root_path.read_bytes()
                ),
                "sigstore-tuf-root.json": (
                    sigstore_roots[-1]
                    if sigstore_roots
                    else active.material.sigstore_tuf_root_path.read_bytes()
                ),
                "offline-release-verifier": active.material.verifier_path.read_bytes(),
                "trust-profile.json": _canonical_json_bytes(
                    successor.as_bootstrap_record()
                ),
            }
            existing_superseded = active_record["supersededMaterialIdentities"]
            all_superseded = sorted(set(existing_superseded) | set(superseded))
            existing_revoked = active_record["revokedSignerKeyIds"]
            all_revoked = sorted(set(existing_revoked) | set(revoked_signers))
            successor_signers = _tuf_root_signer_key_ids_bytes(
                files["github-tuf-root.json"]
            ) | _tuf_root_signer_key_ids_bytes(files["sigstore-tuf-root.json"])
            if successor_signers & set(all_revoked):
                _reject("TRUST_UPDATE_REVOKED_SIGNER_REINTRODUCED")
            successor_material_identities = {
                successor.github_trusted_root_sha256,
                successor.github_tuf_root_sha256,
                successor.sigstore_trusted_root_sha256,
                successor.sigstore_tuf_root_sha256,
                successor.verifier_identity,
            }
            if successor_material_identities & set(all_superseded):
                _reject("TRUST_UPDATE_SUPERSEDED_MATERIAL_REINTRODUCED")
            generation_root = self._stage_successor(successor, files)
            record = {
                "activationSequence": successor.activation_sequence,
                "authorizationIdentity": active_record["authorizationIdentity"],
                "generation": active.generation + 1,
                "generationName": generation_root.name,
                "lastUpdateIdentity": package_identity,
                "profileIdentity": successor.identity,
                "revokedSignerKeyIds": all_revoked,
                "schemaVersion": 3,
                "supersededMaterialIdentities": all_superseded,
            }
            record["commitIdentity"] = _digest(_canonical_json_bytes(record))
            self._commit_active(record)
            return TrustCommitReceipt(
                commit_identity=record["commitIdentity"],
                profile_identity=successor.identity,
                generation=record["generation"],
                authorization_identity=record["authorizationIdentity"],
            )

    def _stage_successor(
        self,
        profile: TrustProfile,
        files: Mapping[str, object],
    ) -> Path:
        if frozenset(files) != _RUNTIME_FILES or any(
            type(value) is not bytes for value in files.values()
        ):
            _reject("TRUST_UPDATE_GENERATION_INVALID")
        root = _safe_state_root(self._root)
        generations = root / "generations"
        generation_name = profile.identity.removeprefix("sha256:")
        final = generations / generation_name
        if final.exists():
            try:
                material = PretrustedTrustMaterial.load(final)
            except Exception:  # noqa: BLE001 - immutable generation stays closed
                _reject("TRUST_UPDATE_GENERATION_CONFLICT")
            if material.profile.identity != profile.identity:
                _reject("TRUST_UPDATE_GENERATION_CONFLICT")
            return final
        staging = root / f".generation-{secrets.token_hex(12)}"
        try:
            staging.mkdir(mode=0o700)
            for name in sorted(_RUNTIME_FILES):
                data = files[name]
                assert isinstance(data, bytes)
                target = staging / name
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o755 if name == "offline-release-verifier" else 0o644,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(
                    target,
                    0o755 if name == "offline-release-verifier" else 0o644,
                )
            _fsync_directory(staging)
            try:
                material = PretrustedTrustMaterial.load(staging)
            except Exception:  # noqa: BLE001 - staged generation stays closed
                _reject("TRUST_UPDATE_GENERATION_INVALID")
            if material.profile.identity != profile.identity:
                _reject("TRUST_UPDATE_GENERATION_INVALID")
            os.rename(staging, final)
            _fsync_directory(generations)
            return final
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _commit_active(self, record: Mapping[str, object]) -> None:
        root = _safe_state_root(self._root)
        target = root / "active-state.json"
        temporary = root / f".active-{secrets.token_hex(12)}"
        data = _canonical_json_bytes(dict(record))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _receipt_from_active(
        active: ActiveTrustSnapshot,
        authorization: AuthorizedBootstrap,
    ) -> TrustCommitReceipt:
        raw = (active.generation_root.parent.parent / "active-state.json").read_bytes()
        value = _load_canonical(raw, code="TRUST_ACTIVE_STATE_INVALID")
        assert isinstance(value, dict)
        if value["authorizationIdentity"] != authorization.authorization_identity:
            _reject("TRUST_INITIAL_PROVISIONING_CONFLICT")
        return TrustCommitReceipt(
            commit_identity=value["commitIdentity"],
            profile_identity=value["profileIdentity"],
            generation=value["generation"],
            authorization_identity=value["authorizationIdentity"],
        )
