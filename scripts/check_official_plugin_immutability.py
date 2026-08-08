#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from packaging.version import InvalidVersion, Version

from ci_refs import RefResolutionError, resolve_refs


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_MODULE_PATH = "backend/plugin_host/official_packages.py"
LEGACY_SYNC_PATH = "backend/plugin_host/management/commands/sync_official_plugins.py"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CONTENT_IDENTITY_VERSION = 1


class GateInputError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginIdentity:
    version: str
    content_digest: str
    package_sha: str


@dataclass(frozen=True)
class Violation:
    code: str
    slug: str
    detail: str


@dataclass(frozen=True)
class PluginResult:
    slug: str
    status: str
    base: PluginIdentity | None
    current: PluginIdentity | None


@dataclass(frozen=True)
class GateReport:
    base_ref: str
    head_ref: str
    results: tuple[PluginResult, ...]
    violations: tuple[Violation, ...]

    @property
    def ok(self):
        return not self.violations


def _git(repo, *args, check=True):
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GateInputError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _file_at_ref(repo, ref, path):
    completed = _git(repo, "show", f"{ref}:{path}", check=False)
    if completed.returncode:
        return None
    return completed.stdout


def _official_slugs_from_source(source, source_path):
    try:
        tree = ast.parse(source.decode("utf-8"), filename=source_path)
    except (UnicodeDecodeError, SyntaxError) as error:
        raise GateInputError(f"Unable to parse {source_path}: {error}") from error
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "OFFICIAL_PLUGIN_SLUGS" for target in targets):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError) as error:
                    raise GateInputError(f"OFFICIAL_PLUGIN_SLUGS in {source_path} must be a literal sequence.") from error
                if not isinstance(value, (tuple, list)) or not all(isinstance(item, str) and item for item in value):
                    raise GateInputError(f"OFFICIAL_PLUGIN_SLUGS in {source_path} is invalid.")
                return tuple(value)
    raise GateInputError(f"OFFICIAL_PLUGIN_SLUGS is missing from {source_path}.")


def _official_slugs_at_ref(repo, ref):
    for path in (OFFICIAL_MODULE_PATH, LEGACY_SYNC_PATH):
        source = _file_at_ref(repo, ref, path)
        if source is not None:
            return _official_slugs_from_source(source, path)
    raise GateInputError(f"Official plugin registry is unavailable at {ref}.")


def _official_slugs_in_worktree(root):
    for path in (OFFICIAL_MODULE_PATH, LEGACY_SYNC_PATH):
        candidate = root / path
        if candidate.is_file():
            return _official_slugs_from_source(candidate.read_bytes(), path)
    raise GateInputError(f"Official plugin registry is unavailable in {root}.")


def _zip_info(name):
    info = ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _legacy_collect_official_package_files(source_root):
    source_root = Path(source_root)
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


def _legacy_build_official_content_descriptor(source_root):
    return [
        {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in _legacy_collect_official_package_files(source_root)
    ]


def _legacy_build_official_package(source_root):
    files = _legacy_collect_official_package_files(source_root)
    manifest = json.loads(dict(files)["manifest.json"].decode("utf-8"))
    descriptor = [
        {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in files
    ]
    package_index = {
        "packageVersion": 1,
        "pluginId": manifest["id"],
        "slug": manifest["slug"],
        "version": manifest["version"],
        "files": descriptor,
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, payload in files:
            archive.writestr(_zip_info(path), payload)
        archive.writestr(
            _zip_info("package-index.json"),
            json.dumps(package_index, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )
    return output.getvalue()


def _official_api_from_source(source, source_path):
    namespace = {"__name__": "official_packages_gate_ref"}
    try:
        exec(compile(source, source_path, "exec"), namespace)
    except Exception as error:
        raise GateInputError(f"Unable to load official package builder from {source_path}: {error}") from error
    builder = namespace.get("build_official_package")
    if not callable(builder):
        raise GateInputError(f"build_official_package is missing from {source_path}.")
    descriptor_builder = namespace.get("build_official_content_descriptor")
    if descriptor_builder is not None and not callable(descriptor_builder):
        raise GateInputError(f"build_official_content_descriptor is invalid in {source_path}.")
    digest_builder = namespace.get("canonical_content_digest_from_descriptor")
    if digest_builder is not None and not callable(digest_builder):
        raise GateInputError(f"canonical_content_digest_from_descriptor is invalid in {source_path}.")
    return (
        builder,
        descriptor_builder or _legacy_build_official_content_descriptor,
        digest_builder or _canonical_content_digest,
    )


def _official_api_at_ref(repo, ref):
    source = _file_at_ref(repo, ref, OFFICIAL_MODULE_PATH)
    if source is None:
        return _legacy_build_official_package, _legacy_build_official_content_descriptor, _canonical_content_digest
    return _official_api_from_source(source.decode("utf-8"), f"{ref}:{OFFICIAL_MODULE_PATH}")


def _official_api_in_worktree(root):
    candidate = root / OFFICIAL_MODULE_PATH
    if not candidate.is_file():
        return _legacy_build_official_package, _legacy_build_official_content_descriptor, _canonical_content_digest
    return _official_api_from_source(candidate.read_text(encoding="utf-8"), str(candidate))


def _extract_plugin(repo, ref, slug, destination):
    path = f"plugins/{slug}"
    completed = _git(repo, "ls-tree", "-r", "-z", ref, "--", path, check=False)
    if completed.returncode:
        return None
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            git_path = PurePosixPath(raw_path.decode("utf-8"))
            relative = git_path.relative_to(PurePosixPath(path))
        except (ValueError, UnicodeDecodeError) as error:
            raise GateInputError(f"Unable to read raw Git tree entry for {path}: {error}") from error
        if object_type != b"blob" or mode == b"120000" or not relative.parts:
            continue
        payload = _git(repo, "cat-file", "blob", object_id.decode("ascii")).stdout
        target = destination.joinpath(path, *relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    root = destination / path
    return root if (root / "manifest.json").is_file() else None


def _worktree_path_matches_git(repo, ref, git_path):
    completed = _git(repo, "diff", "--quiet", ref, "--", git_path, check=False)
    return completed.returncode == 0


def _canonicalize_worktree_descriptor(repo, ref, plugin_root, slug, descriptor):
    canonical = []
    for item in descriptor:
        relative = PurePosixPath(item["path"])
        candidate = plugin_root.joinpath(*relative.parts)
        git_path = f"plugins/{slug}/{relative.as_posix()}"
        raw = _file_at_ref(repo, ref, git_path)
        payload = raw if raw is not None and _worktree_path_matches_git(repo, ref, git_path) else candidate.read_bytes()
        canonical.append({"path": relative.as_posix(), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return canonical


def _parse_semver(value, slug):
    if not isinstance(value, str) or not SEMVER_RE.fullmatch(value):
        raise GateInputError(f"{slug}: version {value!r} does not match the repository SemVer format.")
    try:
        return Version(value)
    except InvalidVersion as error:
        raise GateInputError(f"{slug}: version {value!r} is invalid SemVer.") from error


def _canonical_content_digest(files):
    descriptor = sorted(
        ({"path": item["path"], "size": item["size"], "sha256": item["sha256"]} for item in files),
        key=lambda item: item["path"],
    )
    canonical_json = json.dumps(
        {"contentIdentityVersion": CONTENT_IDENTITY_VERSION, "files": descriptor},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_json).hexdigest()


def _identity(source_root, official_api, slug):
    builder, descriptor_builder, digest_builder = official_api
    try:
        manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
        version = manifest["version"]
        _parse_semver(version, slug)
        descriptor = descriptor_builder(source_root)
        content_digest = digest_builder(descriptor)
        payload = builder(source_root)
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise GateInputError(f"{slug}: unable to build official package identity: {error}") from error
    return PluginIdentity(
        version=version,
        content_digest=content_digest,
        package_sha=hashlib.sha256(payload).hexdigest(),
    )


def _ref_identity(repo, ref, slug, temporary_root):
    plugin_root = _extract_plugin(repo, ref, slug, temporary_root)
    if plugin_root is None:
        return None
    return _identity(plugin_root, _official_api_at_ref(repo, ref), slug)


def _worktree_identity(repo, ref, root, slug):
    plugin_root = root / "plugins" / slug
    if not (plugin_root / "manifest.json").is_file():
        return None
    official_api = _official_api_in_worktree(root)
    builder, descriptor_builder, digest_builder = official_api
    try:
        manifest = json.loads((plugin_root / "manifest.json").read_text(encoding="utf-8"))
        version = manifest["version"]
        _parse_semver(version, slug)
        descriptor = _canonicalize_worktree_descriptor(
            repo,
            ref,
            plugin_root,
            slug,
            descriptor_builder(plugin_root),
        )
        payload = builder(plugin_root)
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise GateInputError(f"{slug}: unable to build official package identity: {error}") from error
    return PluginIdentity(
        version=version,
        content_digest=digest_builder(descriptor),
        package_sha=hashlib.sha256(payload).hexdigest(),
    )


def check_repository(repo, base_ref, head_ref, *, head_root=None):
    repo = Path(repo).resolve()
    head_root = Path(head_root).resolve() if head_root else None
    base_slugs = set(_official_slugs_at_ref(repo, base_ref))
    head_slugs = set(_official_slugs_in_worktree(head_root) if head_root else _official_slugs_at_ref(repo, head_ref))
    results = []
    violations = []

    with tempfile.TemporaryDirectory(prefix="official-plugin-gate-") as temporary:
        temporary_root = Path(temporary)
        for slug in sorted(base_slugs | head_slugs):
            base_registered = slug in base_slugs
            head_registered = slug in head_slugs
            base = _ref_identity(repo, base_ref, slug, temporary_root / f"base-{slug}") if base_registered else None
            current = (
                _worktree_identity(repo, head_ref, head_root, slug)
                if head_registered and head_root
                else _ref_identity(repo, head_ref, slug, temporary_root / f"head-{slug}") if head_registered else None
            )

            if base_registered and not head_registered:
                detail = "Official plugin removal requires an explicit lifecycle decision; registry removal is not an unpublish mechanism."
                violations.append(Violation("removed_official_plugin", slug, detail))
                results.append(PluginResult(slug, "FAIL", base, current))
                continue
            if head_registered and current is None:
                detail = "The official plugin is registered but its publishable manifest/package is missing."
                violations.append(Violation("missing_official_plugin_package", slug, detail))
                results.append(PluginResult(slug, "FAIL", base, current))
                continue
            if not base_registered:
                results.append(PluginResult(slug, "NEW", None, current))
                continue
            if base is None:
                detail = "The base release registered this official plugin but its publishable package is missing."
                violations.append(Violation("invalid_base_official_plugin", slug, detail))
                results.append(PluginResult(slug, "FAIL", base, current))
                continue

            base_version = _parse_semver(base.version, slug)
            current_version = _parse_semver(current.version, slug)
            if current_version < base_version:
                detail = f"Official plugin version decreased from {base.version} to {current.version}."
                violations.append(Violation("version_downgrade", slug, detail))
                results.append(PluginResult(slug, "FAIL", base, current))
            elif current_version == base_version and current.content_digest != base.content_digest:
                detail = "Official plugin canonical content changed without a version bump."
                violations.append(Violation("immutable_content_changed", slug, detail))
                results.append(PluginResult(slug, "FAIL", base, current))
            else:
                results.append(PluginResult(slug, "PASS", base, current))

    return GateReport(base_ref=base_ref, head_ref=head_ref, results=tuple(results), violations=tuple(violations))


def _print_report(report, resolution_source):
    print(f"Official Plugin Gate Base SHA: {report.base_ref}")
    print(f"Official Plugin Gate Head SHA: {report.head_ref}")
    print(f"Base resolution: {resolution_source}")
    for result in report.results:
        print()
        print(f"Plugin: {result.slug}")
        print(f"Status: {result.status}")
        if result.base:
            print(f"Base version: {result.base.version}")
            print(f"Base content digest: {result.base.content_digest}")
            print(f"Base archive SHA: {result.base.package_sha}")
        else:
            print("Base version: NEW OFFICIAL PLUGIN")
        if result.current:
            print(f"Current version: {result.current.version}")
            print(f"Current content digest: {result.current.content_digest}")
            print(f"Current archive SHA: {result.current.package_sha}")
        else:
            print("Current package: MISSING")

    if report.ok:
        print("\nOfficial plugin immutability check: PASS")
        return
    print("\nOFFICIAL PLUGIN IMMUTABILITY VIOLATION", file=sys.stderr)
    for violation in report.violations:
        print(f"\nPlugin: {violation.slug}\nReason: {violation.detail}", file=sys.stderr)
    print(
        "\nFix one of:\n"
        "1. Restore the already-published package contents.\n"
        "2. Bump manifest version to a new SemVer version.\n\n"
        "Never rewrite an already-published immutable version.",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description="Reject official plugin package mutations without a version bump.")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--head-root", type=Path)
    args = parser.parse_args()
    try:
        refs = resolve_refs(repo=args.repo, explicit_base=args.base, explicit_head=args.head)
        report = check_repository(args.repo, refs.base, refs.head, head_root=args.head_root)
    except (RefResolutionError, GateInputError) as error:
        parser.exit(1, f"Official plugin immutability check failed: {error}\n")
    _print_report(report, refs.source)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
