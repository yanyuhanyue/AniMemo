from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

MAXIMUM_PATHS = 20_000
MAXIMUM_TOTAL_BYTES = 20 * 1024 * 1024 * 1024


def closed_runtime_total_bytes(current: int, next_file_size: int) -> int:
    if (
        type(current) is not int
        or type(next_file_size) is not int
        or current < 0
        or next_file_size < 0
        or next_file_size > MAXIMUM_TOTAL_BYTES - current
    ):
        raise ValueError("closed runtime inventory exceeds its byte ceiling")
    return current + next_file_size


def closed_runtime_inventory_digest(root: Path) -> str:
    boundary = Path(root)
    if boundary.is_symlink() or not boundary.is_dir():
        raise SystemExit(40)
    paths = sorted(boundary.rglob("*"), key=lambda item: item.as_posix())
    if not paths or len(paths) > MAXIMUM_PATHS:
        raise SystemExit(41)
    items: list[dict[str, object]] = []
    total = 0
    for path in paths:
        metadata = path.lstat()
        relative = path.relative_to(boundary).as_posix()
        if path.is_symlink():
            raise SystemExit(42)
        if path.is_dir():
            items.append({"path": relative + "/", "type": "directory"})
            continue
        if not path.is_file() or metadata.st_nlink != 1:
            raise SystemExit(43)
        try:
            total = closed_runtime_total_bytes(total, metadata.st_size)
        except ValueError:
            raise SystemExit(44)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        items.append(
            {
                "path": relative,
                "type": "file",
                "size": metadata.st_size,
                "sha256": "sha256:" + digest.hexdigest(),
            }
        )
    encoded = (
        json.dumps(
            items,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    print(closed_runtime_inventory_digest(Path(arguments[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
