#!/usr/bin/env python3
"""Export the Bridge directory as a standalone AstrBot plugin ZIP."""
from __future__ import annotations

import argparse
import os
import re
import runpy
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
OUT = ROOT / "dist"
BRIDGE_ARCHIVE_NAME = "astrbot_plugin_animemo_bridge-0.1.3.zip"


def version():
    match = re.search(r"^version:\s*(.+)$", (BRIDGE / "metadata.yaml").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else "0.1.3"


def checked_output_target(output_root, archive_name, *, create):
    try:
        metadata = output_root.lstat()
    except FileNotFoundError:
        if not create:
            raise RuntimeError("output directory must already exist") from None
        output_root.mkdir(parents=True)
        metadata = output_root.lstat()
    if output_root.is_symlink():
        raise RuntimeError("output directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("output directory must be a real directory")

    target = output_root / archive_name
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        return target
    if target.is_symlink():
        raise RuntimeError("output file must not be a symbolic link")
    if not stat.S_ISREG(target_metadata.st_mode):
        raise RuntimeError("output file must be a regular file")
    return target


def canonical_output_target():
    archive_name = f"astrbot_plugin_animemo_bridge-{version()}.zip"
    return checked_output_target(OUT, archive_name, create=True)


def runner_output_target(environ=os.environ):
    if environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("runner output is available only inside GitHub Actions")
    runner_temp_value = str(environ.get("RUNNER_TEMP") or "")
    runner_temp = Path(runner_temp_value)
    if not runner_temp_value or not runner_temp.is_absolute():
        raise RuntimeError("GitHub runner temp must be an absolute directory")
    return checked_output_target(runner_temp, BRIDGE_ARCHIVE_NAME, create=False)


def package(*, runner_output=False):
    runpy.run_path(str(ROOT / "scripts" / "validate-astrbot-bridge.py"), run_name="__bridge_validator__")["validate"]()
    target = runner_output_target() if runner_output else canonical_output_target()
    with tempfile.TemporaryDirectory(prefix="astrbot-bridge-export-") as temp:
        export_root = Path(temp) / "astrbot_plugin_animemo_bridge"
        shutil.copytree(
            BRIDGE,
            export_root,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                "tests",
                ".env",
                "*.pyc",
            ),
        )
        target.unlink(missing_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(export_root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(Path(temp)).as_posix())
    print(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help=argparse.SUPPRESS)
    args = parser.parse_args()
    runner_output = args.output is not None
    if runner_output:
        expected = os.path.join(os.environ.get("RUNNER_TEMP", ""), BRIDGE_ARCHIVE_NAME)
        if os.environ.get("GITHUB_ACTIONS") != "true" or args.output != expected:
            parser.error("--output must equal the exact GitHub runner output path")
    try:
        package(runner_output=runner_output)
    except RuntimeError as error:
        parser.exit(1, f"Bridge packaging failed: {error}\n")
