#!/usr/bin/env python3
"""Export the Bridge directory as a standalone AstrBot plugin ZIP."""
from __future__ import annotations

import argparse
import re
import runpy
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
OUT = ROOT / "dist"


def version():
    match = re.search(r"^version:\s*(.+)$", (BRIDGE / "metadata.yaml").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else "0.1.1"


def package(output=None):
    runpy.run_path(str(ROOT / "scripts" / "validate-astrbot-bridge.py"), run_name="__bridge_validator__")["validate"]()
    target = Path(output) if output else OUT / f"astrbot_plugin_animemo_bridge-{version()}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="astrbot-bridge-export-") as temp:
        export_root = Path(temp) / "astrbot_plugin_animemo_bridge"
        shutil.copytree(BRIDGE, export_root, ignore=shutil.ignore_patterns("__pycache__", "tests", ".env", "*.pyc"))
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(export_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(Path(temp)).as_posix())
    print(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    package(args.output)
