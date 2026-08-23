"""Single-source Release presentation identity and pre-mutation guards."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .acceptance import (
    AcceptanceError,
    validate_rc_live_acceptance,
    validate_stable_promotion_acceptance,
)

_TAG = re.compile(
    r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:beta|rc)\.[1-9][0-9]*)?",
    re.ASCII,
)


class PresentationError(ValueError):
    """Release presentation authority cannot be preserved."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "RELEASE_PRESENTATION_AUTHORITY_INVALID",
    ) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ReleasePresentationIdentity:
    """The complete presentation identity consumed by RC and Stable workflows."""

    release_tag: str
    release_title: str
    annotated_tag_subject: str

    def as_outputs(self) -> dict[str, str]:
        return asdict(self)


def validate_release_presentation_identity(
    identity: ReleasePresentationIdentity | Mapping[str, Any],
) -> ReleasePresentationIdentity:
    """Validate a closed, canonical, tag-only presentation identity."""

    if isinstance(identity, Mapping):
        required = {"release_tag", "release_title", "annotated_tag_subject"}
        if set(identity) != required or not all(
            isinstance(identity[field], str) for field in required
        ):
            raise PresentationError("release presentation identity is not closed")
        identity = ReleasePresentationIdentity(**dict(identity))
    if not isinstance(identity, ReleasePresentationIdentity):
        raise PresentationError("release presentation identity is invalid")
    if not _TAG.fullmatch(identity.release_tag):
        raise PresentationError("release presentation tag is not canonical ASCII")
    if identity.release_title != identity.release_tag:
        raise PresentationError("release title must equal release tag")
    if identity.annotated_tag_subject != identity.release_tag:
        raise PresentationError("annotated tag subject must equal release tag")
    return identity


def _project_validated_plan(
    plan: Mapping[str, Any], *, require_stable: bool | None
) -> tuple[dict[str, Any], ReleasePresentationIdentity]:
    # The import is local so publication plan construction may remain independent
    # while both RC and Stable callers share this single projection seam.
    from .publication import validate_publication_plan

    validated = validate_publication_plan(plan)
    is_stable = validated["channel"] == "stable"
    if require_stable is not None and is_stable is not require_stable:
        expected = "Stable" if require_stable else "prerelease"
        raise PresentationError(f"presentation plan must be {expected}")
    tag = validated["tag"]
    identity = validate_release_presentation_identity(
        ReleasePresentationIdentity(
            release_tag=tag,
            release_title=tag,
            annotated_tag_subject=tag,
        )
    )
    commands = validated["commands"]
    expected_tag = [
        "git",
        "tag",
        "--annotate",
        tag,
        validated["commit"],
        "--message",
        identity.annotated_tag_subject,
    ]
    if commands.get("create_tag") != expected_tag:
        raise PresentationError(
            "publication plan tag presentation differs from identity"
        )
    create_draft = commands.get("create_draft")
    if not isinstance(create_draft, list):
        raise PresentationError("publication plan Draft command is invalid")
    try:
        title_index = create_draft.index("--title")
    except ValueError as error:
        raise PresentationError("publication plan Draft title is missing") from error
    if (
        title_index + 1 >= len(create_draft)
        or create_draft[title_index + 1] != identity.release_title
    ):
        raise PresentationError("publication plan Draft title differs from identity")
    return validated, identity


def presentation_identity_from_publication_plan(
    plan: Mapping[str, Any],
) -> ReleasePresentationIdentity:
    """Project a validated RC/Beta plan without executing its command arrays."""

    return _project_validated_plan(plan, require_stable=False)[1]


def presentation_identity_from_stable_plan(
    plan: Mapping[str, Any],
) -> ReleasePresentationIdentity:
    """Project a validated Stable plan through the same identity validator."""

    return _project_validated_plan(plan, require_stable=True)[1]


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise PresentationError("local annotated tag readback failed")
    return completed.stdout


def _verify_local_annotated_tag(
    repository: Path,
    *,
    identity: ReleasePresentationIdentity,
    expected_commit: str,
) -> dict[str, Any]:
    """Verify the local annotated tag object before any remote tag push."""

    identity = validate_release_presentation_identity(identity)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit, re.ASCII):
        raise PresentationError("expected tag commit is invalid")
    repository = Path(repository)
    tag_ref = f"refs/tags/{identity.release_tag}"
    if _git(repository, "cat-file", "-t", tag_ref) != b"tag\n":
        raise PresentationError("local tag object type is not annotated tag")
    raw = _git(repository, "cat-file", "-p", tag_ref)
    headers, separator, message = raw.partition(b"\n\n")
    if separator != b"\n\n":
        raise PresentationError("local annotated tag object is malformed")
    header_values: dict[bytes, bytes] = {}
    for line in headers.splitlines():
        key, space, value = line.partition(b" ")
        if not space or key in header_values:
            raise PresentationError("local annotated tag header is malformed")
        header_values[key] = value
    if set(header_values) != {b"object", b"type", b"tag", b"tagger"}:
        raise PresentationError("local annotated tag headers are not closed")
    try:
        object_commit = header_values[b"object"].decode("ascii")
        object_type = header_values[b"type"].decode("ascii")
        object_tag = header_values[b"tag"].decode("ascii")
        subject_bytes = identity.annotated_tag_subject.encode("ascii")
    except UnicodeError as error:
        raise PresentationError(
            "local annotated tag contains non-ASCII identity"
        ) from error
    if (
        object_commit != expected_commit
        or object_type != "commit"
        or object_tag != identity.release_tag
        or not header_values[b"tagger"]
    ):
        raise PresentationError("local annotated tag binding is invalid")
    if message != subject_bytes + b"\n":
        raise PresentationError("local annotated tag subject or body is invalid")
    peeled = (
        _git(repository, "rev-parse", f"{tag_ref}^{{commit}}").decode("ascii").strip()
    )
    if peeled != expected_commit:
        raise PresentationError("local annotated tag peeled commit differs")
    return {
        "schema": "animemo.release-presentation-local-tag/v1",
        "release_tag": identity.release_tag,
        "annotated_tag_subject": identity.annotated_tag_subject,
        "annotated_tag_body": "",
        "commit": expected_commit,
        "tagger_present": True,
        "status": "PASS",
    }


def verify_local_annotated_tag(
    repository: Path,
    *,
    identity: ReleasePresentationIdentity,
    expected_commit: str,
) -> dict[str, Any]:
    """Expose one deterministic failure code for the pre-push transaction gate."""

    try:
        return _verify_local_annotated_tag(
            repository,
            identity=identity,
            expected_commit=expected_commit,
        )
    except PresentationError as error:
        raise PresentationError(
            str(error),
            code="LOCAL_TAG_PRESENTATION_MISMATCH",
        ) from error


def _verify_release_presentation_metadata(
    plan: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    repository: Path,
    state: str,
) -> dict[str, Any]:
    """Verify Draft before assets, or public metadata after publication."""

    validated, identity = _project_validated_plan(plan, require_stable=None)
    required = {
        "id",
        "tag_name",
        "name",
        "target_commitish",
        "draft",
        "prerelease",
        "immutable",
        "assets",
    }
    if not isinstance(metadata, Mapping) or set(metadata) != required:
        raise PresentationError("Release presentation metadata is not closed")
    release_id = metadata["id"]
    if (
        isinstance(release_id, bool)
        or not isinstance(release_id, int)
        or release_id < 1
    ):
        raise PresentationError("Release presentation id is invalid")
    if metadata["tag_name"] != identity.release_tag:
        raise PresentationError("Release tag name differs from presentation identity")
    if metadata["name"] != identity.release_title:
        raise PresentationError("Release title differs from presentation identity")
    expected_prerelease = validated["channel"] != "stable"
    if metadata["prerelease"] is not expected_prerelease:
        raise PresentationError(
            "Release prerelease state differs from publication plan"
        )
    if (
        not isinstance(metadata["target_commitish"], str)
        or not metadata["target_commitish"]
    ):
        raise PresentationError("Release target_commitish is invalid")
    assets = metadata["assets"]
    if not isinstance(assets, list):
        raise PresentationError("Release asset metadata is invalid")
    if state == "draft":
        if metadata["draft"] is not True or metadata["immutable"] is not False:
            raise PresentationError("Draft Release state is invalid")
        if assets:
            raise PresentationError(
                "Draft Release must be verified before any asset upload"
            )
    elif state == "published":
        if metadata["draft"] is not False or metadata["immutable"] is not True:
            raise PresentationError("published Release state is invalid")
    else:
        raise PresentationError("Release presentation verification state is invalid")
    tag_receipt = verify_local_annotated_tag(
        repository,
        identity=identity,
        expected_commit=validated["commit"],
    )
    return {
        "schema": "animemo.release-presentation-metadata/v1",
        "publication_plan_identity": validated["identity"],
        "release_id": release_id,
        "release_tag": identity.release_tag,
        "release_title": identity.release_title,
        "annotated_tag_subject": tag_receipt["annotated_tag_subject"],
        "annotated_tag_body": tag_receipt["annotated_tag_body"],
        "state": state.upper(),
        "asset_count": len(assets),
        "status": "PASS",
    }


def verify_release_presentation_metadata(
    plan: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    repository: Path,
    state: str,
) -> dict[str, Any]:
    """Expose transaction-specific failure codes for Draft and public readback."""

    try:
        return _verify_release_presentation_metadata(
            plan,
            metadata=metadata,
            repository=repository,
            state=state,
        )
    except PresentationError as error:
        code = (
            "PARTIAL_DRAFT_RELEASE_TRANSACTION"
            if state == "draft"
            else "PUBLIC_RELEASE_PRESENTATION_AUTHORITY_MISMATCH"
        )
        raise PresentationError(str(error), code=code) from error


def verify_stable_source_rc_presentation(
    *,
    release: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    promotion_acceptance: Mapping[str, Any] | None,
    repository: Path,
) -> dict[str, Any]:
    """Reject an otherwise accepted RC whose public presentation is not authoritative."""

    required_release = {"id", "tag_name", "name", "draft", "prerelease", "immutable"}
    try:
        if not isinstance(release, Mapping) or set(release) != required_release:
            raise PresentationError("source RC Release metadata is not closed")
        acceptance = validate_rc_live_acceptance(acceptance)
        promotion_acceptance = (
            validate_stable_promotion_acceptance(
                promotion_acceptance,
                acceptance=acceptance,
            )
            if promotion_acceptance is not None
            else None
        )
        release_id = release["id"]
        if (
            isinstance(release_id, bool)
            or not isinstance(release_id, int)
            or release_id < 1
        ):
            raise PresentationError("source RC Release id is invalid")
        tag = acceptance["rc_tag"]
        commit = acceptance["rc_commit"]
        identity = validate_release_presentation_identity(
            ReleasePresentationIdentity(tag, release["name"], tag)
        )
        if (
            release["tag_name"] != tag
            or release["draft"] is not False
            or release["prerelease"] is not True
            or release["immutable"] is not True
            or (
                promotion_acceptance is not None
                and promotion_acceptance.get("status") != "AUTHORIZED"
            )
        ):
            raise PresentationError("source RC Release state is not promotable")
        tag_receipt = verify_local_annotated_tag(
            repository,
            identity=identity,
            expected_commit=commit,
        )
    except (AcceptanceError, KeyError, TypeError, PresentationError) as error:
        raise PresentationError(
            str(error),
            code="SOURCE_RC_PRESENTATION_AUTHORITY_MISMATCH",
        ) from error
    return {
        "schema": "animemo.stable-source-rc-presentation/v1",
        "source_rc_tag": identity.release_tag,
        "source_rc_commit": commit,
        "release_title": identity.release_title,
        "annotated_tag_subject": tag_receipt["annotated_tag_subject"],
        "annotated_tag_body": tag_receipt["annotated_tag_body"],
        "immutable": True,
        "prerelease": True,
        "authority_receipt": "PASS",
        "status": "PASS",
    }
