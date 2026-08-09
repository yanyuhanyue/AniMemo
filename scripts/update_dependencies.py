#!/usr/bin/env python3
"""Reproducibly update or verify the backend dependency lock file."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "backend" / "requirements.in"
LOCK = ROOT / "backend" / "requirements.txt"
_requirement = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*([<>=!~].*)?$")


def pinned_names(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = _requirement.match(line)
        if match:
            result[match.group(1).lower().replace("_", "-")] = line
    return result


def check() -> int:
    source = pinned_names(INPUT)
    locked = pinned_names(LOCK)
    missing = sorted(name for name in source if name not in locked)
    unpinned = sorted(name for name, line in locked.items() if "==" not in line and name in source)
    if missing or unpinned:
        if missing:
            print("锁文件缺少依赖：" + ", ".join(missing))
        if unpinned:
            print("锁文件包含未精确固定的依赖：" + ", ".join(unpinned))
        return 1
    print("依赖锁文件结构检查通过。")
    return 0


def update() -> int:
    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        str(INPUT),
        "--output-file",
        str(LOCK),
        "--strip-extras",
    ]
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as error:
        print("依赖更新失败：请先安装 pip-tools。", file=sys.stderr)
        return error.returncode or 1
    return check()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅检查 requirements.txt 是否覆盖并精确固定 requirements.in")
    args = parser.parse_args()
    return check() if args.check else update()


if __name__ == "__main__":
    raise SystemExit(main())
