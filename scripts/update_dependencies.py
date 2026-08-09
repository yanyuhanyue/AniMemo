#!/usr/bin/env python3
"""Reproducibly update or verify the backend dependency lock file."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


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


def compile_lock(input_path: Path, output_path: Path, *, upgrade: bool) -> None:
    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        str(input_path),
        "--output-file",
        str(output_path),
        "--strip-extras",
        "--no-emit-index-url",
        "--quiet",
    ]
    command.append("--upgrade" if upgrade else "--no-upgrade")
    subprocess.run(command, cwd=ROOT, check=True)


def normalized_lock(path: Path) -> str:
    """Compare pinned requirement entries, independent of pip-compile comments and paths."""
    entries = pinned_names(path)
    return "\n".join(
        f"{name} {re.sub(r'\\s+', ' ', line).strip()}"
        for name, line in sorted(entries.items())
    ) + "\n"


def locked_versions(path: Path) -> dict[str, str]:
    versions = {}
    for name, line in pinned_names(path).items():
        match = re.search(r"==\s*([^\s;]+)", line)
        if match:
            versions[name] = match.group(1)
    return versions


def validate_direct_constraints(input_path: Path, lock_path: Path) -> list[str]:
    versions = locked_versions(lock_path)
    failures = []
    for raw_line in input_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        locked = versions.get(requirement.name.lower().replace("_", "-"))
        if locked is None or not requirement.specifier.contains(Version(locked), prereleases=True):
            failures.append(f"{requirement.name} ({locked or '未锁定'}) 不满足 {requirement.specifier or '无版本约束'}")
    return failures


def check(input_path: Path = INPUT, lock_path: Path = LOCK) -> int:
    if not lock_path.exists():
        print(f"锁文件不存在：{lock_path}", file=sys.stderr)
        return 1
    constraint_failures = validate_direct_constraints(input_path, lock_path)
    if constraint_failures:
        print("直接依赖约束与锁定版本不一致：")
        print("\n".join(f"- {failure}" for failure in constraint_failures))
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="animemo-lock-") as temporary:
            generated = Path(temporary) / lock_path.name
            generated.write_text(lock_path.read_text(encoding="utf-8"), encoding="utf-8")
            compile_lock(input_path, generated, upgrade=False)
            expected = normalized_lock(lock_path)
            actual = normalized_lock(generated)
            if actual != expected:
                print("依赖锁文件与 requirements.in 重新解析结果不一致。")
                expected_entries = pinned_names(lock_path)
                actual_entries = pinned_names(generated)
                for name in sorted(set(expected_entries) - set(actual_entries)):
                    print(f"- 锁文件包含但重新解析结果缺少：{expected_entries[name]}")
                for name in sorted(set(actual_entries) - set(expected_entries)):
                    print(f"- 重新解析结果新增：{actual_entries[name]}")
                for name in sorted(set(expected_entries) & set(actual_entries)):
                    if expected_entries[name] != actual_entries[name]:
                        print(f"- 条目不一致：{expected_entries[name]} != {actual_entries[name]}")
                print("请运行：python scripts/update_dependencies.py")
                return 1
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"依赖锁文件校验失败：{error}", file=sys.stderr)
        return 1
    print("依赖锁文件重新解析并比对通过。")
    return 0


def update() -> int:
    try:
        compile_lock(INPUT, LOCK, upgrade=True)
    except subprocess.CalledProcessError as error:
        print("依赖更新失败：请安装 scripts/requirements-tools.txt 中锁定的 pip-tools。", file=sys.stderr)
        return error.returncode or 1
    return check(INPUT, LOCK)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="重新解析 requirements.in 并检查 requirements.txt 是否漂移")
    parser.add_argument("--input", type=Path, default=INPUT, help=argparse.SUPPRESS)
    parser.add_argument("--lock", type=Path, default=LOCK, help=argparse.SUPPRESS)
    args = parser.parse_args()
    return check(args.input, args.lock) if args.check else update()


if __name__ == "__main__":
    raise SystemExit(main())
