#!/usr/bin/env python3
"""Check the actual Installer archive in an already hash-locked offline runtime.

The caller extracts the archive and installs its wheelhouse into a fresh venv.
Every product probe then runs outside the source checkout with only that material
root on PYTHONPATH. These read-only probes grant no release or platform authority.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

IMPORT_MODULES = (
    "installer.cli",
    "installer.offline_python_runtime",
    "updater.__main__",
    "updater.transport",
    "durability.backup_cli",
    "durability.doctor",
    "release.cli",
    "release.dependency_images",
    "release.mirror",
    "release.qualification_finalization",
    "release.registry_transport",
    "scripts.candidate_profile_runner",
    "scripts.formal_profile_runner",
    "scripts.closed_runtime_inventory",
)
HELP_MODULES = (
    "installer",
    "installer.offline_python_runtime",
    "updater",
    "durability.backup_cli",
    "durability.doctor",
    "release.cli",
    "release.dependency_images",
    "release.mirror",
    "release.qualification_finalization",
    "release.registry_transport",
    "scripts.candidate_profile_runner",
    "scripts.formal_profile_runner",
)

# This code is a test harness, not a product import adapter. Python resolves
# product modules normally from the sole material-root PYTHONPATH below.
_PROBE = r'''
import importlib
import json
import os
from pathlib import Path
import runpy
import sys

def read_only(event, arguments):
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")):
        raise RuntimeError("MATERIAL_SMOKE_NETWORK_OR_PROCESS_FORBIDDEN")
    if event in {
        "os.system", "os.mkdir", "os.rename", "os.remove", "os.rmdir",
        "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate", "os.utime",
    }:
        raise RuntimeError("MATERIAL_SMOKE_MUTATION_FORBIDDEN")
    if event == "open":
        flags = arguments[2]
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise RuntimeError("MATERIAL_SMOKE_WRITE_FORBIDDEN")

sys.addaudithook(read_only)
root = Path(sys.argv[1]).resolve(strict=True)
archive = Path(sys.argv[2]).resolve(strict=True)
request = json.loads(sys.argv[3])

def check_product_origins():
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in {"installer", "updater", "durability", "release", "scripts"}:
            continue
        filename = getattr(module, "__file__", None)
        if filename and not Path(filename).resolve(strict=True).is_relative_to(root):
            raise RuntimeError("MATERIAL_SMOKE_AMBIENT_PRODUCT_IMPORT: " + name)

try:
    if request["kind"] == "resources":
        for module in request["imports"]:
            importlib.import_module(module)
        from jsonschema import Draft202012Validator
        from durability.platform import parse_platform_qualification
        from release.dependency_images import load_dependency_image_authority
        from release.formal_windows_pretrust import inspect_formal_windows_pretrust_in_installer_materials
        from release.materials import VerifiedMaterialSet, inspect_installer_materials
        from release.trust_bootstrap import validate_initial_trust_kit

        identity = inspect_installer_materials(archive)
        materials = VerifiedMaterialSet(root, identity.sha256, identity.files)
        schema_count = 0
        for member in identity.files:
            resource = materials.material(member.path)
            if member.path.endswith(".schema.json"):
                Draft202012Validator.check_schema(json.loads(resource.read_text(encoding="utf-8")))
                schema_count += 1
        if not schema_count:
            raise RuntimeError("MATERIAL_SMOKE_SCHEMAS_MISSING")
        parse_platform_qualification(materials.material("release/platform-qualification.json").read_bytes())
        load_dependency_image_authority()
        validate_initial_trust_kit(root / "release/release_attestation_verifier/pretrust-v2")
        inspect_formal_windows_pretrust_in_installer_materials(archive)
        print(json.dumps({"archive_sha256": identity.sha256, "files": len(identity.files), "schemas": schema_count}))
    elif request["kind"] == "module":
        spec = importlib.util.find_spec(request["module"])
        if spec is None or not Path(spec.origin).resolve(strict=True).is_relative_to(root):
            raise RuntimeError("MATERIAL_SMOKE_ENTRYPOINT_UNAVAILABLE")
        sys.argv = [request["module"], *request["arguments"]]
        runpy.run_module(request["module"], run_name="__main__")
    elif request["kind"] == "guard-test":
        exec(request["source"])
    else:
        raise RuntimeError("MATERIAL_SMOKE_PROBE_INVALID")
finally:
    check_product_origins()
'''


class MaterialSmokeError(RuntimeError):
    pass


def _probe(
    *,
    python: Path,
    materials_root: Path,
    archive: Path,
    cwd: Path,
    request: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    environment = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR")
        if name in os.environ
    }
    environment.update(
        {
            "HOME": str(cwd),
            "TEMP": str(cwd),
            "TMP": str(cwd),
            "PYTHONPATH": str(materials_root),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return subprocess.run(
        [
            str(python), "-P", "-B", "-c", _PROBE,
            str(materials_root), str(archive), json.dumps(request),
        ],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def smoke_installer_materials(
    *, archive: Path, materials_root: Path, python: Path = Path(sys.executable)
) -> dict[str, object]:
    archive = archive.resolve(strict=True)
    materials_root = materials_root.resolve(strict=True)
    requests: list[dict[str, object]] = [
        {"kind": "resources", "imports": IMPORT_MODULES},
        *(
            {"kind": "module", "module": name, "arguments": ["--help"]}
            for name in HELP_MODULES
        ),
        {
            "kind": "module", "module": "updater.transport",
            "arguments": [], "expected_returncode": 2,
        },
        {
            "kind": "module", "module": "scripts.closed_runtime_inventory",
            "arguments": [], "expected_returncode": 64,
        },
    ]
    for profile in ("FRESH_BASE", "DOCKER_BASE", "RUNTIME_BASE_OFFLINE"):
        requests.append(
            {
                "kind": "module", "module": "scripts.candidate_profile_runner",
                "plan_only": True,
                "arguments": [
                    "--verified-candidate-digest", "sha256:" + "1" * 64,
                    "--profile", profile, "--public-origin", "https://smoke.invalid",
                ],
            }
        )
    for profile in ("FORMAL_FRESH", "FORMAL_DOCKER", "FORMAL_OFFLINE"):
        requests.append(
            {
                "kind": "module", "module": "scripts.formal_profile_runner",
                "plan_only": True,
                "arguments": ["--authority-root", str(materials_root), "--profile", profile],
            }
        )

    with tempfile.TemporaryDirectory(prefix="animemo-material-smoke-") as temporary:
        cwd = Path(temporary).resolve()
        if cwd.is_relative_to(materials_root):
            raise MaterialSmokeError("Material smoke cwd must be outside the materials")
        resource_report: dict[str, object] = {}
        for request in requests:
            completed = _probe(
                python=python, materials_root=materials_root, archive=archive,
                cwd=cwd, request=request,
            )
            if completed.returncode != request.get("expected_returncode", 0):
                raise MaterialSmokeError(
                    f"Material probe {request.get('module', 'resources')} failed "
                    f"({completed.returncode}): {completed.stderr.strip()}"
                )
            if request["kind"] == "resources":
                resource_report = json.loads(completed.stdout)
            elif request.get("plan_only"):
                plan = json.loads(completed.stdout)
                if (
                    plan.get("mode") != "PLAN_ONLY"
                    or plan.get("releaseAuthorityGranted") is not False
                    or plan.get("publishAuthorized") is not False
                ):
                    raise MaterialSmokeError("Material profile preflight granted authority")
        if list(cwd.iterdir()):
            raise MaterialSmokeError("Material probes left filesystem state")
    return {
        "status": "PASS", "authority": "NON_AUTHORITATIVE_SMOKE",
        "product_source": "INSTALLER_MATERIALS_ONLY",
        "network_and_mutations": "DENIED", "probes": len(requests), **resource_report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--materials-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = smoke_installer_materials(
            archive=arguments.archive, materials_root=arguments.materials_root
        )
    except (OSError, ValueError, subprocess.SubprocessError, MaterialSmokeError) as error:
        parser.exit(1, f"Installer material smoke failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
