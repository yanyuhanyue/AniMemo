#!/usr/bin/env python3
"""Validate AniMemo license documents against repository evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_LICENSE_PATH = "LICENSE"
POLYFORM_PATH = "PolyForm-Noncommercial-1.0.0.md"
POLYFORM_BLOB = "5ecc88cfc4b1cff608ed640efe913c9dd97935c3"
POLYFORM_SHA256 = "c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5"
POLYFORM_SIZE = 4563
POLYFORM_LINES = 74

PRODUCT_IDENTITY = "AniMemo / My Anime Memory / 我的动漫记忆库"
README_ASSET_WARNING = (
    "发布前请确认品牌视觉、文案与图片素材均为自有、原创或已获得明确授权。"
    "第三方内容继续遵循其自身来源与使用条件。"
)
LEGACY_PROVENANCE_MARKERS = (
    "xh-anime.com",
    "复刻",
    "replica",
    "copied from",
    "copy of",
    "based on",
    "clone",
)
RELEASE_DOCUMENTS = (
    "README.md",
    "NOTICE",
    "TRADEMARKS",
    "THIRD_PARTY_NOTICES",
    "plugins/_template/README.md",
    "plugins/watch-history-importer/README.md",
    "bridges/astrbot_plugin_animemo_bridge/README.md",
)

EVIDENCE_SHA256 = {
    "package-lock.json": "576f0b6f10b67ef0a4360f1028722f64bbd4c9f08eb972ecdec332ec6d4d54e5",
    "backend/requirements.txt": "db0c4cfeeea40f0b8a7d4d7f392b8d493caedf0f467ea8d1a6a8358c506b5a90",
    "release/requirements.txt": "c91590bac77fab44ba03211bf8c0330ce0b5ba36f0f27927af37138278acd563",
    "bridges/astrbot_plugin_animemo_bridge/requirements.txt": (
        "9d25b578e8e7489ee686d0eabaf1b2b2444b3b0761ac3d55548f9e00c99fb2de"
    ),
    "scripts/requirements-tools.txt": (
        "b33d39fc69d850a919bda957bce5fc19f49d66ec3097346001c4b5e4633d002c"
    ),
}

KEY_NODE_PACKAGES = {
    "@fontsource/noto-sans-sc": ("5.3.0", "OFL-1.1"),
    "@fortawesome/fontawesome-svg-core": ("6.7.2", "MIT"),
    "@fortawesome/free-solid-svg-icons": ("6.7.2", "(CC-BY-4.0 AND MIT)"),
    "@fortawesome/react-fontawesome": ("3.5.0", "MIT"),
    "caniuse-lite": ("1.0.30001806", "CC-BY-4.0"),
    "gsap": (
        "3.15.0",
        "Standard 'no charge' license: https://gsap.com/standard-license.",
    ),
}

KEY_PYTHON_PACKAGES = {
    "certifi": "2026.7.22",
    "cffi": "2.1.1",
    "drf-spectacular-sidecar": "2026.8.1",
    "psycopg": "3.3.4",
    "psycopg-binary": "3.3.4",
    "typing-extensions": "4.16.0",
}

EXPECTED_NODE_LICENSE_SUMMARY = {
    "(CC-BY-4.0 AND MIT)": (1, 1, 0),
    "Apache-2.0": (18, 3, 15),
    "BlueOak-1.0.0": (1, 0, 1),
    "BSD-2-Clause": (6, 0, 6),
    "BSD-3-Clause": (2, 1, 1),
    "CC-BY-4.0": (1, 1, 0),
    "ISC": (14, 11, 3),
    "MIT": (242, 194, 48),
    "OFL-1.1": (1, 1, 0),
    "Standard 'no charge' license: https://gsap.com/standard-license.": (1, 1, 0),
}

INTERNAL_POLYFORM_DECLARATIONS = {
    "plugins/_template/manifest.json": ("json", "PolyForm-Noncommercial-1.0.0"),
    "plugins/watch-history-importer/manifest.json": ("json", "PolyForm-Noncommercial-1.0.0"),
    "bridges/astrbot_plugin_animemo_bridge/metadata.yaml": ("yaml", "PolyForm-Noncommercial-1.0.0"),
}
OFFICIAL_IMPORTER_VERSION = "0.4.4"
OFFICIAL_IMPORTER_ROOT = "plugins/watch-history-importer"


class ValidationError(RuntimeError):
    pass


def _read_bytes(relative: str) -> bytes:
    path = ROOT / relative
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValidationError(f"unable to read {relative}: {error}") from error


def _read_text(relative: str) -> str:
    try:
        return _read_bytes(relative).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{relative} is not UTF-8: {error}") from error


def _sha256(relative: str) -> str:
    return hashlib.sha256(_read_bytes(relative)).hexdigest()


def _normalized_text_sha256(relative: str) -> str:
    return hashlib.sha256(_read_text(relative).encode("utf-8")).hexdigest()


def _logical_lines(payload: bytes) -> int:
    # The audit contract counts the final empty position after the official
    # trailing LF, yielding 74 for the 73-LF PolyForm object.
    return payload.count(b"\n") + 1


def _git_blob(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _section(text: str, start: str, end: str) -> str:
    pattern = re.compile(
        rf"{re.escape(start)}\n(?P<body>.*?){re.escape(end)}",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ValidationError(f"generated section is missing or malformed: {start}")
    return match.group("body").strip("\n")


def _markdown_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r"-+:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _validate_polyform_payload(payload: bytes, relative: str) -> None:
    _require(_git_blob(payload) == POLYFORM_BLOB, "PolyForm Git blob identity changed")
    _require(
        hashlib.sha256(payload).hexdigest() == POLYFORM_SHA256,
        f"PolyForm SHA-256 changed: {relative}",
    )
    _require(len(payload) == POLYFORM_SIZE, f"PolyForm byte size changed: {relative}")
    _require(
        _logical_lines(payload) == POLYFORM_LINES,
        f"PolyForm logical line count changed: {relative}",
    )
    _require(b"\r" not in payload, f"PolyForm text must retain official LF-only bytes: {relative}")


def validate_polyform() -> None:
    canonical = _read_bytes(POLYFORM_PATH)
    root_license = _read_bytes(ROOT_LICENSE_PATH)
    _validate_polyform_payload(canonical, POLYFORM_PATH)
    _validate_polyform_payload(root_license, ROOT_LICENSE_PATH)
    _require(root_license == canonical, "root LICENSE is not byte-identical to the verified PolyForm copy")


def validate_documents() -> None:
    for relative in (ROOT_LICENSE_PATH, POLYFORM_PATH, "README.md", "NOTICE", "TRADEMARKS", "THIRD_PARTY_NOTICES"):
        _require((ROOT / relative).is_file(), f"required document is missing: {relative}")

    readme = _read_text("README.md")
    _require(PRODUCT_IDENTITY in readme, "README product identity is missing")
    _require(README_ASSET_WARNING in readme, "README asset authorization warning was removed")
    for link in (
        "[PolyForm Noncommercial License 1.0.0](LICENSE)",
        "[PolyForm-Noncommercial-1.0.0.md](PolyForm-Noncommercial-1.0.0.md)",
        "[NOTICE](NOTICE)",
        "[TRADEMARKS](TRADEMARKS)",
        "[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)",
        "[许可证来源审计](docs/license-provenance-audit-20260813.md)",
    ):
        _require(link in readme, f"README license entry is missing {link}")

    notice = _read_text("NOTICE")
    notice_folded = notice.casefold()
    for phrase in (
        "No legal rights-holder name is inferred or inserted",
        "public/assets/avatar.png",
        "poster-01.webp",
        "poster-02.webp` through `poster-16.webp",
        "AniMemo-created",
        "not relicensed by PolyForm",
        "src/data/anime.js",
        "plugins/_template/manifest.json",
        "plugins/watch-history-importer/manifest.json",
        "bridges/astrbot_plugin_animemo_bridge/metadata.yaml",
        "Bangumi",
        "third-party",
    ):
        _require(phrase.casefold() in notice_folded, f"NOTICE boundary is missing: {phrase}")

    trademarks = _read_text("TRADEMARKS")
    for phrase in (
        "AniMemo",
        "My Anime Memory",
        "我的动漫记忆库",
        "Bangumi",
        "does not infer or name a legal rights holder",
        "Nothing in `LICENSE` re-licenses Bangumi APIs or data",
    ):
        _require(phrase in trademarks, f"TRADEMARKS boundary is missing: {phrase}")

    third_party = _read_text("THIRD_PARTY_NOTICES")
    for phrase in (
        "Noto Sans SC",
        "Font Awesome Free",
        "caniuse-lite",
        "GSAP",
        "Psycopg",
        "LGPL-3.0-only",
        "MPL-2.0",
        "PSF-2.0",
        "MIT-0",
        "Redoc (`MIT`) and Swagger UI (`Apache-2.0`)",
        "Range only; the exact installed version and transitives are environment-dependent",
        "Bangumi metadata and images",
        "historical version `0.4.2` remains an immutable package identity",
    ):
        _require(phrase in third_party, f"THIRD_PARTY_NOTICES is missing: {phrase}")

    for relative in RELEASE_DOCUMENTS:
        text = _read_text(relative).casefold()
        for marker in LEGACY_PROVENANCE_MARKERS:
            _require(
                marker.casefold() not in text,
                f"legacy provenance marker remains in release-facing document {relative}: {marker}",
            )


def validate_evidence_hashes() -> None:
    third_party = _read_text("THIRD_PARTY_NOTICES")
    for relative, expected in EVIDENCE_SHA256.items():
        actual = _normalized_text_sha256(relative)
        _require(actual == expected, f"evidence input changed: {relative}: {actual}")
        _require(expected in third_party, f"THIRD_PARTY_NOTICES omits {relative} SHA-256")


def _node_lock() -> dict:
    try:
        return json.loads(_read_text("package-lock.json"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"package-lock.json is invalid JSON: {error}") from error


def _render_node_direct(lock: dict) -> str:
    packages = lock["packages"]
    root = packages[""]
    lines = [
        "| Scope | Package | Declared | Locked | License metadata |",
        "|---|---|---:|---:|---|",
    ]
    for scope in ("dependencies", "devDependencies"):
        for name in sorted(root.get(scope, {})):
            package = packages[f"node_modules/{name}"]
            lines.append(
                f"| {scope} | `{name}` | `{root[scope][name]}` | `{package['version']}` | `{package['license']}` |"
            )
    return "\n".join(lines)


def _node_license_summary(lock: dict) -> dict[str, tuple[int, int, int]]:
    counts: dict[str, Counter] = {}
    for path, package in lock["packages"].items():
        if not path or not package.get("version"):
            continue
        license_name = package.get("license", "(missing)")
        counter = counts.setdefault(license_name, Counter())
        counter["total"] += 1
        counter["dev" if package.get("dev") else "prod"] += 1
    return {
        name: (counter["total"], counter["prod"], counter["dev"])
        for name, counter in counts.items()
    }


def _render_node_summary(summary: dict[str, tuple[int, int, int]]) -> str:
    lines = [
        "| License metadata | Total | Non-dev | Dev |",
        "|---|---:|---:|---:|",
    ]
    for license_name in sorted(summary, key=str.casefold):
        total, prod, dev = summary[license_name]
        lines.append(f"| `{license_name}` | {total} | {prod} | {dev} |")
    return "\n".join(lines)


def validate_node_inventory() -> None:
    lock = _node_lock()
    _require(lock.get("lockfileVersion") == 3, "unexpected npm lockfile version")
    packages = lock.get("packages", {})
    versioned = [package for path, package in packages.items() if path and package.get("version")]
    _require(len(versioned) == 287, "Node lock versioned package count changed")
    _require(all(package.get("license") for package in versioned), "Node lock contains missing license metadata")

    for name, (version, license_name) in KEY_NODE_PACKAGES.items():
        package = packages.get(f"node_modules/{name}")
        _require(package is not None, f"Node lock is missing {name}")
        _require(package.get("version") == version, f"Node version changed for {name}")
        _require(package.get("license") == license_name, f"Node license metadata changed for {name}")

    summary = _node_license_summary(lock)
    _require(summary == EXPECTED_NODE_LICENSE_SUMMARY, "Node license summary changed")

    third_party = _read_text("THIRD_PARTY_NOTICES")
    actual_direct = _section(
        third_party,
        "<!-- BEGIN GENERATED: NODE DIRECT DEPENDENCIES -->",
        "<!-- END GENERATED: NODE DIRECT DEPENDENCIES -->",
    )
    _require(actual_direct == _render_node_direct(lock), "Node direct dependency table is stale")
    actual_summary = _section(
        third_party,
        "<!-- BEGIN GENERATED: NODE LICENSE SUMMARY -->",
        "<!-- END GENERATED: NODE LICENSE SUMMARY -->",
    )
    _require(actual_summary == _render_node_summary(summary), "Node license summary table is stale")


def _python_pins() -> list[tuple[str, str]]:
    pins = []
    for line in _read_text("backend/requirements.txt").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;]+)(?:\s*;.*)?", line)
        if match:
            pins.append((match.group(1).lower(), match.group(2)))
    return pins


def validate_python_inventory() -> None:
    pins = _python_pins()
    _require(len(pins) == 44, "Python production pin count changed")
    versions = dict(pins)
    for name, version in KEY_PYTHON_PACKAGES.items():
        _require(versions.get(name) == version, f"Python version changed for {name}")

    third_party = _read_text("THIRD_PARTY_NOTICES")
    section = _section(
        third_party,
        "<!-- BEGIN GENERATED: PYTHON PRODUCTION DEPENDENCIES -->",
        "<!-- END GENERATED: PYTHON PRODUCTION DEPENDENCIES -->",
    )
    rows = _markdown_rows(section)
    _require(rows and rows[0][:2] == ["Package", "Locked"], "Python inventory table header changed")
    documented = [(row[0].lower(), row[1]) for row in rows[1:] if len(row) >= 2]
    _require(documented == pins, "Python production dependency table is stale or reordered")

    _require(
        _read_text("release/requirements.txt").splitlines()
        == ["jsonschema==4.26.0", "packaging==25.0", "pyyaml==6.0.3"],
        "release requirements changed",
    )
    _require(
        _read_text("bridges/astrbot_plugin_animemo_bridge/requirements.txt").splitlines()
        == ["httpx>=0.27,<1"],
        "AstrBot bridge requirements changed",
    )


def validate_internal_declarations() -> None:
    for relative, (kind, expected) in INTERNAL_POLYFORM_DECLARATIONS.items():
        if kind == "json":
            value = json.loads(_read_text(relative)).get("license")
        else:
            match = re.search(r"(?m)^license:\s*(\S+)\s*$", _read_text(relative))
            value = match.group(1) if match else None
        _require(value == expected, f"internal PolyForm declaration is missing or changed: {relative}")


def _official_runtime_paths(root: Path) -> list[Path]:
    paths = [root / "manifest.json"]
    frontend = root / "frontend"
    for name in ("plugin.js", "plugin.css"):
        candidate = frontend / name
        if candidate.is_file():
            paths.append(candidate)
    assets = frontend / "assets"
    if assets.is_dir():
        paths.extend(path for path in assets.rglob("*") if path.is_file() and not path.is_symlink())
    backend = root / "backend"
    if backend.is_dir():
        paths.extend(
            path
            for path in backend.rglob("*.py")
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and "tests" not in path.parts
        )
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def validate_official_importer_metadata() -> None:
    root = ROOT / OFFICIAL_IMPORTER_ROOT
    manifest = json.loads(_read_text(f"{OFFICIAL_IMPORTER_ROOT}/manifest.json"))
    _require(manifest.get("version") == OFFICIAL_IMPORTER_VERSION, "watch-history-importer manifest version is stale")
    _require(
        manifest.get("license") == "PolyForm-Noncommercial-1.0.0",
        "watch-history-importer manifest does not declare PolyForm",
    )
    index = json.loads(_read_text(f"{OFFICIAL_IMPORTER_ROOT}/package-index.json"))
    _require(index.get("packageVersion") == 1, "watch-history-importer package index version changed")
    _require(index.get("pluginId") == manifest.get("id"), "watch-history-importer package index id is stale")
    _require(index.get("slug") == manifest.get("slug"), "watch-history-importer package index slug is stale")
    _require(index.get("version") == OFFICIAL_IMPORTER_VERSION, "watch-history-importer package index version is stale")

    expected = []
    for path in _official_runtime_paths(root):
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        expected.append({"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    _require(index.get("files") == expected, "watch-history-importer package-index.json is stale")
    _require(
        f'version: "{OFFICIAL_IMPORTER_VERSION}"' in _read_text(f"{OFFICIAL_IMPORTER_ROOT}/frontend/index.jsx"),
        "watch-history-importer frontend source version is stale",
    )
    _require(
        f'version: "{OFFICIAL_IMPORTER_VERSION}"' in _read_text(f"{OFFICIAL_IMPORTER_ROOT}/frontend/plugin.js"),
        "watch-history-importer bundled frontend version is stale",
    )
    _require(
        f'PLUGIN_VERSION = "{OFFICIAL_IMPORTER_VERSION}"' in _read_text(f"{OFFICIAL_IMPORTER_ROOT}/backend/plugin.py"),
        "watch-history-importer backend version is stale",
    )


def _git_diff_names(base: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", base, "--"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise ValidationError(completed.stderr.strip() or f"git diff failed for {base}")
    return [line.replace("\\", "/") for line in completed.stdout.splitlines() if line]


def validate_protected_paths(base: str) -> None:
    changed = set(_git_diff_names(base))
    protected = {
        "public/assets/avatar.png",
        *(f"public/assets/posters/poster-{number:02d}.webp" for number in range(1, 17)),
        "package.json",
        "package-lock.json",
        "backend/requirements.in",
        "backend/requirements.txt",
        "release/requirements.txt",
        "bridges/astrbot_plugin_animemo_bridge/requirements.txt",
        "scripts/requirements-tools.txt",
    }
    violations = sorted(
        path
        for path in changed
        if path in protected
        or path.startswith("public/assets/")
        or path.startswith("src/pages/")
        or path.startswith("src/data/")
    )
    _require(not violations, f"protected path changed relative to {base}: {', '.join(violations)}")


def validate_all(*, base: str | None = None) -> None:
    validate_polyform()
    validate_documents()
    validate_evidence_hashes()
    validate_node_inventory()
    validate_python_inventory()
    validate_internal_declarations()
    validate_official_importer_metadata()
    if base:
        validate_protected_paths(base)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        validate_all(base=base)
    except ValidationError as error:
        print(f"license documentation validation: FAIL: {error}", file=sys.stderr)
        return 1
    print("license documentation validation: PASS")
    print(
        f"PolyForm: blob={POLYFORM_BLOB} sha256={POLYFORM_SHA256} "
        f"bytes={POLYFORM_SIZE} lines={POLYFORM_LINES}"
    )
    if base:
        print(f"Protected-path comparison base: {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
