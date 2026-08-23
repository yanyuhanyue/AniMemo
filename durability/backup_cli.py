"""Installed AniMemo production Backup operator command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import backup
from .instance import DEFAULT_INSTANCE_NAME, InstanceName, LocatorError

EXIT_CODES = {
    "SUCCESS": 0,
    "USAGE": 2,
    "VALIDATION": 3,
    "COMPATIBILITY": 4,
    "RECOVERY": 5,
    "ENVIRONMENT": 6,
}


class _ProtectionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        namespace.protection_kind = self.const


def _instance(value: str) -> InstanceName:
    try:
        return InstanceName(value)
    except LocatorError:
        raise argparse.ArgumentTypeError("instance name is invalid") from None


def _add_create_protection(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(
        protection_kind=None,
        one_time_key_output=None,
        passphrase_file=None,
        passphrase_fd=None,
        secret_reference_file=None,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--one-time-key-output",
        type=Path,
        action=_ProtectionAction,
        const="one-time-key",
    )
    group.add_argument(
        "--passphrase-file",
        type=Path,
        action=_ProtectionAction,
        const="passphrase-file",
    )
    group.add_argument(
        "--passphrase-fd",
        type=int,
        action=_ProtectionAction,
        const="passphrase-fd",
    )
    group.add_argument(
        "--secret-reference-file",
        type=Path,
        action=_ProtectionAction,
        const="secret-reference",
    )


def _add_verify_protection(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(
        protection_kind=None,
        one_time_key_file=None,
        passphrase_file=None,
        passphrase_fd=None,
        secret_reference_file=None,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--one-time-key-file",
        type=Path,
        action=_ProtectionAction,
        const="one-time-key",
    )
    group.add_argument(
        "--passphrase-file",
        type=Path,
        action=_ProtectionAction,
        const="passphrase-file",
    )
    group.add_argument(
        "--passphrase-fd",
        type=int,
        action=_ProtectionAction,
        const="passphrase-fd",
    )
    group.add_argument(
        "--secret-reference-file",
        type=Path,
        action=_ProtectionAction,
        const="secret-reference",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animemo",
        description="AniMemo production operator command",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser(
        "backup", help="create, verify, or inspect a production backup"
    )
    actions = backup_parser.add_subparsers(dest="backup_command", required=True)

    create = actions.add_parser(
        "create", help="create one verified Backup Format v1 artifact"
    )
    create.add_argument("--instance", type=_instance, default=DEFAULT_INSTANCE_NAME)
    create.add_argument("--destination", type=Path)
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--non-interactive", action="store_true")
    create.add_argument("--accept", action="store_true")
    create.add_argument("--json", action="store_true")
    _add_create_protection(create)

    verify = actions.add_parser(
        "verify", help="fully verify a backup and its protection"
    )
    verify.add_argument("--instance", type=_instance, default=DEFAULT_INSTANCE_NAME)
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--json", action="store_true")
    _add_verify_protection(verify)

    inspect = actions.add_parser(
        "inspect", help="inspect public manifest metadata only"
    )
    inspect.add_argument("--instance", type=_instance, default=DEFAULT_INSTANCE_NAME)
    inspect.add_argument("--backup", type=Path, required=True)
    inspect.add_argument("--json", action="store_true")
    return parser


def _protection_request(args, *, creating: bool):
    from .backup_production import ProtectionRequest

    path = None
    fd = None
    if args.protection_kind == "one-time-key":
        path = args.one_time_key_output if creating else args.one_time_key_file
    elif args.protection_kind == "passphrase-file":
        path = args.passphrase_file
    elif args.protection_kind == "passphrase-fd":
        fd = args.passphrase_fd
    elif args.protection_kind == "secret-reference":
        path = args.secret_reference_file
    return ProtectionRequest(kind=args.protection_kind, path=path, fd=fd)


def _emit(payload: object, *, as_json: bool, stream=None) -> None:
    target = stream or sys.stdout
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=target)
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            rendered = (
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
            )
            print(f"{key}: {rendered}", file=target)
        return
    print(payload, file=target)


def _run_create(args) -> int:
    from .backup_production import production_backup_runtime

    protection = _protection_request(args, creating=True)
    runtime = production_backup_runtime()
    plan = runtime.plan(
        instance_name=args.instance,
        destination=args.destination,
        protection=protection,
    )
    if args.dry_run:
        _emit(plan.as_dict(), as_json=args.json)
        return EXIT_CODES["SUCCESS"]
    if args.non_interactive and not args.accept:
        from .backup_production import ProductionBackupError

        raise ProductionBackupError("BACKUP_PLAN_ACCEPTANCE_REQUIRED", "VALIDATION")
    if not args.accept:
        response = input(f"Type BACKUP {plan.plan_digest} to accept this exact plan: ")
        if response != f"BACKUP {plan.plan_digest}":
            from .backup_production import ProductionBackupError

            raise ProductionBackupError("BACKUP_PLAN_ACCEPTANCE_REQUIRED", "VALIDATION")
    receipt = runtime.execute(
        plan,
        protection=protection,
        accepted_plan_digest=plan.plan_digest,
    )
    _emit(receipt.as_dict(), as_json=args.json)
    return EXIT_CODES["SUCCESS"]


def _run_verify(args) -> int:
    from .backup_production import verify_protected_backup

    result = verify_protected_backup(
        args.backup,
        protection=_protection_request(args, creating=False),
    )
    _require_result_instance(result, args.instance)
    _emit(result, as_json=args.json)
    return EXIT_CODES["SUCCESS"]


def _require_result_instance(result: object, instance: InstanceName | str) -> None:
    if not isinstance(result, dict):
        return
    source = result.get("sourceInstance")
    if isinstance(source, dict) and source.get("name") not in {None, str(instance)}:
        from .backup_production import ProductionBackupError

        raise ProductionBackupError("BACKUP_INSTANCE_MISMATCH", "VALIDATION")


def _run_inspect(args) -> int:
    result = backup.inspect_backup(args.backup)
    _require_result_instance(result, args.instance)
    _emit(result, as_json=args.json)
    return EXIT_CODES["SUCCESS"]


def main(argv: list[str] | None = None) -> int:
    from updater.errors import StateError

    from .backup_production import ProductionBackupError
    from .managed_config import ManagedConfigError
    from .private_store import PrivateStoreError
    from .secret_envelope import SecretEnvelopeError

    args = _parser().parse_args(argv)
    try:
        if args.backup_command == "create":
            return _run_create(args)
        if args.backup_command == "verify":
            return _run_verify(args)
        if args.backup_command == "inspect":
            return _run_inspect(args)
    except backup.UnsupportedBackupFormat as error:
        _emit(
            {"ok": False, "error": {"code": error.code}},
            as_json=True,
            stream=sys.stderr,
        )
        return EXIT_CODES["COMPATIBILITY"]
    except backup.BackupError as error:
        category = (
            "ENVIRONMENT"
            if error.code in {"BACKUP_IO_FAILED", "PG_DUMP_FAILED", "PG_DUMP_TIMEOUT"}
            else "VALIDATION"
        )
        _emit(
            {"ok": False, "error": {"code": error.code}},
            as_json=True,
            stream=sys.stderr,
        )
        return EXIT_CODES[category]
    except ProductionBackupError as error:
        _emit(
            {"ok": False, "error": {"code": error.code}},
            as_json=True,
            stream=sys.stderr,
        )
        return EXIT_CODES.get(error.category, EXIT_CODES["ENVIRONMENT"])
    except (
        LocatorError,
        ManagedConfigError,
        PrivateStoreError,
        SecretEnvelopeError,
        StateError,
        OSError,
        ValueError,
    ) as error:
        _emit(
            {
                "ok": False,
                "error": {"code": getattr(error, "code", "BACKUP_ENVIRONMENT_FAILED")},
            },
            as_json=True,
            stream=sys.stderr,
        )
        return EXIT_CODES["ENVIRONMENT"]
    raise AssertionError("argparse accepted an unknown backup command")


if __name__ == "__main__":
    raise SystemExit(main())
