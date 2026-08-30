from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys

from durability.instance import DEFAULT_INSTANCE_NAME, InstanceName, LocatorError
from durability.managed_config import ListenConfig

from .errors import UpdaterError
from .public_errors import public_updater_failure
from .runtime import (
    adopt_initial_release,
    load_initial_adoption_request,
    production_runtime,
)

OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _instance(value: str) -> InstanceName:
    try:
        return InstanceName(value)
    except LocatorError:
        raise argparse.ArgumentTypeError("instance name is invalid") from None


def _operation_id(value: str) -> str:
    if not OPERATION_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("operation id must be 32 lowercase hexadecimal characters")
    return value


def _listen(value: str) -> ListenConfig:
    if not isinstance(value, str) or not value:
        raise argparse.ArgumentTypeError("listen must be ADDRESS:PORT")
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise argparse.ArgumentTypeError("listen must be ADDRESS:PORT")
        host, port_text = value[1:closing], value[closing + 2 :]
    else:
        try:
            host, port_text = value.rsplit(":", 1)
        except ValueError:
            raise argparse.ArgumentTypeError("listen must be ADDRESS:PORT") from None
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError:
        raise argparse.ArgumentTypeError("listen endpoint is invalid") from None
    if address.compressed != host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("listen endpoint is invalid")
    return ListenConfig(host, port)


def _add_configuration_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--public-origin")
    parser.add_argument("--listen", type=_listen)
    parser.add_argument("--accept-direct-exposure", action="store_true")
    parser.add_argument("--accept-insecure-http", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animemo-updater",
        description="Restricted AniMemo host Update Agent",
    )
    parser.add_argument(
        "--instance",
        type=_instance,
        default=DEFAULT_INSTANCE_NAME,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the fixed local Unix Socket")
    commands.add_parser("status", help="print observable Agent status")
    commands.add_parser("adopt-current", help="one-time exact adoption from the fixed request")
    reconcile = commands.add_parser(
        "reconcile",
        help="verify live CURRENT state and resolve one manual recovery block",
    )
    reconcile.add_argument("--operation-id", required=True, type=_operation_id)
    reconcile.add_argument("--confirmation", required=True)
    commands.add_parser("version", help="print the installed Updater version")
    config = commands.add_parser(
        "config",
        help="show, validate, plan, or atomically apply managed configuration",
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show")
    validate = config_commands.add_parser("validate")
    _add_configuration_fields(validate)
    dry_run = config_commands.add_parser("dry-run")
    _add_configuration_fields(dry_run)
    set_origin = config_commands.add_parser("set-origin")
    set_origin.add_argument("public_origin")
    set_origin.add_argument("--accept-insecure-http", action="store_true")
    set_listen = config_commands.add_parser("set-listen")
    set_listen.add_argument("listen", type=_listen)
    set_listen.add_argument("--accept-direct-exposure", action="store_true")
    apply = config_commands.add_parser("apply")
    _add_configuration_fields(apply)
    apply.add_argument(
        "--accept",
        action="store_true",
        help="accept the exact plan generated in this invocation",
    )
    return parser


def _print(payload: object, *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream or sys.stdout)


def _configuration_request(args):
    from .configuration import ConfigurationChangeRequest

    if args.config_command == "set-origin":
        return ConfigurationChangeRequest(
            public_origin=args.public_origin,
            accept_insecure_http=args.accept_insecure_http,
        )
    if args.config_command == "set-listen":
        return ConfigurationChangeRequest(
            listen=args.listen,
            accept_direct_exposure=args.accept_direct_exposure,
        )
    return ConfigurationChangeRequest(
        public_origin=args.public_origin,
        listen=args.listen,
        accept_direct_exposure=args.accept_direct_exposure,
        accept_insecure_http=args.accept_insecure_http,
    )


def _run_configuration(args) -> int:
    from installer.production import production_configuration_doctor

    from .configuration import ConfigurationError, build_configuration_manager

    manager = build_configuration_manager(
        doctor=production_configuration_doctor,
        instance_name=args.instance,
    )
    if args.config_command == "show":
        _print(manager.show())
        return 0
    request = _configuration_request(args)
    if args.config_command == "dry-run":
        _print(manager.dry_run(request))
        return 0
    plan = manager.validate(request)
    if args.config_command in {"validate", "set-origin", "set-listen"}:
        _print(plan.as_dict())
        return 0
    if not args.accept:
        raise ConfigurationError("CONFIG_PLAN_ACCEPTANCE_REQUIRED")
    result = manager.apply(plan, accepted_plan_digest=plan.plan_digest)
    _print(result.as_dict())
    if result.manual_recovery_required:
        return 5
    return 0 if result.outcome.value in {"APPLIED", "NO_CHANGE"} else 6


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        from . import __version__

        _print({"updaterVersion": __version__})
        return 0

    try:
        if args.command == "config":
            return _run_configuration(args)
        if args.command == "adopt-current":
            _print(
                adopt_initial_release(
                    load_initial_adoption_request(args.instance)
                ).as_dict()
            )
            return 0
        runtime = production_runtime(args.instance)
        if args.command == "serve":
            runtime.serve_forever()
            return 0
        if args.command == "status":
            _print(runtime.status())
            return 0
        if args.command == "reconcile":
            _print(runtime.reconcile(args.operation_id, args.confirmation))
            return 0
    except UpdaterError as error:
        _print(
            {"ok": False, "error": public_updater_failure(error.code)},
            stream=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 - CLI must never print an internal traceback
        _print(
            {"ok": False, "error": public_updater_failure("internal_error")},
            stream=sys.stderr,
        )
        return 1
    raise AssertionError("argparse accepted an unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
