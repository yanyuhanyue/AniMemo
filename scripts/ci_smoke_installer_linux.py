#!/usr/bin/env python3
"""Build and probe Linux Installer materials in the existing isolated CI job.

This uses current source, real compiled verifiers, verified public TUF metadata,
and hash-locked Linux wheels. The platform document is explicitly a schema
fixture. No qualification producer or release publication is invoked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from release.formal_windows_pretrust import build_formal_windows_pretrust_kit
from release.materials import (
    _FIXED_DEPLOYMENT_FILES,
    build_installer_materials,
    extract_installer_materials,
)
from release.trust_bootstrap import build_initial_trust_kit
from scripts.smoke_installer_materials import smoke_installer_materials

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_FIXTURE = "scripts/tests/fixtures/installer-platform-schema.sample.json"
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
_GO_BUILD = """set -eu
test "$(go env GOVERSION)" = "go$1"
go mod download
go mod verify
export CGO_ENABLED=0 GOARCH=amd64 GOPROXY=off GOSUMDB=off
GOOS=linux go build -mod=readonly -trimpath -o /output/offline-release-verifier .
GOOS=windows go build -mod=readonly -trimpath -o /output/formal-release-verifier.exe .
"""


def environment(output: Path) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(output),
        "TMPDIR": str(output),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }


def go_build_command(root: Path, output: Path, authority: dict) -> list[str]:
    image = authority["releaseProducer"]["goBase"]
    version = authority["go"]["version"]
    if not re.fullmatch(r"golang:[a-zA-Z0-9.-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Installer smoke requires the locked Go image digest")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Installer smoke Go version is invalid")
    source = root / "release/release_attestation_verifier"
    if any("," in str(path) for path in (source, output)):
        raise ValueError("Installer smoke bind path contains a separator")
    return [
        *DOCKER, "run", "--rm", "--pull", "never", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--tmpfs", "/tmp:rw,nosuid,nodev",
        "--mount", f"type=bind,src={source},dst=/source,readonly",
        "--mount", f"type=bind,src={output},dst=/output",
        "--workdir", "/source", "--env", "GOTOOLCHAIN=local",
        "--env", "GOPATH=/output/go", "--env", "GOCACHE=/output/cache",
        image, "/bin/sh", "-c", _GO_BUILD, "installer-smoke", version,
    ]


def stage_source(root: Path, destination: Path) -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "durability", "release", "updater", "installer"],
        cwd=root,
    ).decode("utf-8").split("\0")
    for relative in sorted({*filter(None, tracked), *_FIXED_DEPLOYMENT_FILES}):
        source = root / relative
        target = destination / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Installer smoke source is not a regular file: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(root / PLATFORM_FIXTURE, destination / "release/platform-qualification.json")


def run_smoke(output: Path) -> dict[str, object]:
    authority = json.loads((ROOT / "release/producer-toolchain.lock.json").read_bytes())["byteAuthority"]
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("Installer Linux smoke requires a native Linux amd64 runner")
    if platform.python_version() != authority["python"]["hostedRuntimeVersion"]:
        raise ValueError("Installer Linux smoke requires the locked Python runtime")
    output = output.resolve()
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError("Installer smoke output must be separate from the checkout")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    clean_env = environment(output)

    def run(command: list[str]) -> None:
        subprocess.run(command, cwd=output, env=clean_env, check=True)

    build_output = output / "verifiers"
    build_output.mkdir()
    command = go_build_command(ROOT, build_output, authority)
    run([*DOCKER, "pull", authority["releaseProducer"]["goBase"]])
    run(command)
    initial = output / "initial-pretrust"
    formal = output / "formal-pretrust"
    build_initial_trust_kit(verifier=build_output / "offline-release-verifier", output=initial)
    build_formal_windows_pretrust_kit(
        verifier=build_output / "formal-release-verifier.exe",
        source_initial_trust_kit=initial, output=formal,
    )
    source = output / "source"
    stage_source(ROOT, source)
    shutil.copyfile(
        build_output / "offline-release-verifier",
        source / "release/release_attestation_verifier/offline-release-verifier",
    )
    wheelhouse = output / "wheelhouse"
    run([
        sys.executable, "-I", "-B", "-m", "pip", "download",
        "--disable-pip-version-check", "--no-cache-dir", "--only-binary=:all:",
        "--require-hashes", "--dest", str(wheelhouse),
        "-r", str(source / "release/requirements.lock"),
        "-r", str(source / "durability/requirements.lock"),
    ])
    archive = output / "installer-materials.tar"
    built = build_installer_materials(
        source, wheelhouse=wheelhouse, output=archive,
        initial_trust_kit=initial, formal_windows_pretrust_kit=formal,
    )
    # The production extractor verifies the complete tar before any runtime use.
    contract = {
        "schemaVersion": 2, "profile": "v1.1-instance-scoped", "platform": "linux/amd64",
        "archive": {"name": archive.name, "sha256": built.sha256, "size": built.size, "format": "tar"},
        "materials": [member.as_dict() for member in built.files],
    }
    materials = extract_installer_materials(archive, contract, output / "materials")
    runtime = output / "runtime"
    run([sys.executable, "-I", "-B", "-m", "venv", str(runtime)])
    python = runtime / "bin/python"
    run([
        str(python), "-I", "-B", "-m", "pip", "install",
        "--disable-pip-version-check", "--no-cache-dir", "--no-index",
        "--only-binary=:all:", "--require-hashes",
        "--find-links", str(materials.root / "wheelhouse"),
        "-r", str(materials.root / "release/requirements.lock"),
        "-r", str(materials.root / "durability/requirements.lock"),
    ])
    result = smoke_installer_materials(archive=archive, materials_root=materials.root, python=python)
    return {
        **result,
        "platform": "linux/amd64",
        "platform_qualification": "NON_AUTHORITATIVE_SCHEMA_FIXTURE",
        "platform_fixture_sha256": "sha256:" + hashlib.sha256((ROOT / PLATFORM_FIXTURE).read_bytes()).hexdigest(),
        "qualification_or_release_authority_granted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = run_smoke(arguments.output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
