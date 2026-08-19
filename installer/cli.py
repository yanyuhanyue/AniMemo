"""Thin command-line Adapter for the canonical Installer Module."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import replace
from pathlib import Path

from .runtime import (
    Installer,
    InstallerError,
    InstallerMode,
    InstallRequest,
    InstallTransportSource,
    ListenRequest,
    ReleaseSelector,
    RestoreProtectionKind,
    RestoreProtectionRequest,
    explicit_transport_policy,
)

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_COMPATIBILITY = 4
EXIT_RECOVERY = 5
EXIT_ENVIRONMENT = 6


def _listen(value: str) -> ListenRequest:
    if not isinstance(value, str) or not value:
        raise argparse.ArgumentTypeError("listen must be ADDRESS:PORT")
    host: str
    port_text: str
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
        port = int(port_text)
    except ValueError:
        raise argparse.ArgumentTypeError("listen port must be an integer") from None
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("listen endpoint is invalid")
    try:
        canonical = ipaddress.ip_address(host).compressed
    except ValueError:
        raise argparse.ArgumentTypeError("listen host must be a canonical IP") from None
    if canonical != host:
        raise argparse.ArgumentTypeError("listen host must be a canonical IP")
    return ListenRequest(host=host, port=port)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="animemo-installer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, mode in (
        ("install", InstallerMode.FRESH),
        ("restore-to-new", InstallerMode.RESTORE_TO_NEW),
    ):
        child = subparsers.add_parser(command)
        child.set_defaults(mode=mode)
        selector = child.add_mutually_exclusive_group(required=True)
        selector.add_argument("--channel", choices=("stable", "rc"))
        selector.add_argument("--version")
        child.add_argument(
            "--source",
            choices=tuple(source.value for source in InstallTransportSource),
            default=InstallTransportSource.GITHUB.value,
        )
        child.add_argument("--bundle-payload", type=Path)
        child.add_argument("--release-attestation", type=Path)
        child.add_argument("--public-origin", required=True)
        child.add_argument("--listen", type=_listen, default=ListenRequest())
        child.add_argument("--accept-direct-exposure", action="store_true")
        child.add_argument("--accept-insecure-http", action="store_true")
        if mode is InstallerMode.RESTORE_TO_NEW:
            child.add_argument("--backup", type=Path, required=True)
            protection = child.add_mutually_exclusive_group(required=True)
            protection.add_argument("--protection-none", action="store_true")
            protection.add_argument("--one-time-key-file", type=Path)
            protection.add_argument("--passphrase-file", type=Path)
            protection.add_argument("--passphrase-fd", type=int)
        child.add_argument("--dry-run", action="store_true")
        child.add_argument("--non-interactive", action="store_true")
        child.add_argument(
            "--accept",
            action="store_true",
            help="explicitly accept the displayed plan in this invocation",
        )
        child.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _request(args: argparse.Namespace) -> InstallRequest:
    restore_protection = None
    if args.mode is InstallerMode.RESTORE_TO_NEW:
        if args.protection_none:
            restore_protection = RestoreProtectionRequest(RestoreProtectionKind.NONE)
        elif args.one_time_key_file is not None:
            restore_protection = RestoreProtectionRequest(
                RestoreProtectionKind.ONE_TIME_KEY_FILE,
                path=args.one_time_key_file,
            )
        elif args.passphrase_file is not None:
            restore_protection = RestoreProtectionRequest(
                RestoreProtectionKind.PASSPHRASE_FILE,
                path=args.passphrase_file,
            )
        else:
            restore_protection = RestoreProtectionRequest(
                RestoreProtectionKind.PASSPHRASE_FD,
                fd=args.passphrase_fd,
            )
    return InstallRequest(
        mode=args.mode,
        selector=ReleaseSelector(channel=args.channel, version=args.version),
        public_origin=args.public_origin,
        transport_source=InstallTransportSource(args.source),
        local_bundle_payload=(
            args.bundle_payload.absolute() if args.bundle_payload is not None else None
        ),
        local_bundle_release_attestation=(
            args.release_attestation.absolute()
            if args.release_attestation is not None
            else None
        ),
        listen=replace(
            args.listen,
            direct_exposure_accepted=args.accept_direct_exposure,
        ),
        backup_root=getattr(args, "backup", None),
        restore_protection=restore_protection,
        non_interactive=args.non_interactive,
        insecure_http_accepted=args.accept_insecure_http,
    )


def _exit_code(error: InstallerError) -> int:
    if error.outcome.value == "COMPATIBILITY_BLOCKED":
        return EXIT_COMPATIBILITY
    if error.outcome.value == "RECOVERY_REQUIRED":
        return EXIT_RECOVERY
    if error.outcome.value == "ENVIRONMENT_FAILED":
        return EXIT_ENVIRONMENT
    return EXIT_VALIDATION


def _write(value: object, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(value)


def main(argv: list[str] | None = None, *, runtime: Installer | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = _request(args)
        if runtime is None:
            from .production import build_runtime

            runtime = build_runtime(
                transport_source=request.transport_source,
                transport_policy=explicit_transport_policy(request.transport_source),
                local_bundle_payload=request.local_bundle_payload,
                local_bundle_release_attestation=(
                    request.local_bundle_release_attestation
                ),
            )
        plan = runtime.plan(request)
        if args.dry_run:
            _write(plan.as_dict(), json_output=args.json_output)
            return EXIT_SUCCESS
        if args.non_interactive and not args.accept:
            _write(
                {
                    "outcome": "VALIDATION_FAILED",
                    "reasonCode": "INSTALL_PLAN_ACCEPTANCE_REQUIRED",
                    "planDigest": plan.plan_digest,
                },
                json_output=args.json_output,
            )
            return EXIT_VALIDATION
        if not args.non_interactive and not args.accept:
            print(f"Plan digest: {plan.plan_digest}")
            answer = input("Type ACCEPT to execute this exact plan: ").strip()
            if answer != "ACCEPT":
                _write(
                    {
                        "outcome": "VALIDATION_FAILED",
                        "reasonCode": "INSTALL_PLAN_NOT_ACCEPTED",
                        "planDigest": plan.plan_digest,
                    },
                    json_output=args.json_output,
                )
                return EXIT_VALIDATION
        result = runtime.execute(plan, accepted_plan_digest=plan.plan_digest)
        _write(result.as_dict(), json_output=args.json_output)
        if result.outcome.value in {"SUCCEEDED", "NO_CHANGE"}:
            return EXIT_SUCCESS
        return {
            "RECOVERY_REQUIRED": EXIT_RECOVERY,
            "COMPATIBILITY_BLOCKED": EXIT_COMPATIBILITY,
            "ENVIRONMENT_FAILED": EXIT_ENVIRONMENT,
        }.get(result.outcome.value, EXIT_VALIDATION)
    except InstallerError as error:
        _write(
            {"outcome": error.outcome.value, "reasonCode": error.code},
            json_output=bool(getattr(args, "json_output", False)),
        )
        return _exit_code(error)
    except (OSError, EOFError, KeyboardInterrupt):
        _write(
            {"outcome": "ENVIRONMENT_FAILED", "reasonCode": "INSTALL_INPUT_FAILED"},
            json_output=bool(getattr(args, "json_output", False)),
        )
        return EXIT_ENVIRONMENT


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
