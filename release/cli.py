from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

from updater.oci import (
    OCIContractError,
    OCIImageExpectation,
    normalize_crane_oci_layout,
    verify_oci_image_set,
)

from .acceptance import (
    AcceptanceError,
    validate_rc_live_acceptance,
    validate_stable_promotion_acceptance,
)
from .candidate import (
    CandidateContractError,
    build_candidate_input,
    decode_aggregate_receipt_b64url,
    extract_candidate_oci_archive,
    normalize_candidate_oci_layout,
    verify_prepublication_candidate,
    verify_rc14_r2_origin_from_environment,
)
from .contract import (
    ReleaseContractError,
    build_deployment_contract,
    build_manifest,
    build_provenance_plan,
    deployment_contract_digest,
    previous_stable_tag,
    promote_manifest,
    resolve_prerelease,
    validate_deployment_contract,
    validate_manifest,
)
from .materials import (
    MAX_MATERIAL_TOTAL_BYTES,
    DuplicateJsonFieldError,
    MaterialContractError,
    build_installer_materials,
    build_prepublication_material_identity,
    extract_qualification_artifact,
    read_bounded_release_file,
    reject_duplicate_json_keys,
    verify_prepublication_material_identity,
)
from .metadata_freshness import WORKFLOW_PATH as METADATA_FRESHNESS_WORKFLOW_PATH
from .metadata_freshness import (
    FreshnessExpectation,
    FreshnessRunIdentity,
    GitHubAssociatedPullSource,
    MetadataFreshnessError,
    collect_metadata_freshness,
    extract_metadata_freshness_artifact,
    validate_freshness_run_metadata,
    validate_qualification_run_metadata,
    verify_metadata_freshness_artifact,
)
from .mirror import (
    MirrorError,
    build_offline_pair_mirror_plan_from_files,
    replicate_offline_pair_files,
)
from .notes import (
    CANONICAL_RELEASE_ASSETS,
    ReleaseNotesError,
    build_release_notes,
    promote_release_notes,
    render_release_notes,
    validate_release_notes,
)
from .portable import (
    PORTABLE_IMAGE_REPOSITORIES,
    PortableBundleError,
    build_portable_payload,
    inspect_portable_archive,
    portable_release_asset_name,
    promote_portable_payload,
)
from .presentation import (
    PresentationError,
    presentation_identity_from_publication_plan,
    presentation_identity_from_stable_plan,
    verify_local_annotated_tag,
    verify_release_presentation_metadata,
    verify_stable_source_rc_presentation,
)
from .publication import (
    PublicationError,
    build_publication_plan,
    validate_publication_plan,
)
from .trust_bootstrap import TrustBootstrapError, build_initial_trust_kit

_MAX_PRESENTATION_JSON_BYTES = 1024 * 1024


class PublicationInputSnapshot:
    """Freeze each release authority input the first time it is consumed."""

    def __init__(self) -> None:
        self._values: dict[Path, bytes] = {}

    def read(self, path: Path, *, subject: str) -> bytes:
        key = Path(os.path.abspath(path))
        if key not in self._values:
            self._values[key] = read_bounded_release_file(
                key,
                subject=subject,
                maximum=MAX_MATERIAL_TOTAL_BYTES,
            )
        return self._values[key]


def _authority_bytes(
    path: Path,
    *,
    subject: str,
    snapshot: PublicationInputSnapshot | None = None,
) -> bytes:
    return (snapshot or PublicationInputSnapshot()).read(path, subject=subject)


def _read_tags(
    path: Path,
    *,
    snapshot: PublicationInputSnapshot | None = None,
) -> list[str]:
    raw = _authority_bytes(path, subject="Release tag list", snapshot=snapshot)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseContractError("Release tag list is not UTF-8") from error
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]


def _decode_json_object(value: bytes, path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (DuplicateJsonFieldError, UnicodeDecodeError) as error:
        raise ReleaseContractError(str(error)) from error
    if not isinstance(parsed, dict):
        raise ReleaseContractError(f"Expected a JSON object in {path}")
    return parsed


def _read_json(
    path: Path,
    *,
    snapshot: PublicationInputSnapshot | None = None,
) -> dict[str, object]:
    return _decode_json_object(
        _authority_bytes(path, subject="Release JSON authority", snapshot=snapshot),
        path,
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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


def _workspace_path(path: Path, *, subject: str, directory: bool = False) -> Path:
    workspace = Path.cwd().resolve(strict=True)
    candidate = Path(os.path.abspath(path))
    try:
        resolved = candidate.resolve(strict=True)
        contained = os.path.commonpath((str(workspace), str(resolved))) == str(
            workspace
        )
    except (OSError, ValueError) as error:
        raise PresentationError(f"{subject} is unavailable") from error
    if not contained or candidate.is_symlink():
        raise PresentationError(f"{subject} must be contained by the current workspace")
    if directory:
        if not resolved.is_dir():
            raise PresentationError(f"{subject} must be a directory")
    elif not resolved.is_file():
        raise PresentationError(f"{subject} must be a regular file")
    return resolved


def _read_presentation_json(
    path: Path, *, workspace_contained: bool
) -> dict[str, object]:
    source = (
        _workspace_path(path, subject="Release presentation plan")
        if workspace_contained
        else Path(os.path.abspath(path))
    )
    value = read_bounded_release_file(
        source,
        subject="Release presentation JSON",
        maximum=_MAX_PRESENTATION_JSON_BYTES,
        allow_empty=False,
    )
    return _decode_json_object(value, source)


def _write_presentation_outputs(path: Path, payload: dict[str, str]) -> None:
    required = {"release_tag", "release_title", "annotated_tag_subject"}
    if set(payload) != required or any(
        "\n" in value or "\r" in value for value in payload.values()
    ):
        raise PresentationError("Release presentation outputs are invalid")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PresentationError("GitHub output must be a single-link regular file")
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PresentationError("GitHub output changed before open")
        with os.fdopen(
            descriptor, "a", encoding="utf-8", newline="\n", closefd=False
        ) as output:
            for name in ("release_tag", "release_title", "annotated_tag_subject"):
                delimiter = f"ANIMEMO_PRESENTATION_{secrets.token_hex(16)}"
                while payload[name] == delimiter:
                    delimiter = f"ANIMEMO_PRESENTATION_{secrets.token_hex(16)}"
                output.write(f"{name}<<{delimiter}\n{payload[name]}\n{delimiter}\n")
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _presentation_plan(path: Path) -> dict[str, object]:
    return _read_presentation_json(path, workspace_contained=True)


def _presentation_identity(plan: dict[str, object]):
    validated = validate_publication_plan(plan)
    if validated["channel"] == "stable":
        return validated, presentation_identity_from_stable_plan(validated)
    return validated, presentation_identity_from_publication_plan(validated)


def _emit_presentation(args, *, stable: bool) -> dict[str, object]:
    plan = _presentation_plan(args.plan)
    identity = (
        presentation_identity_from_stable_plan(plan)
        if stable
        else presentation_identity_from_publication_plan(plan)
    )
    _write_presentation_outputs(args.github_output, identity.as_outputs())
    return identity.as_outputs()


def _emit_publication_presentation(args) -> dict[str, object]:
    return _emit_presentation(args, stable=False)


def _emit_stable_presentation(args) -> dict[str, object]:
    return _emit_presentation(args, stable=True)


def _verify_local_tag_presentation(args) -> dict[str, object]:
    validated, identity = _presentation_identity(_presentation_plan(args.plan))
    repository = _workspace_path(
        args.repository,
        subject="Local Git repository",
        directory=True,
    )
    return verify_local_annotated_tag(
        repository,
        identity=identity,
        expected_commit=validated["commit"],
    )


def _verify_release_presentation(args) -> dict[str, object]:
    plan = _presentation_plan(args.plan)
    metadata = _read_presentation_json(args.metadata, workspace_contained=False)
    repository = _workspace_path(
        args.repository,
        subject="Local Git repository",
        directory=True,
    )
    return verify_release_presentation_metadata(
        plan,
        metadata=metadata,
        repository=repository,
        state=args.state,
    )


def _verify_stable_source_presentation(args) -> dict[str, object]:
    acceptance = _read_presentation_json(args.acceptance, workspace_contained=True)
    promotion = (
        _read_presentation_json(
            args.promotion_acceptance,
            workspace_contained=True,
        )
        if args.promotion_acceptance is not None
        else None
    )
    release = _read_presentation_json(args.release, workspace_contained=False)
    repository = _workspace_path(
        args.repository,
        subject="Local Git repository",
        directory=True,
    )
    return verify_stable_source_rc_presentation(
        release=release,
        acceptance=acceptance,
        promotion_acceptance=promotion,
        repository=repository,
    )


def _resolve(args) -> dict[str, object]:
    payload = resolve_prerelease(
        tags=_read_tags(args.tags_file),
        bump=args.bump,
        channel=args.channel,
        target_version_override=args.target_version_override,
        publication_reservations=_read_json(
            args.publication_reservations_file
        ),
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
    deployment_contract = _read_json(args.deployment_contract_file)
    validate_deployment_contract(
        deployment_contract,
        root=args.deployment_root.resolve(),
        installer_materials=args.installer_materials.resolve(),
    )
    if (
        plugin_sdk.get("manifestSchema") != 2
        or plugin_sdk.get("runtime") != "trusted-in-process"
    ):
        raise ReleaseContractError(
            "Compatibility policy must preserve Plugin Manifest v2 trusted in-process runtime"
        )
    payload = build_manifest(
        version=args.version,
        channel=args.channel,
        commit=args.commit,
        created_at=args.created_at,
        api_digest=args.api_digest,
        web_digest=args.web_digest,
        deployment_contract_sha256=deployment_contract_digest(deployment_contract),
        deployment_files=deployment_contract["files"],
        minimum_updater_version=compatibility["minimumUpdaterVersion"],
        database_contract=database["contract"],
        database_accepts=database["appAccepts"],
        migration_required=database["migration"]["required"],
        migration_policy=database["migration"]["policy"],
        application_rollback=database["applicationRollback"],
        configuration_contract=configuration["contract"],
        configuration_accepts=configuration["appAccepts"],
        plugin_sdk_apis=plugin_sdk["supportedApis"],
        installer_materials_sha256=deployment_contract["archive"]["sha256"],
    )
    _write_json(args.output, payload)
    return payload


def _generate_deployment_contract(args) -> dict[str, object]:
    payload = build_deployment_contract(
        args.root, installer_materials=args.installer_materials
    )
    _write_json(args.output, payload)
    return payload


def _build_installer_materials(args) -> dict[str, object]:
    identity = build_installer_materials(
        args.root,
        wheelhouse=args.wheelhouse,
        output=args.output,
        initial_trust_kit=args.initial_trust_kit,
    )
    return {
        "archive": str(args.output),
        "sha256": identity.sha256,
        "size": identity.size,
        "files": len(identity.files),
    }


def _build_prepublication_materials(args) -> dict[str, object]:
    payload = build_prepublication_material_identity(
        installer_materials=args.installer_materials,
        deployment_contract=args.deployment_contract,
        candidate_sha=args.candidate_sha,
        candidate_tree_sha=args.candidate_tree_sha,
    )
    _write_json(args.output, payload)
    return payload


def _verify_prepublication_materials(args) -> dict[str, object]:
    return verify_prepublication_material_identity(
        _read_json(args.prepublication),
        installer_materials=args.installer_materials,
        deployment_contract=args.deployment_contract,
        expected_candidate_sha=args.expected_candidate_sha,
        expected_candidate_tree_sha=args.expected_candidate_tree_sha,
    )


def _normalize_candidate_oci_layout(args) -> dict[str, object]:
    return normalize_candidate_oci_layout(
        source_root=args.source_root,
        layout=args.layout,
        role=args.role,
        repository=args.repository,
        expected_digest=args.expected_digest,
    )


def _extract_candidate_oci_archive(args) -> dict[str, object]:
    return extract_candidate_oci_archive(
        archive=args.archive,
        destination=args.destination,
    )


def _build_prepublication_candidate_input(args) -> dict[str, object]:
    return build_candidate_input(
        root=args.root,
        qualification_run_id=args.qualification_run_id,
        qualification_run_attempt=args.qualification_run_attempt,
        source_sha=args.source_sha,
        source_tree=args.source_tree,
        artifact_ids={
            "platform_qualification": args.platform_artifact_id,
            "release_dry_run": args.dry_run_artifact_id,
        },
        artifact_api_digests={
            "platform_qualification": args.platform_artifact_digest,
            "release_dry_run": args.dry_run_artifact_digest,
        },
        generated_at=args.generated_at,
        output=args.output,
    )


def _verify_prepublication_candidate(args) -> dict[str, object]:
    return verify_prepublication_candidate(
        archive=args.archive,
        run_metadata=_read_json(args.run_metadata),
        jobs_metadata=_read_json(args.jobs_metadata),
        artifacts_metadata=_read_json(args.artifacts_metadata),
        containing_artifact_id=args.containing_artifact_id,
        containing_artifact_api_digest=args.containing_artifact_api_digest,
        expected_run_id=args.expected_run_id,
        expected_source_sha=args.expected_source_sha,
        expected_source_tree=args.expected_source_tree,
        expected_candidate_version=args.expected_candidate_version,
        verified_at=args.verified_at,
    )


def _decode_candidate_acceptance_receipt(args) -> dict[str, object]:
    receipt, encoded = decode_aggregate_receipt_b64url(args.value)
    if args.output.exists() or args.output.is_symlink():
        raise CandidateContractError("CANDIDATE_RECEIPT_OUTPUT_EXISTS")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(args.output, 0o600)
    return {
        "status": "PASS",
        "output": str(args.output),
        "qualificationRunId": receipt["qualification_run_id"],
        "candidateVersion": receipt["candidate_version"],
        "sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }


def _verify_rc14_r2_origin(args) -> dict[str, object]:
    del args
    return verify_rc14_r2_origin_from_environment()


def _extract_qualification_artifact(args) -> dict[str, object]:
    return extract_qualification_artifact(
        args.archive,
        args.destination,
        qualification_run_id=args.qualification_run_id,
        expected_sha256=args.expected_sha256,
        require_candidate_contract=args.require_candidate_contract,
    )


def _collect_metadata_freshness(args) -> dict[str, object]:
    token = os.environ.get("GITHUB_TOKEN", "")
    identity = FreshnessRunIdentity(
        workflow_run_id=args.workflow_run_id,
        workflow_attempt=args.workflow_attempt,
        workflow_path=METADATA_FRESHNESS_WORKFLOW_PATH,
        workflow_sha=args.workflow_sha,
        candidate_sha=args.candidate_sha,
        candidate_tree=args.candidate_tree,
        qualification_run_id=args.qualification_run_id,
        qualification_artifact_id=args.qualification_artifact_id,
        candidate_acceptance_receipt_sha256=(
            args.candidate_acceptance_receipt_sha256
        ),
        candidate_version=args.candidate_version,
    )
    return collect_metadata_freshness(
        repository_root=args.repository_root,
        qualification_directory=args.qualification_directory,
        output_directory=args.output_directory,
        identity=identity,
        source=GitHubAssociatedPullSource(token),
        candidate_acceptance_receipt=args.candidate_acceptance_receipt,
    )


def _extract_metadata_freshness_artifact(args) -> dict[str, object]:
    return extract_metadata_freshness_artifact(
        args.archive,
        args.destination,
        expected_sha256=args.expected_sha256,
    )


def _verify_metadata_freshness(args) -> dict[str, object]:
    return verify_metadata_freshness_artifact(
        artifact_directory=args.artifact_directory,
        qualification_directory=args.qualification_directory,
        expectation=FreshnessExpectation(
            workflow_run_id=args.expected_workflow_run_id,
            candidate_sha=args.expected_candidate_sha,
            candidate_tree=args.expected_candidate_tree,
            qualification_run_id=args.expected_qualification_run_id,
            qualification_artifact_id=args.expected_qualification_artifact_id,
            candidate_acceptance_receipt_sha256=(
                args.expected_candidate_acceptance_receipt_sha256
            ),
            candidate_version=args.expected_candidate_version,
        ),
    )


def _validate_qualification_run_metadata(args) -> dict[str, object]:
    return validate_qualification_run_metadata(
        run_metadata=_read_json(args.run_metadata),
        jobs_metadata=_read_json(args.jobs_metadata),
        artifacts_metadata=_read_json(args.artifacts_metadata),
        expected_run_id=args.expected_run_id,
        expected_sha=args.expected_sha,
    )


def _validate_freshness_run_metadata(args) -> dict[str, object]:
    return validate_freshness_run_metadata(
        run_metadata=_read_json(args.run_metadata),
        artifacts_metadata=_read_json(args.artifacts_metadata),
        expected_run_id=args.expected_run_id,
        expected_sha=args.expected_sha,
    )


def _validate(args) -> dict[str, object]:
    payload = _read_json(args.manifest)
    validate_manifest(payload, updater_version=args.updater_version or None)
    return {
        "valid": True,
        "version": payload["release"]["version"],
        "commit": payload["release"]["commit"],
        "apiDigest": payload["images"]["api"]["digest"],
        "webDigest": payload["images"]["web"]["digest"],
        "deploymentContractSha256": payload["deployment"]["contractSha256"],
    }


def _validate_deployment(args) -> dict[str, object]:
    payload = _read_json(args.contract)
    validate_deployment_contract(
        payload, installer_materials=args.installer_materials.resolve()
    )
    return {
        "profile": payload["profile"],
        "archiveSha256": payload["archive"]["sha256"],
        "materials": len(payload["materials"]),
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
    payload = {
        "previousStable": previous_stable_tag(
            _read_tags(args.tags_file), target=args.target
        )
        or ""
    }
    _write_outputs(args.github_output, payload)
    return payload


def _promote(args) -> dict[str, object]:
    payload = promote_manifest(
        _read_json(args.rc_manifest),
        existing_tags=_read_tags(args.tags_file),
        provenance_source_commit=args.provenance_source_commit,
        created_at=args.created_at or None,
    )
    _write_json(args.output, payload)
    return payload


def _write_checksums(args) -> dict[str, object]:
    lines = []
    snapshot = PublicationInputSnapshot()
    for source in args.files:
        content = snapshot.read(source, subject="Release checksum input")
        digest = hashlib.sha256(content).hexdigest()
        lines.append(f"{digest}  {source.name}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"checksums": str(args.output), "files": len(lines)}


def _portable_images(values: list[str]) -> list[dict[str, object]]:
    parsed: dict[str, dict[str, object]] = {}
    for value in values:
        role, separator, reference = value.partition("=")
        repository, at, digest = reference.rpartition("@")
        if (
            not separator
            or not at
            or role in parsed
            or role not in PORTABLE_IMAGE_REPOSITORIES
            or repository != PORTABLE_IMAGE_REPOSITORIES[role]
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise PortableBundleError("PORTABLE_IMAGE_REFERENCE_INVALID")
        parsed[role] = {
            "digest": digest,
            "layoutPath": f"oci/{role}",
            "platform": "linux/amd64",
            "repository": repository,
            "role": role,
        }
    if set(parsed) != set(PORTABLE_IMAGE_REPOSITORIES):
        raise PortableBundleError("PORTABLE_OCI_ROLES_INCOMPLETE_OR_UNORDERED")
    return [parsed[role] for role in sorted(parsed)]


def _build_portable(args) -> dict[str, object]:
    images = _portable_images(args.image)
    verify_oci_image_set(args.source_root, images)
    inspection = build_portable_payload(args.source_root, args.output, images)
    return {
        "archive": str(args.output),
        "sha256": inspection.archive_sha256,
        "files": len(inspection.files),
        "imageRoles": [item["role"] for item in images],
        "authorityState": inspection.index["authorityState"],
    }


def _normalize_oci_layout(args) -> dict[str, object]:
    return normalize_crane_oci_layout(
        args.layout,
        OCIImageExpectation(
            role=args.role,
            repository=args.repository,
            digest=args.expected_digest,
            platform=args.expected_platform,
            layout_path=f"oci/{args.role}",
        ),
        source_root=args.source_root,
    )


def _build_initial_trust_kit(args) -> dict[str, object]:
    return build_initial_trust_kit(
        verifier=args.verifier,
        output=args.output,
    )


def _promote_portable(args) -> dict[str, object]:
    inspection = promote_portable_payload(
        args.rc_payload,
        authority_directory=args.authority_directory,
        archive=args.output,
    )
    return {
        "archive": str(args.output),
        "sha256": inspection.archive_sha256,
        "files": len(inspection.files),
        "imageRoles": [item["role"] for item in inspection.index["ociImages"]],
        "authorityState": inspection.index["authorityState"],
    }


def _plan_offline_mirror(args) -> dict[str, object]:
    plan = build_offline_pair_mirror_plan_from_files(
        authority="GITHUB_RELEASE",
        repository=args.repository,
        tag=args.tag,
        commit=args.commit,
        release_identity=args.release_identity,
        payload=args.payload,
        release_attestation=args.release_attestation,
    )
    _write_json(args.output, plan)
    return plan


def _replicate_offline_mirror(args) -> dict[str, object]:
    receipt = replicate_offline_pair_files(
        _read_json(args.plan),
        source_directory=args.source_directory,
        destination_directory=args.destination_directory,
    )
    _write_json(args.output, receipt)
    return receipt


def _verify_declared_portable(args) -> dict[str, object]:
    plan = validate_publication_plan(_read_json(args.plan))
    transport = plan.get("transport_assets")
    if not isinstance(transport, dict) or len(transport) != 1:
        raise PublicationError("publication plan has no single declared portable asset")
    name, identity = next(iter(transport.items()))
    if args.payload.name != name:
        raise PublicationError("portable payload name differs from publication plan")
    inspection = inspect_portable_archive(args.payload)
    if (
        inspection.archive_sha256 != identity["sha256"]
        or inspection.archive_size != identity["size"]
    ):
        raise PublicationError("portable payload identity differs from publication plan")
    return {
        "status": "PASS",
        "publicationPlanIdentity": plan["identity"],
        "name": name,
        "sha256": inspection.archive_sha256,
        "size": identity["size"],
        "authorityRole": "TRANSPORT_ONLY",
    }


def _generate_release_notes(args) -> dict[str, object]:
    source = _read_json(args.input)
    if set(source) != {"context", "pulls"}:
        raise ReleaseNotesError("release note input has unknown or missing fields")
    artifact = build_release_notes(context=source["context"], pulls=source["pulls"])
    markdown = render_release_notes(artifact)
    _write_json(args.output_json, artifact)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(markdown, encoding="utf-8", newline="\n")
    return artifact


def _plan_publication(args) -> dict[str, object]:
    source = _read_json(args.input)
    expected = {
        "repository",
        "channel",
        "tag",
        "commit",
        "qualification_identity",
        "release_notes_identity",
        "release_notes_markdown_sha256",
        "assets",
        "transport_assets",
        "api_digest",
        "web_digest",
    }
    if set(source) not in {frozenset(expected), frozenset(expected - {"transport_assets"})}:
        raise PublicationError("publication plan input has unknown or missing fields")
    plan = build_publication_plan(**source)
    _write_json(args.output, plan)
    return plan


def _plan_publication_files(args) -> dict[str, object]:
    snapshot = PublicationInputSnapshot()
    qualification = _read_json(args.qualification, snapshot=snapshot)
    notes = validate_release_notes(_read_json(args.release_notes, snapshot=snapshot))
    markdown = snapshot.read(
        args.release_notes_markdown,
        subject="Release Notes markdown",
    )
    markdown_sha256 = "sha256:" + hashlib.sha256(markdown).hexdigest()
    binding = qualification.get("release_notes")
    if not isinstance(binding, dict) or binding != {
        "snapshot_identity": notes["identity"],
        "markdown_sha256": markdown_sha256,
    }:
        raise PublicationError("qualification does not bind the supplied Release Notes")
    if (
        qualification.get("candidate_sha") != args.commit
        or qualification.get("channel") != args.channel
        or qualification.get("release_tag") != args.tag
        or notes["context"]["candidate_sha"] != args.commit
        or notes["context"]["channel"] != args.channel
        or notes["context"]["release_tag"] != args.tag
    ):
        raise PublicationError(
            "qualification, Release Notes, and publication identity tuple differ"
        )
    qualification_identity = qualification.get("artifact_sha256")
    assets = {}
    for name in CANONICAL_RELEASE_ASSETS:
        path = args.asset_directory / name
        content = snapshot.read(path, subject=f"Canonical release asset {name}")
        assets[name] = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    portable_name = portable_release_asset_name(args.tag)
    if args.portable.name != portable_name:
        raise PublicationError("portable transport asset name does not match release tag")
    portable_inspection = inspect_portable_archive(args.portable)
    transport_assets = {
        portable_name: {
            "role": "PORTABLE_RELEASE_BUNDLE",
            "sha256": portable_inspection.archive_sha256,
            "size": portable_inspection.archive_size,
        }
    }
    plan = build_publication_plan(
        repository=args.repository,
        channel=args.channel,
        tag=args.tag,
        commit=args.commit,
        qualification_identity=qualification_identity,
        release_notes_identity=notes["identity"],
        release_notes_markdown_sha256=markdown_sha256,
        assets=assets,
        transport_assets=transport_assets,
        api_digest=args.api_digest,
        web_digest=args.web_digest,
    )
    _write_json(args.output, plan)
    return plan


def _promote_release_notes(args) -> dict[str, object]:
    snapshot = promote_release_notes(_read_json(args.rc_notes), stable_tag=args.stable_tag)
    markdown = render_release_notes(snapshot)
    _write_json(args.output_json, snapshot)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text(markdown, encoding="utf-8", newline="\n")
    return snapshot


def _validate_stable_publication_authority_inputs(
    args,
    snapshot: PublicationInputSnapshot | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    snapshot = snapshot or PublicationInputSnapshot()
    acceptance = validate_rc_live_acceptance(
        _read_json(args.acceptance, snapshot=snapshot)
    )
    promotion = validate_stable_promotion_acceptance(
        _read_json(args.promotion_acceptance, snapshot=snapshot),
        acceptance=acceptance,
    )
    rc_manifest_bytes = snapshot.read(args.rc_manifest, subject="Accepted RC manifest")
    if (
        "sha256:" + hashlib.sha256(rc_manifest_bytes).hexdigest()
        != acceptance["release_manifest_identity"]
    ):
        raise PublicationError("RC manifest differs from the live acceptance record")
    rc_manifest = _decode_json_object(rc_manifest_bytes, args.rc_manifest)
    stable_manifest = _read_json(
        args.asset_directory / "release-manifest.json",
        snapshot=snapshot,
    )
    validate_manifest(rc_manifest)
    validate_manifest(stable_manifest)
    try:
        expected_stable_manifest = promote_manifest(
            rc_manifest,
            existing_tags=[],
            provenance_source_commit=stable_manifest["provenance"]["sourceCommit"],
            created_at=stable_manifest["release"]["createdAt"],
        )
    except (KeyError, TypeError) as error:
        raise PublicationError("Stable manifest authority fields are incomplete") from error
    if stable_manifest != expected_stable_manifest or stable_manifest["release"]["version"] != args.tag:
        raise PublicationError("Stable manifest does not derive exactly from the accepted RC")
    if (
        stable_manifest["release"]["commit"] != promotion["stable_commit"]
        or stable_manifest["images"]["api"]["digest"]
        != promotion["stable_api_digest"]
        or stable_manifest["images"]["web"]["digest"]
        != promotion["stable_web_digest"]
    ):
        raise PublicationError("Stable manifest differs from promotion acceptance")
    for name, expected in (
        ("deployment-contract.json", acceptance["deployment_contract_identity"]),
        ("installer-materials.tar", acceptance["installer_materials_identity"]),
    ):
        content = snapshot.read(
            args.asset_directory / name,
            subject=f"Stable immutable material {name}",
        )
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise PublicationError(
                "Stable immutable materials differ from the accepted RC"
            )
    return acceptance, promotion


def _plan_stable_publication_files(args) -> dict[str, object]:
    snapshot = PublicationInputSnapshot()
    _acceptance, promotion = _validate_stable_publication_authority_inputs(
        args,
        snapshot,
    )
    rc_notes = validate_release_notes(_read_json(args.rc_notes, snapshot=snapshot))
    stable_notes = validate_release_notes(
        _read_json(args.stable_notes, snapshot=snapshot)
    )
    expected_stable = promote_release_notes(rc_notes, stable_tag=args.tag)
    if stable_notes != expected_stable:
        raise PublicationError("Stable Release Notes do not derive from the frozen RC population")
    markdown = snapshot.read(
        args.stable_notes_markdown,
        subject="Stable Release Notes markdown",
    )
    markdown_sha256 = "sha256:" + hashlib.sha256(markdown).hexdigest()
    assets = {}
    for name in CANONICAL_RELEASE_ASSETS:
        content = snapshot.read(
            args.asset_directory / name,
            subject=f"Canonical stable release asset {name}",
        )
        assets[name] = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    portable_name = portable_release_asset_name(args.tag)
    if args.portable.name != portable_name:
        raise PublicationError("portable transport asset name does not match release tag")
    portable_inspection = inspect_portable_archive(args.portable)
    transport_assets = {
        portable_name: {
            "role": "PORTABLE_RELEASE_BUNDLE",
            "sha256": portable_inspection.archive_sha256,
            "size": portable_inspection.archive_size,
        }
    }
    plan = build_publication_plan(
        repository=args.repository,
        channel="stable",
        tag=args.tag,
        commit=promotion["stable_commit"],
        qualification_identity=promotion["identity"],
        release_notes_identity=stable_notes["identity"],
        release_notes_markdown_sha256=markdown_sha256,
        assets=assets,
        transport_assets=transport_assets,
        api_digest=promotion["stable_api_digest"],
        web_digest=promotion["stable_web_digest"],
    )
    _write_json(args.output, plan)
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo release contract tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-version")
    resolve.add_argument("--tags-file", type=Path, required=True)
    resolve.add_argument(
        "--publication-reservations-file", type=Path, required=True
    )
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
    generate.add_argument("--deployment-contract-file", type=Path, required=True)
    generate.add_argument("--deployment-root", type=Path, required=True)
    generate.add_argument("--installer-materials", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.set_defaults(handler=_generate)

    deployment = subparsers.add_parser("generate-deployment-contract")
    deployment.add_argument("--root", type=Path, required=True)
    deployment.add_argument("--installer-materials", type=Path, required=True)
    deployment.add_argument("--output", type=Path, required=True)
    deployment.set_defaults(handler=_generate_deployment_contract)

    materials = subparsers.add_parser("build-installer-materials")
    materials.add_argument("--root", type=Path, required=True)
    materials.add_argument("--wheelhouse", type=Path, required=True)
    materials.add_argument("--output", type=Path, required=True)
    materials.add_argument("--initial-trust-kit", type=Path, required=True)
    materials.set_defaults(handler=_build_installer_materials)

    prepublication = subparsers.add_parser("build-prepublication-materials")
    prepublication.add_argument("--installer-materials", type=Path, required=True)
    prepublication.add_argument("--deployment-contract", type=Path, required=True)
    prepublication.add_argument("--candidate-sha", required=True)
    prepublication.add_argument("--candidate-tree-sha", required=True)
    prepublication.add_argument("--output", type=Path, required=True)
    prepublication.set_defaults(handler=_build_prepublication_materials)

    verify_prepublication = subparsers.add_parser(
        "verify-prepublication-materials"
    )
    verify_prepublication.add_argument("--prepublication", type=Path, required=True)
    verify_prepublication.add_argument(
        "--installer-materials", type=Path, required=True
    )
    verify_prepublication.add_argument(
        "--deployment-contract", type=Path, required=True
    )
    verify_prepublication.add_argument("--expected-candidate-sha", required=True)
    verify_prepublication.add_argument(
        "--expected-candidate-tree-sha", required=True
    )
    verify_prepublication.set_defaults(handler=_verify_prepublication_materials)

    candidate_oci = subparsers.add_parser("normalize-candidate-oci-layout")
    candidate_oci.add_argument("--source-root", type=Path, required=True)
    candidate_oci.add_argument("--layout", type=Path, required=True)
    candidate_oci.add_argument("--role", required=True)
    candidate_oci.add_argument("--repository", required=True)
    candidate_oci.add_argument("--expected-digest", required=True)
    candidate_oci.set_defaults(handler=_normalize_candidate_oci_layout)

    candidate_oci_extract = subparsers.add_parser("extract-candidate-oci-archive")
    candidate_oci_extract.add_argument("--archive", type=Path, required=True)
    candidate_oci_extract.add_argument("--destination", type=Path, required=True)
    candidate_oci_extract.set_defaults(handler=_extract_candidate_oci_archive)

    candidate_input = subparsers.add_parser(
        "build-prepublication-candidate-input"
    )
    candidate_input.add_argument("--root", type=Path, required=True)
    candidate_input.add_argument("--qualification-run-id", type=int, required=True)
    candidate_input.add_argument(
        "--qualification-run-attempt", type=int, required=True
    )
    candidate_input.add_argument("--source-sha", required=True)
    candidate_input.add_argument("--source-tree", required=True)
    candidate_input.add_argument("--platform-artifact-id", type=int, required=True)
    candidate_input.add_argument("--platform-artifact-digest", required=True)
    candidate_input.add_argument("--dry-run-artifact-id", type=int, required=True)
    candidate_input.add_argument("--dry-run-artifact-digest", required=True)
    candidate_input.add_argument("--generated-at", required=True)
    candidate_input.add_argument("--output", type=Path, required=True)
    candidate_input.set_defaults(handler=_build_prepublication_candidate_input)

    candidate_verify = subparsers.add_parser("verify-prepublication-candidate")
    candidate_verify.add_argument("--archive", type=Path, required=True)
    candidate_verify.add_argument("--run-metadata", type=Path, required=True)
    candidate_verify.add_argument("--jobs-metadata", type=Path, required=True)
    candidate_verify.add_argument("--artifacts-metadata", type=Path, required=True)
    candidate_verify.add_argument("--containing-artifact-id", type=int, required=True)
    candidate_verify.add_argument(
        "--containing-artifact-api-digest", required=True
    )
    candidate_verify.add_argument("--expected-run-id", type=int, required=True)
    candidate_verify.add_argument("--expected-source-sha", required=True)
    candidate_verify.add_argument("--expected-source-tree", required=True)
    candidate_verify.add_argument("--expected-candidate-version", required=True)
    candidate_verify.add_argument("--verified-at", required=True)
    candidate_verify.set_defaults(handler=_verify_prepublication_candidate)

    candidate_receipt = subparsers.add_parser(
        "decode-candidate-acceptance-receipt"
    )
    candidate_receipt.add_argument("--value", required=True)
    candidate_receipt.add_argument("--output", type=Path, required=True)
    candidate_receipt.set_defaults(handler=_decode_candidate_acceptance_receipt)

    r2_precheck = subparsers.add_parser("verify-rc14-r2-origin-empty")
    r2_precheck.set_defaults(handler=_verify_rc14_r2_origin)

    qualification_artifact = subparsers.add_parser(
        "extract-qualification-artifact"
    )
    qualification_artifact.add_argument("--archive", type=Path, required=True)
    qualification_artifact.add_argument("--destination", type=Path, required=True)
    qualification_artifact.add_argument(
        "--qualification-run-id", type=int, required=True
    )
    qualification_artifact.add_argument("--expected-sha256", required=True)
    qualification_artifact.add_argument(
        "--require-candidate-contract", action="store_true"
    )
    qualification_artifact.set_defaults(handler=_extract_qualification_artifact)

    freshness_collection = subparsers.add_parser("collect-metadata-freshness")
    freshness_collection.add_argument("--repository-root", type=Path, required=True)
    freshness_collection.add_argument(
        "--qualification-directory", type=Path, required=True
    )
    freshness_collection.add_argument("--output-directory", type=Path, required=True)
    freshness_collection.add_argument("--workflow-run-id", type=int, required=True)
    freshness_collection.add_argument("--workflow-attempt", type=int, required=True)
    freshness_collection.add_argument("--workflow-sha", required=True)
    freshness_collection.add_argument("--candidate-sha", required=True)
    freshness_collection.add_argument("--candidate-tree", required=True)
    freshness_collection.add_argument(
        "--qualification-run-id", type=int, required=True
    )
    freshness_collection.add_argument(
        "--qualification-artifact-id", type=int, required=True
    )
    freshness_collection.add_argument(
        "--candidate-acceptance-receipt", type=Path, required=True
    )
    freshness_collection.add_argument(
        "--candidate-acceptance-receipt-sha256", required=True
    )
    freshness_collection.add_argument("--candidate-version", required=True)
    freshness_collection.set_defaults(handler=_collect_metadata_freshness)

    freshness_artifact = subparsers.add_parser(
        "extract-metadata-freshness-artifact"
    )
    freshness_artifact.add_argument("--archive", type=Path, required=True)
    freshness_artifact.add_argument("--destination", type=Path, required=True)
    freshness_artifact.add_argument("--expected-sha256", required=True)
    freshness_artifact.set_defaults(handler=_extract_metadata_freshness_artifact)

    freshness_verify = subparsers.add_parser("verify-metadata-freshness")
    freshness_verify.add_argument(
        "--artifact-directory", type=Path, required=True
    )
    freshness_verify.add_argument(
        "--qualification-directory", type=Path, required=True
    )
    freshness_verify.add_argument(
        "--expected-workflow-run-id", type=int, required=True
    )
    freshness_verify.add_argument("--expected-candidate-sha", required=True)
    freshness_verify.add_argument("--expected-candidate-tree", required=True)
    freshness_verify.add_argument(
        "--expected-qualification-run-id", type=int, required=True
    )
    freshness_verify.add_argument(
        "--expected-qualification-artifact-id", type=int, required=True
    )
    freshness_verify.add_argument(
        "--expected-candidate-acceptance-receipt-sha256", required=True
    )
    freshness_verify.add_argument("--expected-candidate-version", required=True)
    freshness_verify.set_defaults(handler=_verify_metadata_freshness)

    qualification_metadata = subparsers.add_parser(
        "validate-qualification-run-metadata"
    )
    qualification_metadata.add_argument("--run-metadata", type=Path, required=True)
    qualification_metadata.add_argument("--jobs-metadata", type=Path, required=True)
    qualification_metadata.add_argument(
        "--artifacts-metadata", type=Path, required=True
    )
    qualification_metadata.add_argument("--expected-run-id", type=int, required=True)
    qualification_metadata.add_argument("--expected-sha", required=True)
    qualification_metadata.set_defaults(
        handler=_validate_qualification_run_metadata
    )

    freshness_metadata = subparsers.add_parser("validate-freshness-run-metadata")
    freshness_metadata.add_argument("--run-metadata", type=Path, required=True)
    freshness_metadata.add_argument(
        "--artifacts-metadata", type=Path, required=True
    )
    freshness_metadata.add_argument("--expected-run-id", type=int, required=True)
    freshness_metadata.add_argument("--expected-sha", required=True)
    freshness_metadata.set_defaults(handler=_validate_freshness_run_metadata)

    trust_bootstrap = subparsers.add_parser("build-initial-trust-kit")
    trust_bootstrap.add_argument("--verifier", type=Path, required=True)
    trust_bootstrap.add_argument("--output", type=Path, required=True)
    trust_bootstrap.set_defaults(handler=_build_initial_trust_kit)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--updater-version", default="")
    validate.set_defaults(handler=_validate)

    validate_deployment = subparsers.add_parser("validate-deployment-contract")
    validate_deployment.add_argument("--contract", type=Path, required=True)
    validate_deployment.add_argument("--installer-materials", type=Path, required=True)
    validate_deployment.set_defaults(handler=_validate_deployment)

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
    promote.add_argument("--provenance-source-commit", required=True)
    promote.add_argument("--created-at", default="")
    promote.add_argument("--output", type=Path, required=True)
    promote.set_defaults(handler=_promote)

    checksums = subparsers.add_parser("write-checksums")
    checksums.add_argument("--output", type=Path, required=True)
    checksums.add_argument("files", type=Path, nargs="+")
    checksums.set_defaults(handler=_write_checksums)

    portable = subparsers.add_parser("build-portable")
    portable.add_argument("--source-root", type=Path, required=True)
    portable.add_argument("--output", type=Path, required=True)
    portable.add_argument("--image", action="append", default=[], required=True)
    portable.set_defaults(handler=_build_portable)

    normalize_oci = subparsers.add_parser("normalize-oci-layout")
    normalize_oci.add_argument("--source-root", type=Path, required=True)
    normalize_oci.add_argument("--layout", type=Path, required=True)
    normalize_oci.add_argument("--role", required=True)
    normalize_oci.add_argument("--repository", required=True)
    normalize_oci.add_argument("--expected-digest", required=True)
    normalize_oci.add_argument("--expected-platform", required=True)
    normalize_oci.set_defaults(handler=_normalize_oci_layout)

    portable_promotion = subparsers.add_parser("promote-portable")
    portable_promotion.add_argument("--rc-payload", type=Path, required=True)
    portable_promotion.add_argument("--authority-directory", type=Path, required=True)
    portable_promotion.add_argument("--output", type=Path, required=True)
    portable_promotion.set_defaults(handler=_promote_portable)

    mirror_plan = subparsers.add_parser("plan-offline-mirror")
    mirror_plan.add_argument("--repository", required=True)
    mirror_plan.add_argument("--tag", required=True)
    mirror_plan.add_argument("--commit", required=True)
    mirror_plan.add_argument("--release-identity", required=True)
    mirror_plan.add_argument("--payload", type=Path, required=True)
    mirror_plan.add_argument("--release-attestation", type=Path, required=True)
    mirror_plan.add_argument("--output", type=Path, required=True)
    mirror_plan.set_defaults(handler=_plan_offline_mirror)

    mirror_copy = subparsers.add_parser("replicate-offline-mirror")
    mirror_copy.add_argument("--plan", type=Path, required=True)
    mirror_copy.add_argument("--source-directory", type=Path, required=True)
    mirror_copy.add_argument("--destination-directory", type=Path, required=True)
    mirror_copy.add_argument("--output", type=Path, required=True)
    mirror_copy.set_defaults(handler=_replicate_offline_mirror)

    portable_verify = subparsers.add_parser("verify-declared-portable")
    portable_verify.add_argument("--plan", type=Path, required=True)
    portable_verify.add_argument("--payload", type=Path, required=True)
    portable_verify.set_defaults(handler=_verify_declared_portable)

    notes = subparsers.add_parser("generate-release-notes")
    notes.add_argument("--input", type=Path, required=True)
    notes.add_argument("--output-json", type=Path, required=True)
    notes.add_argument("--output-markdown", type=Path, required=True)
    notes.set_defaults(handler=_generate_release_notes)

    publication = subparsers.add_parser("plan-publication")
    publication.add_argument("--input", type=Path, required=True)
    publication.add_argument("--output", type=Path, required=True)
    publication.set_defaults(handler=_plan_publication)

    publication_files = subparsers.add_parser("plan-publication-files")
    publication_files.add_argument("--repository", required=True)
    publication_files.add_argument("--channel", required=True)
    publication_files.add_argument("--tag", required=True)
    publication_files.add_argument("--commit", required=True)
    publication_files.add_argument("--qualification", type=Path, required=True)
    publication_files.add_argument("--release-notes", type=Path, required=True)
    publication_files.add_argument("--release-notes-markdown", type=Path, required=True)
    publication_files.add_argument("--asset-directory", type=Path, required=True)
    publication_files.add_argument("--portable", type=Path, required=True)
    publication_files.add_argument("--api-digest", required=True)
    publication_files.add_argument("--web-digest", required=True)
    publication_files.add_argument("--output", type=Path, required=True)
    publication_files.set_defaults(handler=_plan_publication_files)

    stable_notes = subparsers.add_parser("promote-release-notes")
    stable_notes.add_argument("--rc-notes", type=Path, required=True)
    stable_notes.add_argument("--stable-tag", required=True)
    stable_notes.add_argument("--output-json", type=Path, required=True)
    stable_notes.add_argument("--output-markdown", type=Path, required=True)
    stable_notes.set_defaults(handler=_promote_release_notes)

    stable_publication = subparsers.add_parser("plan-stable-publication-files")
    stable_publication.add_argument("--repository", required=True)
    stable_publication.add_argument("--tag", required=True)
    stable_publication.add_argument("--acceptance", type=Path, required=True)
    stable_publication.add_argument(
        "--promotion-acceptance", type=Path, required=True
    )
    stable_publication.add_argument("--rc-manifest", type=Path, required=True)
    stable_publication.add_argument("--rc-notes", type=Path, required=True)
    stable_publication.add_argument("--stable-notes", type=Path, required=True)
    stable_publication.add_argument("--stable-notes-markdown", type=Path, required=True)
    stable_publication.add_argument("--asset-directory", type=Path, required=True)
    stable_publication.add_argument("--portable", type=Path, required=True)
    stable_publication.add_argument("--output", type=Path, required=True)
    stable_publication.set_defaults(handler=_plan_stable_publication_files)
    publication_presentation = subparsers.add_parser("emit-publication-presentation")
    publication_presentation.add_argument("--plan", type=Path, required=True)
    publication_presentation.add_argument("--github-output", type=Path, required=True)
    publication_presentation.set_defaults(handler=_emit_publication_presentation)

    stable_presentation = subparsers.add_parser("emit-stable-presentation")
    stable_presentation.add_argument("--plan", type=Path, required=True)
    stable_presentation.add_argument("--github-output", type=Path, required=True)
    stable_presentation.set_defaults(handler=_emit_stable_presentation)

    local_tag_presentation = subparsers.add_parser("verify-local-tag-presentation")
    local_tag_presentation.add_argument("--plan", type=Path, required=True)
    local_tag_presentation.add_argument("--repository", type=Path, default=Path("."))
    local_tag_presentation.set_defaults(handler=_verify_local_tag_presentation)

    release_presentation = subparsers.add_parser("verify-release-presentation")
    release_presentation.add_argument("--plan", type=Path, required=True)
    release_presentation.add_argument("--metadata", type=Path, required=True)
    release_presentation.add_argument("--repository", type=Path, default=Path("."))
    release_presentation.add_argument(
        "--state", choices=("draft", "published"), required=True
    )
    release_presentation.set_defaults(handler=_verify_release_presentation)

    stable_source_presentation = subparsers.add_parser(
        "verify-stable-source-presentation"
    )
    stable_source_presentation.add_argument("--acceptance", type=Path, required=True)
    stable_source_presentation.add_argument("--promotion-acceptance", type=Path)
    stable_source_presentation.add_argument("--release", type=Path, required=True)
    stable_source_presentation.add_argument(
        "--repository", type=Path, default=Path(".")
    )
    stable_source_presentation.set_defaults(handler=_verify_stable_source_presentation)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload = args.handler(args)
    except PresentationError as error:
        print(
            json.dumps(
                {"code": error.code, "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except MetadataFreshnessError as error:
        print(
            json.dumps(
                {"code": error.code, "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except CandidateContractError as error:
        print(
            json.dumps(
                {"code": error.code, "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (
        ReleaseContractError,
        AcceptanceError,
        ReleaseNotesError,
        PublicationError,
        PortableBundleError,
        OCIContractError,
        MirrorError,
        TrustBootstrapError,
        MaterialContractError,
        KeyError,
        TypeError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(
            json.dumps(
                {"code": "release_contract_invalid", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
