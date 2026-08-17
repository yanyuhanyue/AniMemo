import hashlib
import json
import os
import stat
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


OFFICIAL_PLUGIN_SLUGS = ("watch-history-importer",)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CONTENT_IDENTITY_VERSION = 1


def _is_link(path):
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _validate_source_tree(source_root):
    source_root = Path(source_root)
    try:
        root_metadata = source_root.lstat()
    except OSError as error:
        raise RuntimeError("Official plugin source root is unavailable") from error
    if _is_link(source_root) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("Official plugin source root must be a real directory")

    for current_root, directory_names, file_names in os.walk(
        source_root,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in directory_names:
            candidate = current / name
            metadata = candidate.lstat()
            if _is_link(candidate) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    "Official plugin source tree must not contain links or special files"
                )
        for name in file_names:
            candidate = current / name
            metadata = candidate.lstat()
            if _is_link(candidate) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(
                    "Official plugin source tree must not contain links or special files"
                )


def _zip_info(name):
    info = ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def collect_official_package_files(source_root):
    source_root = Path(source_root)
    _validate_source_tree(source_root)
    paths = [source_root / "manifest.json"]
    frontend = source_root / "frontend"
    for name in ("plugin.js", "plugin.css"):
        if (frontend / name).is_file():
            paths.append(frontend / name)
    assets = frontend / "assets"
    if assets.is_dir():
        paths.extend(path for path in assets.rglob("*") if path.is_file() and not path.is_symlink())
    backend = source_root / "backend"
    if backend.is_dir():
        paths.extend(
            path
            for path in backend.rglob("*.py")
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and "tests" not in path.parts
        )
    paths = sorted(set(paths), key=lambda path: path.relative_to(source_root).as_posix())
    return tuple((path.relative_to(source_root).as_posix(), path.read_bytes()) for path in paths)


def _content_descriptor(files):
    return [
        {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in files
    ]


def build_official_content_descriptor(source_root):
    return _content_descriptor(collect_official_package_files(source_root))


def canonical_content_digest_from_descriptor(files):
    descriptor = sorted(
        (
            {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
            for item in files
        ),
        key=lambda item: item["path"],
    )
    canonical_json = json.dumps(
        {"contentIdentityVersion": CONTENT_IDENTITY_VERSION, "files": descriptor},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_json).hexdigest()


def canonical_content_digest_from_source(source_root):
    return canonical_content_digest_from_descriptor(build_official_content_descriptor(source_root))


def canonical_content_digest_from_package(payload):
    from .package import inspect_package

    inspected = inspect_package(payload)
    return canonical_content_digest_from_descriptor(inspected["files"])


def build_official_package(source_root):
    source_root = Path(source_root)
    files = collect_official_package_files(source_root)
    manifest = json.loads(dict(files)["manifest.json"].decode("utf-8"))
    descriptor = _content_descriptor(files)
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": manifest["slug"],
        "version": manifest["version"],
        "files": descriptor,
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in files:
            archive.writestr(_zip_info(name), payload)
        index_payload = json.dumps(
            package_index,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        archive.writestr(_zip_info("package-index.json"), index_payload)
    return output.getvalue()
