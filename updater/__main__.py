from __future__ import annotations

import argparse
import json
import re
import sys

from .errors import UpdaterError
from .redaction import redact
from .runtime import production_runtime


OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _operation_id(value: str) -> str:
    if not OPERATION_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("operation id must be 32 lowercase hexadecimal characters")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animemo-updater",
        description="Restricted AniMemo host Update Agent",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the fixed local Unix Socket")
    commands.add_parser("status", help="print observable Agent status")
    commands.add_parser("import-current", help="one-time import from the fixed bootstrap manifest")
    reconcile = commands.add_parser(
        "reconcile",
        help="verify live CURRENT state and resolve one manual recovery block",
    )
    reconcile.add_argument("--operation-id", required=True, type=_operation_id)
    reconcile.add_argument("--confirmation", required=True)
    commands.add_parser("version", help="print the installed Updater version")
    return parser


def _print(payload: object, *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream or sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        from . import __version__

        _print({"updaterVersion": __version__})
        return 0

    runtime = production_runtime()
    try:
        if args.command == "serve":
            runtime.serve_forever()
            return 0
        if args.command == "status":
            _print(runtime.status())
            return 0
        if args.command == "import-current":
            _print(runtime.import_current())
            return 0
        if args.command == "reconcile":
            _print(runtime.reconcile(args.operation_id, args.confirmation))
            return 0
    except UpdaterError as error:
        _print({"ok": False, "error": {"code": error.code, "detail": redact(error)}}, stream=sys.stderr)
        return 1
    raise AssertionError("argparse accepted an unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
