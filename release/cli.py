from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .contract import (
    ReleaseContractError,
    build_manifest,
    build_provenance_plan,
    previous_stable_tag,
    promote_manifest,
    resolve_prerelease,
    validate_manifest,
)


def _read_tags(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseContractError(f"Expected a JSON object in {path}")
    return value


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_outputs(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    names = {
        "targetVersion": "target_version",
        "releaseTag": "release_tag",
        "previousStable": "previous_stable",
        "sequence": "sequence",
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in payload.items():
            output.write(f"{names.get(key, key)}={value}\n")


def _resolve(args) -> dict[str, object]:
    payload = resolve_prerelease(
        tags=_read_tags(args.tags_file),
        bump=args.bump,
        channel=args.channel,
        target_version_override=args.target_version_override,
    )
    _write_outputs(args.github_output, payload)
    return payload


def _generate(args) -> dict[str, object]:
    compatibility = _read_json(args.compatibility_file)
    if compatibility.get("schemaVersion") != 1:
        raise ReleaseContractError("Unsupported compatibility schemaVersion")
    database = compatibility["database"]
    configuration = compatibility["configuration"]
    plugin_sdk = compatibility["pluginSdk"]
    if plugin_sdk.get("manifestSchema") != 2 or plugin_sdk.get("runtime") != "trusted-in-process":
        raise ReleaseContractError("Compatibility policy must preserve Plugin Manifest v2 trusted in-process runtime")
    payload = build_manifest(
        version=args.version,
        channel=args.channel,
        commit=args.commit,
        created_at=args.created_at,
        api_digest=args.api_digest,
        web_digest=args.web_digest,
        minimum_updater_version=compatibility["minimumUpdaterVersion"],
        database_contract=database["contract"],
        database_accepts=database["appAccepts"],
        migration_required=database["migration"]["required"],
        migration_policy=database["migration"]["policy"],
        application_rollback=database["applicationRollback"],
        configuration_contract=configuration["contract"],
        configuration_accepts=configuration["appAccepts"],
        plugin_sdk_apis=plugin_sdk["supportedApis"],
    )
    _write_json(args.output, payload)
    return payload


def _validate(args) -> dict[str, object]:
    payload = _read_json(args.manifest)
    validate_manifest(payload, updater_version=args.updater_version or None)
    return {
        "valid": True,
        "version": payload["release"]["version"],
        "commit": payload["release"]["commit"],
        "apiDigest": payload["images"]["api"]["digest"],
        "webDigest": payload["images"]["web"]["digest"],
    }


def _generate_provenance_plan(args) -> dict[str, object]:
    payload = build_provenance_plan(
        version=args.version,
        commit=args.commit,
        created_at=args.created_at,
        api_digest=args.api_digest,
        web_digest=args.web_digest,
    )
    _write_json(args.output, payload)
    return payload


def _previous_stable(args) -> dict[str, object]:
    payload = {"previousStable": previous_stable_tag(_read_tags(args.tags_file), target=args.target) or ""}
    _write_outputs(args.github_output, payload)
    return payload


def _promote(args) -> dict[str, object]:
    payload = promote_manifest(
        _read_json(args.rc_manifest),
        existing_tags=_read_tags(args.tags_file),
        created_at=args.created_at or None,
    )
    _write_json(args.output, payload)
    return payload


def _write_checksums(args) -> dict[str, object]:
    lines = []
    for source in args.files:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        lines.append(f"{digest}  {source.name}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"checksums": str(args.output), "files": len(lines)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo release contract tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-version")
    resolve.add_argument("--tags-file", type=Path, required=True)
    resolve.add_argument("--bump", required=True)
    resolve.add_argument("--channel", required=True)
    resolve.add_argument("--target-version-override", default="")
    resolve.add_argument("--github-output", type=Path)
    resolve.set_defaults(handler=_resolve)

    previous = subparsers.add_parser("previous-stable")
    previous.add_argument("--tags-file", type=Path, required=True)
    previous.add_argument("--target", required=True)
    previous.add_argument("--github-output", type=Path)
    previous.set_defaults(handler=_previous_stable)

    generate = subparsers.add_parser("generate-manifest")
    generate.add_argument("--version", required=True)
    generate.add_argument("--channel", required=True)
    generate.add_argument("--commit", required=True)
    generate.add_argument("--created-at", required=True)
    generate.add_argument("--api-digest", required=True)
    generate.add_argument("--web-digest", required=True)
    generate.add_argument("--compatibility-file", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.set_defaults(handler=_generate)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--updater-version", default="")
    validate.set_defaults(handler=_validate)

    provenance = subparsers.add_parser("generate-provenance-plan")
    provenance.add_argument("--version", required=True)
    provenance.add_argument("--commit", required=True)
    provenance.add_argument("--created-at", required=True)
    provenance.add_argument("--api-digest", required=True)
    provenance.add_argument("--web-digest", required=True)
    provenance.add_argument("--output", type=Path, required=True)
    provenance.set_defaults(handler=_generate_provenance_plan)

    promote = subparsers.add_parser("promote-manifest")
    promote.add_argument("--rc-manifest", type=Path, required=True)
    promote.add_argument("--tags-file", type=Path, required=True)
    promote.add_argument("--created-at", default="")
    promote.add_argument("--output", type=Path, required=True)
    promote.set_defaults(handler=_promote)

    checksums = subparsers.add_parser("write-checksums")
    checksums.add_argument("--output", type=Path, required=True)
    checksums.add_argument("files", type=Path, nargs="+")
    checksums.set_defaults(handler=_write_checksums)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload = args.handler(args)
    except (ReleaseContractError, KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"code": "release_contract_invalid", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
