#!/usr/bin/env python3
"""Prove the real AstrBot loader rejects the pre-hotfix boolean schema."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "scripts" / "smoke-astrbot-bridge-loader.py"


def run_loader(runtime_root: Path, plugin_zip: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(LOADER),
            "--runtime-root",
            str(runtime_root),
            "--plugin-zip",
            str(plugin_zip),
            "--label",
            "SCHEMA GATE",
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def create_red_zip(source: Path, destination: Path) -> None:
    schema_name = "astrbot_plugin_animemo_bridge/_conf_schema.json"
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED
    ) as mutated:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == schema_name:
                schema = json.loads(content.decode("utf-8"))
                schema["enabled"]["type"] = "boolean"
                content = json.dumps(schema, ensure_ascii=False, indent=2).encode("utf-8")
            mutated.writestr(info, content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--plugin-zip", required=True, type=Path)
    args = parser.parse_args()

    green = run_loader(args.runtime_root.resolve(), args.plugin_zip.resolve())
    if green.returncode != 0:
        raise SystemExit("GREEN failed: fixed package did not pass the real AstrBot loader")

    with tempfile.TemporaryDirectory(prefix="animemo-schema-red-") as temp:
        red_zip = Path(temp) / "astrbot_plugin_animemo_bridge-red.zip"
        create_red_zip(args.plugin_zip.resolve(), red_zip)
        red = run_loader(args.runtime_root.resolve(), red_zip)
        output = red.stdout + red.stderr
        if red.returncode == 0 or "boolean" not in output:
            raise SystemExit(
                "RED failed: unsupported boolean schema was not rejected by the real AstrBot loader\n"
                f"returncode={red.returncode}"
            )

    print("RED: PASS")
    print("GREEN: PASS")


if __name__ == "__main__":
    main()
