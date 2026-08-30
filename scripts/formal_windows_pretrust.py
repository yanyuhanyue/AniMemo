from __future__ import annotations

import argparse
import json

from release.formal_windows_pretrust import (
    build_formal_windows_pretrust_kit,
    hold_formal_pretrust_release_workspace,
    inspect_formal_windows_pretrust_in_installer_materials,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or inspect the closed Formal Windows pretrust kit"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("build")
    subparsers.add_parser("inspect-installer-materials")
    return parser


def main() -> int:
    args = _parser().parse_args()
    with hold_formal_pretrust_release_workspace(
        require_build_workspace=args.command == "build"
    ) as workspace:
        if args.command == "build":
            result = build_formal_windows_pretrust_kit(
                verifier=workspace.verifier,
                source_initial_trust_kit=workspace.source_initial_trust_kit,
                output=workspace.output,
            )
        else:
            result = inspect_formal_windows_pretrust_in_installer_materials(
                workspace.installer_materials
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
