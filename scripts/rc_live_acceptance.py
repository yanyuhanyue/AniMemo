"""Validate a governed RC live-acceptance artifact for stable promotion."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.acceptance import AcceptanceError, verify_stable_promotion_acceptance


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AcceptanceError(f"expected a JSON object: {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify RC live acceptance authority")
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--stable-commit", required=True)
    parser.add_argument("--stable-api-digest", required=True)
    parser.add_argument("--stable-web-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_stable_promotion_acceptance(
            _read(args.record),
            expected=_read(args.expected),
            stable_commit=args.stable_commit,
            stable_api_digest=args.stable_api_digest,
            stable_web_digest=args.stable_web_digest,
        )
    except (OSError, json.JSONDecodeError, AcceptanceError) as error:
        print(
            json.dumps(
                {"code": "rc_live_acceptance_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
