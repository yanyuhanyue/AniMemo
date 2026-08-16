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


def version():
    match = re.search(r"^version:\s*(.+)$", (BRIDGE / "metadata.yaml").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else "0.1.3"


def canonical_output_target():
    try:
        metadata = OUT.lstat()
    except FileNotFoundError:
        OUT.mkdir(parents=True)
        metadata = OUT.lstat()
    if OUT.is_symlink():
        raise RuntimeError("output directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("output directory must be a real directory")

    target = OUT / f"astrbot_plugin_animemo_bridge-{version()}.zip"
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        return target
    if target.is_symlink():
        raise RuntimeError("output file must not be a symbolic link")
    if not stat.S_ISREG(target_metadata.st_mode):
        raise RuntimeError("output file must be a regular file")
    return target


def package():
    runpy.run_path(str(ROOT / "scripts" / "validate-astrbot-bridge.py"), run_name="__bridge_validator__")["validate"]()
    target = canonical_output_target()
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
    parser.parse_args()
    package()
