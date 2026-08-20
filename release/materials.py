from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from durability.platform import PlatformQualificationError, parse_platform_qualification

MAX_MATERIAL_FILES = 512
MAX_MATERIAL_FILE_BYTES = 64 * 1024 * 1024
MAX_MATERIAL_TOTAL_BYTES = 256 * 1024 * 1024
INSTALLER_MATERIALS_NAME = "installer-materials.tar"
PLATFORM_QUALIFICATION_MATERIAL = "release/platform-qualification.json"
OFFLINE_RELEASE_VERIFIER_MATERIAL = (
    "release/release_attestation_verifier/offline-release-verifier"
)
INITIAL_TRUST_KIT_PREFIX = "release/release_attestation_verifier/pretrust-v2"
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

_FIXED_DEPLOYMENT_FILES = (
    "deploy/docker-compose.yml",
    "deploy/install-updater.sh",
    "deploy/updater/animemo-updater",
    "deploy/updater/animemo-updater.service",
    "deploy/updater/animemo-updater.sysusers.conf",
    "deploy/updater/animemo-updater.tmpfiles.conf",
)


class MaterialContractError(ValueError):
    pass


@dataclass(frozen=True)
class MaterialFileIdentity:
    path: str
    sha256: str
    size: int
    mode: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": format(self.mode, "04o"),
        }


@dataclass(frozen=True)
class MaterialArchiveIdentity:
    sha256: str
    size: int
    files: tuple[MaterialFileIdentity, ...]


@dataclass(frozen=True)
class VerifiedMaterialSet:
    root: Path
    archive_sha256: str
    files: tuple[MaterialFileIdentity, ...]

    def material(self, relative: str) -> Path:
        relative = _validate_relative_path(relative)
        identities = {item.path: item for item in self.files}
        if relative not in identities:
            raise MaterialContractError("Installer material is not declared")
        target = self.root.joinpath(*PurePosixPath(relative).parts)
        try:
            target_stat = target.lstat()
        except OSError as error:
            raise MaterialContractError(
                "Verified installer material is unavailable"
            ) from error
        if (
            target.is_symlink()
            or not stat.S_ISREG(target_stat.st_mode)
            or target_stat.st_nlink != 1
        ):
            raise MaterialContractError(
                "Verified installer material is not a regular file"
            )
        value = target.read_bytes()
        identity = identities[relative]
        if len(value) != identity.size or _sha256_bytes(value) != identity.sha256:
            raise MaterialContractError("Verified installer material has changed")
        return target


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _validate_dynamic_material(relative: str, value: bytes) -> None:
    if relative != PLATFORM_QUALIFICATION_MATERIAL:
        return
    try:
        parse_platform_qualification(value)
    except PlatformQualificationError as error:
        raise MaterialContractError(
            "Platform qualification material is invalid"
        ) from error


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MaterialContractError("Installer material path is invalid")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise MaterialContractError("Installer material path is invalid")
    canonical = parsed.as_posix()
    if canonical != value:
        raise MaterialContractError("Installer material path is not canonical")
    return canonical


def _source_bytes(root: Path, relative: str) -> bytes:
    relative = _validate_relative_path(relative)
    source = root.joinpath(*PurePosixPath(relative).parts)
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise MaterialContractError(
            f"Installer material source is unavailable: {relative}"
        ) from error
    if (
        source.is_symlink()
        or not stat.S_ISREG(source_stat.st_mode)
        or source_stat.st_nlink != 1
    ):
        raise MaterialContractError(
            f"Installer material source must be a single-link regular file: {relative}"
        )
    if source_stat.st_size > MAX_MATERIAL_FILE_BYTES:
        raise MaterialContractError(
            f"Installer material source is too large: {relative}"
        )
    try:
        value = source.read_bytes()
    except OSError as error:
        raise MaterialContractError(
            f"Installer material source is unreadable: {relative}"
        ) from error
    if len(value) != source_stat.st_size:
        raise MaterialContractError(
            f"Installer material source changed while reading: {relative}"
        )
    return value


def _direct_source_bytes(source: Path, relative: str) -> bytes:
    try:
        source_stat = source.lstat()
    except OSError as error:
        raise MaterialContractError(
            f"Installer material source is unavailable: {relative}"
        ) from error
    if (
        source.is_symlink()
        or not stat.S_ISREG(source_stat.st_mode)
        or source_stat.st_nlink != 1
    ):
        raise MaterialContractError(
            f"Installer material source must be a single-link regular file: {relative}"
        )
    if source_stat.st_size > MAX_MATERIAL_FILE_BYTES:
        raise MaterialContractError(
            f"Installer material source is too large: {relative}"
        )
    value = source.read_bytes()
    if len(value) != source_stat.st_size:
        raise MaterialContractError(
            f"Installer material source changed while reading: {relative}"
        )
    return value


def _profile_paths(
    root: Path,
    wheelhouse: Path,
    initial_trust_kit: Path,
) -> list[tuple[str, Path]]:
    result = [(relative, root) for relative in _FIXED_DEPLOYMENT_FILES]
    for package in ("durability", "release", "updater", "installer"):
        package_root = root / package
        if package == "installer" and not package_root.exists():
            continue
        if not package_root.is_dir() or package_root.is_symlink():
            raise MaterialContractError(
                f"Installer material package is unavailable: {package}"
            )
        for source in package_root.rglob("*"):
            relative_parts = source.relative_to(root).parts
            if "__pycache__" in relative_parts or "tests" in relative_parts:
                continue
            if source.is_file() or source.is_symlink():
                result.append((source.relative_to(root).as_posix(), root))

    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise MaterialContractError("Offline wheelhouse is unavailable")
    wheels = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
    if not wheels or any(item.suffix != ".whl" for item in wheels):
        raise MaterialContractError("Offline wheelhouse must contain only wheel files")
    result.extend((f"wheelhouse/{wheel.name}", wheel) for wheel in wheels)

    from release.trust_bootstrap import validate_initial_trust_kit

    try:
        validate_initial_trust_kit(initial_trust_kit)
    except ValueError as error:
        raise MaterialContractError("Initial pretrust kit is invalid") from error
    result.extend(
        (
            f"{INITIAL_TRUST_KIT_PREFIX}/{name}",
            initial_trust_kit / name,
        )
        for name in sorted(INITIAL_TRUST_KIT_FILES)
    )

    paths = [relative for relative, _ in result]
    if len(paths) > MAX_MATERIAL_FILES or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material profile is duplicate or too large"
        )
    return sorted(result, key=lambda item: item[0])


def _mode_for(relative: str) -> int:
    return (
        0o755
        if relative.endswith(".sh")
        or relative == "deploy/updater/animemo-updater"
        or relative == OFFLINE_RELEASE_VERIFIER_MATERIAL
        else 0o644
    )


def build_installer_materials(
    root: Path,
    *,
    wheelhouse: Path,
    output: Path,
    initial_trust_kit: Path | None = None,
) -> MaterialArchiveIdentity:
    root = root.resolve()
    wheelhouse = wheelhouse.resolve()
    output = output.resolve()
    if initial_trust_kit is None:
        raise MaterialContractError("Initial pretrust kit is required")
    initial_trust_kit = initial_trust_kit.resolve()
    files: list[MaterialFileIdentity] = []
    total_bytes = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
            for relative, source_root in _profile_paths(
                root,
                wheelhouse,
                initial_trust_kit,
            ):
                value = (
                    _direct_source_bytes(source_root, relative)
                    if source_root.is_file()
                    else _source_bytes(source_root, relative)
                )
                _validate_dynamic_material(relative, value)
                total_bytes += len(value)
                if total_bytes > MAX_MATERIAL_TOTAL_BYTES:
                    raise MaterialContractError(
                        "Installer material profile exceeds its byte ceiling"
                    )
                mode = _mode_for(relative)
                identity = MaterialFileIdentity(
                    path=relative,
                    sha256=_sha256_bytes(value),
                    size=len(value),
                    mode=mode,
                )
                files.append(identity)
                member = tarfile.TarInfo(relative)
                member.size = len(value)
                member.mode = mode
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                archive.addfile(member, io.BytesIO(value))
        archive_size = temporary.stat().st_size
        archive_sha256 = _sha256_file(temporary)
        os.replace(temporary, output)
        return MaterialArchiveIdentity(
            sha256=archive_sha256,
            size=archive_size,
            files=tuple(files),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def inspect_installer_materials(archive_path: Path) -> MaterialArchiveIdentity:
    try:
        archive_stat = archive_path.lstat()
    except OSError as error:
        raise MaterialContractError(
            "Installer material archive is unavailable"
        ) from error
    if (
        archive_path.is_symlink()
        or not stat.S_ISREG(archive_stat.st_mode)
        or archive_stat.st_nlink != 1
        or archive_stat.st_size <= 0
        or archive_stat.st_size > MAX_MATERIAL_TOTAL_BYTES + MAX_MATERIAL_FILES * 2048
    ):
        raise MaterialContractError(
            "Installer material archive is not a bounded regular file"
        )
    files: list[MaterialFileIdentity] = []
    total = 0
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MATERIAL_FILES:
                raise MaterialContractError(
                    "Installer material archive has an invalid file count"
                )
            for member in members:
                relative = _validate_relative_path(member.name)
                if (
                    not member.isfile()
                    or member.size < 0
                    or member.size > MAX_MATERIAL_FILE_BYTES
                    or stat.S_IMODE(member.mode) not in {0o644, 0o755}
                    or member.mtime != 0
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                ):
                    raise MaterialContractError(
                        "Installer material archive entry is invalid"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise MaterialContractError(
                        "Installer material archive entry is unreadable"
                    )
                value = source.read(MAX_MATERIAL_FILE_BYTES + 1)
                if len(value) != member.size:
                    raise MaterialContractError(
                        "Installer material archive entry size differs"
                    )
                _validate_dynamic_material(relative, value)
                total += len(value)
                if total > MAX_MATERIAL_TOTAL_BYTES:
                    raise MaterialContractError(
                        "Installer material archive exceeds its byte ceiling"
                    )
                files.append(
                    MaterialFileIdentity(
                        path=relative,
                        sha256=_sha256_bytes(value),
                        size=len(value),
                        mode=stat.S_IMODE(member.mode),
                    )
                )
    except (OSError, tarfile.TarError) as error:
        raise MaterialContractError(
            "Installer material archive is not an uncompressed tar"
        ) from error
    paths = [item.path for item in files]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material archive is duplicate or unordered"
        )
    return MaterialArchiveIdentity(
        sha256=_sha256_file(archive_path),
        size=archive_stat.st_size,
        files=tuple(files),
    )


def _parse_material_contract(
    payload: object,
) -> tuple[dict[str, object], tuple[MaterialFileIdentity, ...]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "profile",
        "platform",
        "archive",
        "materials",
    }:
        raise MaterialContractError("Installer material contract has an invalid shape")
    if (
        payload["schemaVersion"] != 2
        or payload["profile"] != "v1.1-standard"
        or payload["platform"] != "linux/amd64"
    ):
        raise MaterialContractError(
            "Installer material contract has an unsupported profile"
        )
    archive = payload["archive"]
    if (
        not isinstance(archive, dict)
        or set(archive) != {"name", "sha256", "size", "format"}
        or archive["name"] != INSTALLER_MATERIALS_NAME
        or archive["format"] != "tar"
        or not isinstance(archive["sha256"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", archive["sha256"])
        or not isinstance(archive["size"], int)
        or isinstance(archive["size"], bool)
        or archive["size"] <= 0
        or archive["size"] > MAX_MATERIAL_TOTAL_BYTES + MAX_MATERIAL_FILES * 2048
    ):
        raise MaterialContractError("Installer material archive identity is invalid")
    raw_materials = payload["materials"]
    if (
        not isinstance(raw_materials, list)
        or not raw_materials
        or len(raw_materials) > MAX_MATERIAL_FILES
    ):
        raise MaterialContractError("Installer material file list is invalid")
    materials: list[MaterialFileIdentity] = []
    total = 0
    for item in raw_materials:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size", "mode"}
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["sha256"])
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or item["size"] > MAX_MATERIAL_FILE_BYTES
            or item.get("mode") not in {"0644", "0755"}
        ):
            raise MaterialContractError("Installer material file identity is invalid")
        relative = _validate_relative_path(item.get("path"))
        total += item["size"]
        if total > MAX_MATERIAL_TOTAL_BYTES:
            raise MaterialContractError(
                "Installer material file list exceeds its byte ceiling"
            )
        materials.append(
            MaterialFileIdentity(
                path=relative,
                sha256=item["sha256"],
                size=item["size"],
                mode=int(item["mode"], 8),
            )
        )
    paths = [item.path for item in materials]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise MaterialContractError(
            "Installer material file list is duplicate or unordered"
        )
    required_pretrust = {
        f"{INITIAL_TRUST_KIT_PREFIX}/{name}" for name in INITIAL_TRUST_KIT_FILES
    }
    if not required_pretrust.issubset(paths):
        raise MaterialContractError("Installer material profile lacks initial pretrust")
    return archive, tuple(materials)


def validate_material_contract(
    payload: object,
) -> tuple[dict[str, object], tuple[MaterialFileIdentity, ...]]:
    """Validate the exact material profile through its public contract seam."""
    return _parse_material_contract(payload)


def extract_installer_materials(
    archive_path: Path,
    contract: object,
    destination: Path,
) -> VerifiedMaterialSet:
    archive_identity, materials = _parse_material_contract(contract)
    try:
        archive_stat = archive_path.lstat()
    except OSError as error:
        raise MaterialContractError(
            "Installer material archive is unavailable"
        ) from error
    if (
        archive_path.is_symlink()
        or not stat.S_ISREG(archive_stat.st_mode)
        or archive_stat.st_nlink != 1
        or archive_stat.st_size != archive_identity["size"]
    ):
        raise MaterialContractError("Installer material archive identity differs")
    if _sha256_file(archive_path) != archive_identity["sha256"]:
        raise MaterialContractError("Installer material archive checksum differs")
    if destination.exists() or destination.is_symlink():
        raise MaterialContractError("Installer material destination must not exist")
    destination.mkdir(parents=True, mode=0o700)
    try:
        try:
            with tarfile.open(archive_path, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != len(materials):
                    raise MaterialContractError(
                        "Installer material archive file set differs"
                    )
                for member, identity in zip(members, materials, strict=True):
                    if (
                        member.name != identity.path
                        or not member.isfile()
                        or member.size != identity.size
                        or stat.S_IMODE(member.mode) != identity.mode
                        or member.mtime != 0
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != ""
                        or member.gname != ""
                    ):
                        raise MaterialContractError(
                            "Installer material archive entry differs"
                        )
                    target = destination.joinpath(*PurePosixPath(identity.path).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise MaterialContractError(
                            "Installer material archive entry is unreadable"
                        )
                    digest = hashlib.sha256()
                    written = 0
                    with target.open("xb") as handle:
                        while chunk := source.read(1024 * 1024):
                            written += len(chunk)
                            if written > identity.size:
                                raise MaterialContractError(
                                    "Installer material archive entry exceeds its size"
                                )
                            digest.update(chunk)
                            handle.write(chunk)
                    if (
                        written != identity.size
                        or "sha256:" + digest.hexdigest() != identity.sha256
                    ):
                        raise MaterialContractError(
                            "Installer material archive entry checksum differs"
                        )
                    target.chmod(identity.mode)
        except (OSError, tarfile.TarError) as error:
            raise MaterialContractError(
                "Installer material archive is not an uncompressed tar"
            ) from error
        return VerifiedMaterialSet(
            root=destination,
            archive_sha256=archive_identity["sha256"],
            files=materials,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
