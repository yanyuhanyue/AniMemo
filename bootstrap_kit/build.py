"""Deterministic DEV kit builder from explicitly hash-selected local inputs."""
from __future__ import annotations

import io
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bootstrap_kit import manifest
from bootstrap_kit.safe_files import (
    exclusive_file,
    file_digest,
    held_file,
    read_bounded,
    safe_chain,
    safe_relative,
)


@dataclass(frozen=True)
class BuildInput:
    path: str
    source: Path
    sha256: str
    role: str


@dataclass(frozen=True)
class ToolInput:
    name: str
    source: Path
    sha256: str
    size: int


def _tools(inputs):
    manifest.require(type(inputs) in (list, tuple) and 1 <= len(inputs) <= 128)
    records, names, total = [], set(), 0
    for item in inputs:
        manifest.require(type(item) is ToolInput and item.name not in names)
        names.add(item.name)
        manifest.require(type(item.size) is int and 0 < item.size <= manifest.MAX_ARCHIVE)
        total += item.size
        manifest.require(total <= manifest.MAX_ARCHIVE)
        with held_file(item.source, manifest.MAX_ARCHIVE) as stream:
            manifest.require(file_digest(stream) == (item.sha256, item.size), "BOOTSTRAP_KIT_TOOL_INPUT_CHANGED")
        records.append({"name": item.name, "sha256": item.sha256, "size": item.size})
    return sorted(records, key=lambda item: item["name"])


def _inputs(inputs):
    manifest.require(type(inputs) in (list, tuple) and 1 <= len(inputs) <= manifest.MAX_MEMBERS)
    data, seen, total = [], set(), 0
    for item in inputs:
        manifest.require(type(item) is BuildInput and item.role in manifest.ROLES)
        name = safe_relative(item.path)
        manifest.require(name.casefold() not in seen, "BOOTSTRAP_KIT_DUPLICATE_INPUT")
        seen.add(name.casefold())
        raw = read_bounded(item.source, manifest.MAX_FILE)
        manifest.require(manifest.digest(raw) == item.sha256, "BOOTSTRAP_KIT_BUILD_INPUT_CHANGED")
        total += len(raw)
        manifest.require(total <= manifest.MAX_ARCHIVE)
        data.append((item, raw))
    return sorted(data, key=lambda pair: pair[0].path)


def build_entry(entry_sources):
    """Whole entry.pyz is the OS-verified seed, not an untrusted tar member."""
    entries = _inputs(entry_sources)
    required = {"bootstrap_kit/__init__.py", "bootstrap_kit/seed.py",
                "bootstrap_kit/manifest.py", "bootstrap_kit/safe_files.py",
                "bootstrap_kit/http_supervisor.py", "bootstrap_kit/http_worker.py",
                "bootstrap_kit/http_protocol.py", "bootstrap_kit/owned_process.py",
                "bootstrap_kit/egress.py"}
    manifest.require(required <= {item.path for item, _ in entries}, "BOOTSTRAP_KIT_ENTRY_CLOSURE_INCOMPLETE")
    manifest.require(all(item.path.startswith("bootstrap_kit/") and item.path.endswith(".py")
                         for item, _ in entries))
    main = b"from bootstrap_kit.seed import main\nraise SystemExit(main())\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name, raw in sorted([("__main__.py", main), *[(item.path, raw) for item, raw in entries]]):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    result = output.getvalue()
    manifest.require(len(result) <= manifest.MAX_FILE)
    return result


def build_kit(*, inputs, entry_sources, tool_inputs, output_dir, metadata, now: datetime):
    """No discovery, downloaded authority, product tools or implicit source pins."""
    manifest.require(metadata.get("classification") == "DEVELOPMENT_ONLY",
                     "BOOTSTRAP_KIT_BUILDER_DEVELOPMENT_ONLY")
    output_dir = safe_chain(output_dir)
    data = _inputs(inputs)
    tools = _tools(tool_inputs)
    manifest.require(sorted(metadata["build"]["toolInputs"], key=lambda item: item["name"]) == tools,
                     "BOOTSTRAP_KIT_TOOL_INPUT_SET_MISMATCH")
    selected_entry_sources = _inputs(entry_sources)
    entry = build_entry(entry_sources)
    # Manifest outside tar avoids recursive package identity. USTAR avoids
    # extension records with a second path/size interpretation.
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for item, raw in data:
            member = tarfile.TarInfo(item.path)
            member.size, member.mode, member.mtime = len(raw), 0o600, 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(raw))
    archive_raw = archive_bytes.getvalue()
    manifest.require(len(archive_raw) <= manifest.MAX_ARCHIVE)
    value = {**metadata, "entry": {"name": "entry.pyz", "sha256": manifest.digest(entry), "size": len(entry)},
             "archive": {"sha256": manifest.digest(archive_raw), "size": len(archive_raw)},
             "members": [{"path": item.path, "type": "file", "size": len(raw),
                          "sha256": item.sha256, "role": item.role} for item, raw in data]}
    build_record = {"source": value["source"], "members": value["members"],
                    "entrySources": [{"path": item.path, "size": len(raw), "sha256": item.sha256}
                                     for item, raw in selected_entry_sources], "toolInputs": tools}
    value["build"] = {"identity": manifest.digest(manifest.canonical(build_record)), "toolInputs": tools}
    raw = manifest.canonical(value)
    source = value["source"]
    # Build-time consistency checking does not replace external operator selection.
    check = manifest.OperatorSelection(manifest.digest(raw), value["platform"],
        value["minimumSequence"], value["minimumKitVersion"], manifest.digest(entry), len(entry),
        source["commit"], source["tree"])
    manifest.validate_manifest(raw, check, now=now)
    paths = {"manifest": output_dir / "manifest.json", "archive": output_dir / "kit.tar",
             "entry": output_dir / "entry.pyz"}
    for name, content in (("manifest", raw), ("archive", archive_raw), ("entry", entry)):
        with exclusive_file(paths[name]) as stream:
            stream.write(content)
    return {"authority": "DEVELOPMENT_ONLY_NOT_RELEASE_NOT_QUALIFIED", "paths": paths,
            "manifest_sha256": manifest.digest(raw), "archive_sha256": manifest.digest(archive_raw),
            "entry_sha256": manifest.digest(entry), "entry_size": len(entry),
            "source_commit": source["commit"], "source_tree": source["tree"],
            "build_identity": value["build"]["identity"]}
