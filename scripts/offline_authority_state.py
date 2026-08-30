"""Explicit maintenance commands for the offline Authority durable state."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from updater.errors import RequestRejected
from updater.offline import migrate_pristine_offline_authority_state_file_v1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式迁移 pristine 离线 Authority state v1；含历史状态必须重验原始 signed evidence"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-pristine-v1")
    migrate.add_argument("--state", type=Path, required=True)
    migrate.add_argument("--trust-material-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        state = migrate_pristine_offline_authority_state_file_v1(
            state_path=args.state,
            trust_material_root=args.trust_material_root,
        )
    except (OSError, RequestRejected, ValueError) as error:
        print(
            json.dumps(
                {
                    "code": "offline_authority_state_migration_rejected",
                    "detail": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "activeProfileIdentity": state.active_profile_identity,
                "generation": state.generation,
                "schema": "animemo.offline-authority-state-migration-receipt/v1",
                "stateSchemaVersion": state.schema_version,
                "status": "MIGRATED_PRISTINE_V1_TO_V2",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
