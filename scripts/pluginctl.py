#!/usr/bin/env python3
"""Build, validate, package and inspect Anime Journal .ajplugin packages.

The command intentionally never installs dependencies. Runtime packages are
assembled from files already present in the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
OUT = ROOT / "dist" / "plugins"

ALLOWED_ROOT_FILES = {"manifest.json", "package-index.json"}
ALLOWED_TOP_DIRS = {"frontend", "backend"}
SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx"}
SHARED_IMPORTS = {
    "react": {
        "Children", "Component", "Fragment", "Profiler", "PureComponent", "StrictMode", "Suspense",
        "cloneElement", "createContext", "createElement", "forwardRef", "isValidElement", "lazy", "memo",
        "startTransition", "useCallback", "useContext", "useDebugValue", "useDeferredValue", "useEffect",
        "useId", "useImperativeHandle", "useInsertionEffect", "useLayoutEffect", "useMemo", "useOptimistic",
        "useReducer", "useRef", "useState", "useSyncExternalStore", "useTransition", "version",
    },
    "react/jsx-runtime": {"Fragment", "jsx", "jsxs", "jsxDEV"},
    "react-dom": {"createRoot", "hydrateRoot", "version"},
    "react-dom/client": {"createRoot", "hydrateRoot", "version"},
    "react-router-dom": {
        "Await", "BrowserRouter", "Form", "HashRouter", "Link", "MemoryRouter", "NavLink", "Navigate",
        "Outlet", "Route", "Router", "RouterProvider", "Routes", "ScrollRestoration", "useActionData",
        "useAsyncError", "useAsyncValue", "useBeforeUnload", "useFetcher", "useFetchers", "useFormAction",
        "useHref", "useInRouterContext", "useLinkClickHandler", "useLoaderData", "useLocation", "useMatch",
        "useMatches", "useNavigate", "useNavigation", "useNavigationType", "useOutlet", "useOutletContext",
        "useParams", "useResolvedPath", "useRevalidator", "useRouteError", "useRouteLoaderData", "useRoutes",
        "useSearchParams", "useSubmit", "useViewTransitionState", "createBrowserRouter", "createHashRouter",
        "createMemoryRouter", "defer", "generatePath", "isRouteErrorResponse", "json", "matchPath",
        "matchRoutes", "redirect", "redirectDocument", "resolvePath",
    },
}
IMPORT_FROM_RE = re.compile(r"^[ \t]*(?:import|export)[ \t]+([^;]+?)[ \t\r\n]+from[ \t\r\n]+['\"]([^'\"]+)['\"][ \t]*;", re.MULTILINE)
SIDE_EFFECT_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]*['\"]([^'\"]+)['\"][ \t]*;", re.MULTILINE)
DYNAMIC_IMPORT_RE = re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
USER_SCOPED_HOOKS = {
    "journal.after_create", "journal.after_update", "journal.after_delete",
    "column.after_publish", "column.after_delete",
}


def _boundary_error(detail=""):
    suffix = f" {detail}" if detail else ""
    raise SystemExit(
        "Plugin source import escapes plugin package boundary. Use the Host SDK instead."
        + suffix
    )


def _named_imports(clause):
    match = re.search(r"\{([\s\S]*?)\}", clause)
    if not match:
        return set()
    names = set()
    for item in match.group(1).split(","):
        name = item.strip().split(" as ", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _validate_shared_import(module, clause, source_path):
    supported = SHARED_IMPORTS[module]
    if re.search(r"(?:^|,)\s*(?:[A-Za-z_$][\w$]*\s*,\s*)?\*\s+as\s+", clause):
        raise SystemExit(
            f"PluginBuildError: Unsupported shared module import form in {source_path.name}: "
            f"namespace imports from {module} are not supported. Use supported named imports."
        )
    if module != "react" and re.match(r"\s*[A-Za-z_$][\w$]*(?:\s*,|\s*$)", clause):
        raise SystemExit(
            f"PluginBuildError: Unsupported shared module import form in {source_path.name}: "
            f"default imports from {module} are not supported. Use supported named imports."
        )
    unsupported = sorted(_named_imports(clause) - supported)
    if unsupported:
        raise SystemExit(
            f"PluginBuildError: unsupported {module} export(s) in {source_path.name}: {', '.join(unsupported)}"
        )


def _validate_source_imports(root: Path) -> None:
    package_root = root.resolve()
    frontend_root = (root / "frontend").resolve()
    for source_path in sorted(frontend_root.rglob("*")):
        if not source_path.is_file() or source_path.suffix.lower() not in SOURCE_SUFFIXES or source_path.name == "plugin.js":
            continue
        source = source_path.read_text(encoding="utf-8")
        imports = [(module, clause) for clause, module in IMPORT_FROM_RE.findall(source)]
        imports.extend((module, "") for module in SIDE_EFFECT_IMPORT_RE.findall(source))
        imports.extend((module, "") for module in DYNAMIC_IMPORT_RE.findall(source))
        for module, clause in imports:
            if module.startswith("."):
                candidate = (source_path.parent / module).resolve()
                try:
                    candidate.relative_to(package_root)
                except ValueError:
                    _boundary_error(f"Offending import: {module} in {source_path.relative_to(root)}")
                continue
            if module not in SHARED_IMPORTS:
                _boundary_error(f"Unsupported package import: {module} in {source_path.relative_to(root)}")
            _validate_shared_import(module, clause, source_path)


def _validate_metafile(meta_path: Path, plugin_root: Path) -> None:
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    root = plugin_root.resolve()
    for input_path in metadata.get("inputs", {}):
        candidate = (ROOT / input_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            _boundary_error(f"Bundled input: {input_path}")


def _runtime_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts or any(part.startswith(".") for part in relative.parts):
            continue
        if len(relative.parts) == 1:
            if relative.name in ALLOWED_ROOT_FILES:
                files.append(path)
            continue
        if relative.parts[0] not in ALLOWED_TOP_DIRS:
            continue
        if relative.parts[0] == "frontend" and relative.name not in {"plugin.js", "plugin.css"} and relative.parts[1] != "assets":
            continue
        if relative.parts[0] == "backend" and (relative.name == "pyproject.toml" or "tests" in relative.parts or relative.name in {"apps.py", "models.py", "admin.py", "urls.py", "migrations"} or "migrations" in relative.parts):
            continue
        if path.suffix in {".jsx", ".tsx", ".ts", ".map", ".pyc"} or path.name in {"package-lock.json", "vite.config.js", "vite.config.mjs"}:
            continue
        files.append(path)
    return sorted(files)


def read_manifest(slug: str) -> dict:
    path = PLUGINS / slug / "manifest.json"
    if not path.is_file():
        raise SystemExit(f"manifest.json not found for {slug}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 2 or manifest.get("sdkApi") != 2:
        raise SystemExit("only Manifest v2 / SDK API v2 packages are supported")
    if manifest.get("slug") != slug:
        raise SystemExit("manifest slug does not match directory")
    if not isinstance((manifest.get("author") or {}).get("name"), str) or not manifest["author"]["name"].strip():
        raise SystemExit("manifest author.name is required")
    if not isinstance(manifest.get("license"), str) or not manifest["license"].strip():
        raise SystemExit("manifest license is required")
    if manifest.get("installationMode") not in {"user", "system"}:
        raise SystemExit("manifest installationMode must be user or system")
    if manifest.get("installationMode") == "user" and not set(manifest.get("hooks") or []) <= USER_SCOPED_HOOKS:
        raise SystemExit("USER plugins may only declare user-scoped journal/column hooks")
    return manifest


def validate(slug: str | None = None) -> None:
    candidates = [slug] if slug else [p.name for p in PLUGINS.iterdir() if p.is_dir() and not p.name.startswith("_")]
    for item in candidates:
        manifest = read_manifest(item)
        root = PLUGINS / item
        _validate_source_imports(root)
        if "frontend" in manifest.get("runtimes", []) and not (root / "frontend" / "plugin.js").is_file():
            raise SystemExit(f"{item}: frontend/plugin.js is missing; run pluginctl build first")
        if "backend" in manifest.get("runtimes", []) and not (root / "backend" / "plugin.py").is_file():
            raise SystemExit(f"{item}: backend/plugin.py is missing")
    print(f"validated {len(candidates)} plugin(s)")


def build(slug: str) -> None:
    manifest = read_manifest(slug)
    root = PLUGINS / slug
    frontend = root / "frontend"
    if "frontend" not in manifest.get("runtimes", []):
        print(f"{slug}: backend-only plugin, no frontend build needed")
        return
    source = frontend / "index.jsx"
    entry = frontend / "plugin.js"
    if not source.is_file():
        raise SystemExit(f"{slug}: frontend/index.jsx is missing")
    _validate_source_imports(root)
    esbuild = ROOT / "node_modules" / ".bin" / ("esbuild.cmd" if sys.platform.startswith("win") else "esbuild")
    if not esbuild.is_file():
        raise SystemExit("PluginBuildError: frontend build requires the bundled esbuild binary; install dependencies with npm ci")
    with tempfile.TemporaryDirectory(prefix=f"pluginctl-{slug}-") as temporary:
        output = Path(temporary) / "plugin.js"
        metafile = Path(temporary) / "meta.json"
        command = [
            str(esbuild), str(source), "--bundle", "--format=esm", "--platform=browser",
            "--jsx=automatic", "--jsx-import-source=react",
            "--external:react", "--external:react/jsx-runtime", "--external:react-dom",
            "--external:react-dom/client", "--external:react-router-dom",
            f"--metafile={metafile}", f"--outfile={output}",
        ]
        completed = subprocess.run(command, cwd=ROOT, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode:
            raise SystemExit(f"PluginBuildError: esbuild failed for {slug}: {completed.stderr.strip() or completed.stdout.strip()}")
        _validate_metafile(metafile, root)
        shutil.copyfile(output, entry)
    print(f"built {slug} -> {entry.relative_to(ROOT)}")


def package(slug: str) -> Path:
    validate(slug)
    manifest = read_manifest(slug)
    root = PLUGINS / slug
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{slug}-{manifest['version']}.ajplugin"
    files = [p for p in _runtime_files(root) if p.name != "package-index.json"]
    index = []
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        index.append({"path": relative, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    package_index = {"packageVersion": 1, "pluginId": manifest["id"], "slug": slug, "version": manifest["version"], "files": index}
    (root / "package-index.json").write_text(json.dumps(package_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = _runtime_files(root)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(root).as_posix())
    print(f"packed {target}")
    return target


def inspect(path: Path) -> None:
    from plugin_host.package import inspect_package
    result = inspect_package(path.read_bytes())
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(prog="pluginctl")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").add_argument("slug", nargs="?")
    sub.add_parser("build").add_argument("slug")
    sub.add_parser("pack").add_argument("slug")
    sub.add_parser("inspect").add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "validate": validate(args.slug)
    elif args.command == "build": build(args.slug)
    elif args.command == "pack": package(args.slug)
    else: inspect(args.path)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "backend"))
    raise SystemExit(main())
