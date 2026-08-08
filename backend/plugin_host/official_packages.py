import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


OFFICIAL_PLUGIN_SLUGS = ("watch-history-importer",)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _zip_info(name):
    info = ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_official_package(source_root):
    source_root = Path(source_root)
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
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
    files = []
    for path in paths:
        relative = path.relative_to(source_root).as_posix()
        payload = path.read_bytes()
        files.append({"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": manifest["slug"],
        "version": manifest["version"],
        "files": files,
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in paths:
            name = path.relative_to(source_root).as_posix()
            archive.writestr(_zip_info(name), path.read_bytes())
        index_payload = json.dumps(
            package_index,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        archive.writestr(_zip_info("package-index.json"), index_payload)
    return output.getvalue()
