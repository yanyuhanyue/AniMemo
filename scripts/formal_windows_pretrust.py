from __future__ import annotations

import argparse
import json
from pathlib import Path

from release.formal_windows_pretrust import (
    build_formal_windows_pretrust_kit,
    inspect_formal_windows_pretrust_in_installer_materials,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or inspect the closed Formal Windows pretrust kit"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--verifier", type=Path, required=True)
    build.add_argument("--source-initial-trust-kit", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)

    inspect = subparsers.add_parser("inspect-installer-materials")
    inspect.add_argument("--installer-materials", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "build":
        result = build_formal_windows_pretrust_kit(
            verifier=args.verifier,
            source_initial_trust_kit=args.source_initial_trust_kit,
            output=args.output,
        )
    else:
        result = inspect_formal_windows_pretrust_in_installer_materials(
            args.installer_materials
        ).as_prepublication_record()
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
