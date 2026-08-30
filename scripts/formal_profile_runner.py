"""Closed guest runner for one post-publication Formal profile.

The runner resolves and installs the published RC through the production
Installer composition.  Callers cannot supply a PASS receipt or substitute a
profile observation; only the production executor's readbacks are accepted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from release.candidate import (
    canonical_json_bytes,
    reject_duplicate_json_keys,
    sha256_bytes,
)
from release.formal_vm_controller import (
    FORMAL_PROFILES,
    FormalAuthorityRequest,
    FormalProducerError,
)

CONTEXT_ENV = "ANIMEMO_FORMAL_PROFILE_CONTEXT_B64URL"
FORMAL_GUEST_RECEIPT = Path(
    "/var/lib/animemo/formal-acceptance/profile-receipt-draft.json"
)
PUBLIC_ORIGIN = "https://formal.invalid"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class FormalProfileRunnerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FormalInstallExecutor(Protocol):
    def execute(
        self,
        *,
        authority: FormalAuthorityRequest,
        authority_root: Path,
        profile: str,
    ) -> Mapping[str, object]: ...


def _reject(code: str) -> None:
    raise FormalProfileRunnerError(code)


def _strict_json(path: Path, *, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or metadata.st_nlink != 1
            or metadata.st_size < 2
            or metadata.st_size > maximum
        ):
            _reject("FORMAL_PROFILE_AUTHORITY_INPUT_UNSAFE")
        value = path.read_bytes()
        parsed = json.loads(
            value.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except FormalProfileRunnerError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise FormalProfileRunnerError(
            "FORMAL_PROFILE_AUTHORITY_INPUT_INVALID"
        ) from error
    if type(parsed) is not dict or canonical_json_bytes(parsed) != value:
        _reject("FORMAL_PROFILE_AUTHORITY_INPUT_INVALID")
    return parsed


def _load_authority(
    root: Path,
) -> tuple[FormalAuthorityRequest, str, dict[str, Any]]:
    value = _strict_json(root / "formal-rc-authority.json")
    identity = value.pop("identity", None)
    if (
        value.pop("schema", None) != "animemo.formal-rc-authority/v1"
        or value.pop("release_authority_granted", None) is not False
        or value.pop("publish_authorized", None) is not False
    ):
        _reject("FORMAL_PROFILE_AUTHORITY_INPUT_INVALID")
    try:
        authority = FormalAuthorityRequest(**value)
    except (TypeError, FormalProducerError) as error:
        raise FormalProfileRunnerError(
            "FORMAL_PROFILE_AUTHORITY_INPUT_INVALID"
        ) from error
    expected = sha256_bytes(canonical_json_bytes(authority.identity_body()))
    if identity != expected:
        _reject("FORMAL_PROFILE_AUTHORITY_INPUT_INVALID")
    publication = _strict_json(root / "formal-publication-preflight.json")
    if (
        set(publication)
        != {
            "schema",
            "publication_authority_identity",
            "publication_execution_receipt_identity",
            "publication_signed_claim_identity",
            "publication_signed_at",
            "formal_windows_pretrust_kit_identity",
            "offline_release_trust_profile_identity",
            "pretrusted_profile_identity",
            "provenance_verifier_identity",
            "github_trusted_root_identity",
            "sigstore_trusted_root_identity",
            "release_authority_granted",
            "publish_authorized",
        }
        or publication["schema"] != "animemo.formal-publication-preflight/v1"
        or publication["publication_authority_identity"]
        != authority.publication_identity
        or any(
            type(publication[field]) is not str
            or _DIGEST.fullmatch(publication[field]) is None
            for field in (
                "publication_execution_receipt_identity",
                "publication_signed_claim_identity",
                "formal_windows_pretrust_kit_identity",
                "offline_release_trust_profile_identity",
                "pretrusted_profile_identity",
                "provenance_verifier_identity",
                "github_trusted_root_identity",
                "sigstore_trusted_root_identity",
            )
        )
        or type(publication["publication_signed_at"]) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            publication["publication_signed_at"],
        )
        is None
        or publication["release_authority_granted"] is not False
        or publication["publish_authorized"] is not False
    ):
        _reject("FORMAL_PROFILE_PUBLICATION_INPUT_INVALID")
    return authority, expected, publication


def _decode_context(value: str) -> dict[str, Any]:
    if (
        type(value) is not str
        or not value
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        _reject("FORMAL_PROFILE_CONTEXT_INVALID")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        context = json.loads(
            decoded.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys
        )
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise FormalProfileRunnerError("FORMAL_PROFILE_CONTEXT_INVALID") from error
    if (
        type(context) is not dict
        or set(context) != {"profile", "rc_authority_identity"}
        or context.get("profile") not in FORMAL_PROFILES
        or type(context.get("rc_authority_identity")) is not str
        or _DIGEST.fullmatch(context["rc_authority_identity"]) is None
        or canonical_json_bytes(context) != decoded
    ):
        _reject("FORMAL_PROFILE_CONTEXT_INVALID")
    return context


def _production_executor_output(
    *,
    authority: FormalAuthorityRequest,
    authority_root: Path,
    profile: str,
) -> dict[str, object]:
    from durability.instance import DEFAULT_INSTANCE_NAME
    from installer.production import (
        ProductionDoctorAcceptance,
        build_formal_production_composition,
        build_production_composition,
        issue_formal_candidate_bound_offline_verifier,
    )
    from installer.runtime import (
        InstallerMode,
        InstallOutcome,
        InstallRequest,
        InstallTransportSource,
        ReleaseSelector,
        explicit_transport_policy,
    )

    transport = (
        InstallTransportSource.LOCAL_BUNDLE
        if profile == "FORMAL_OFFLINE"
        else InstallTransportSource.GITHUB
    )
    payload = authority_root / f"animemo-{authority.rc_tag}-portable.tar"
    sidecar = authority_root / "release-attestation.sigstore.json"
    if profile == "FORMAL_OFFLINE":
        for path in (payload, sidecar):
            try:
                metadata = path.lstat()
            except OSError as error:
                raise FormalProfileRunnerError(
                    "FORMAL_OFFLINE_MATERIAL_UNAVAILABLE"
                ) from error
            if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
                _reject("FORMAL_OFFLINE_MATERIAL_UNSAFE")
    if profile == "FORMAL_OFFLINE":
        composition = build_formal_production_composition(
            instance_name=DEFAULT_INSTANCE_NAME,
            transport_policy=explicit_transport_policy(transport),
            local_bundle_payload=payload,
            local_bundle_release_attestation=sidecar,
            offline_verifier_capability=(
                issue_formal_candidate_bound_offline_verifier(
                    authority_root,
                    expected_profile_identity=(
                        authority.offline_release_trust_profile_identity
                    ),
                )
            ),
        )
    else:
        composition = build_production_composition(
            instance_name=DEFAULT_INSTANCE_NAME,
            transport_source=transport,
            transport_policy=explicit_transport_policy(transport),
        )
    try:
        request = InstallRequest(
            mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version=authority.rc_tag),
            public_origin=PUBLIC_ORIGIN,
            transport_source=transport,
            non_interactive=True,
        )
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        session = composition.plan_platform(request, observed_at)
        if (
            session.release.version != authority.rc_tag
            or session.release.commit != authority.source_sha
        ):
            _reject("FORMAL_PROFILE_RESOLVED_RELEASE_MISMATCH")
        platform_receipt = composition.execute_platform(
            session, session.plan.plan_digest
        )
        plan = composition.runtime.plan(request)
        result = composition.runtime.execute(
            plan, accepted_plan_digest=plan.plan_digest
        )
        install_succeeded = result.outcome is InstallOutcome.SUCCEEDED
        fresh = getattr(composition.runtime, "_fresh", None)
        doctor = getattr(fresh, "doctor_acceptor", None)
        if type(doctor) is not ProductionDoctorAcceptance:
            _reject("FORMAL_PROFILE_DOCTOR_OBSERVATION_UNAVAILABLE")
        doctor_report = doctor.latest_report
        canonical_acceptance = list(doctor.latest_canonical_acceptance)
        expected_test_names = [
            "application.journal-crud",
            "service.api.health",
            "service.web.health",
        ]
        if doctor_report is None:
            if install_succeeded:
                _reject("FORMAL_PROFILE_DOCTOR_OBSERVATION_UNAVAILABLE")
            doctor_value = None
            canonical_acceptance = []
        else:
            doctor_value = doctor_report.as_dict()
            if [item.get("name") for item in canonical_acceptance] != (
                expected_test_names
            ):
                _reject("FORMAL_PROFILE_DOCTOR_OBSERVATION_UNAVAILABLE")
        acceptance_passed = (
            install_succeeded
            and doctor_value is not None
            and doctor_report.overall_status.value == "PASS"
            and all(item.get("result") == "PASS" for item in canonical_acceptance)
        )
        materials = composition.releases.latest_materials()
        images = materials.manifest.get("images")
        if type(images) is not dict:
            _reject("FORMAL_PROFILE_RESOLVED_RELEASE_MISMATCH")
        offline_authority_binding = None
        if profile == "FORMAL_OFFLINE":
            try:
                offline_authority_binding = (
                    composition.releases.latest_offline_authority_binding()
                )
            except Exception as error:
                raise FormalProfileRunnerError(
                    "FORMAL_OFFLINE_RELEASE_EXECUTION_UNAVAILABLE"
                ) from error
        return {
            "resolvedRelease": {
                "version": session.release.version,
                "sourceSha": session.release.commit,
                "releaseManifestIdentity": session.release.manifest_digest,
                "deploymentContractIdentity": (
                    session.release.deployment_identity_digest
                ),
                "installerMaterialsIdentity": (
                    session.release.material_identity_digest
                ),
                "apiDigest": images["api"]["digest"],
                "webDigest": images["web"]["digest"],
                "publicationIdentity": authority.publication_identity,
                "workflowIdentity": authority.workflow_identity,
                "attestationClaimIdentities": dict(
                    authority.attestation_claim_identities
                ),
            },
            "transportSource": transport.value,
            "platformPlanDigest": session.plan.plan_digest,
            "platformReceiptDigest": sha256_bytes(
                canonical_json_bytes(platform_receipt.as_dict())
            ),
            "installerPlanDigest": plan.plan_digest,
            "installerExecutionReceiptDigest": sha256_bytes(
                canonical_json_bytes(result.as_dict())
            ),
            "doctorReport": doctor_value,
            "doctorReceiptDigest": (
                sha256_bytes(canonical_json_bytes(doctor_value))
                if doctor_value is not None
                else None
            ),
            "canonicalAcceptanceTests": canonical_acceptance,
            "offlineAuthorityBinding": offline_authority_binding,
            "result": "PASS" if acceptance_passed else "FAIL",
        }
    finally:
        composition.close_formal_authority()


class ProductionFormalInstallExecutor:
    def execute(
        self,
        *,
        authority: FormalAuthorityRequest,
        authority_root: Path,
        profile: str,
    ) -> Mapping[str, object]:
        return _production_executor_output(
            authority=authority,
            authority_root=authority_root,
            profile=profile,
        )


def _validated_observation(
    value: object,
    *,
    authority: FormalAuthorityRequest,
    authority_identity: str,
    publication: Mapping[str, Any],
    profile: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "resolvedRelease",
        "transportSource",
        "platformPlanDigest",
        "platformReceiptDigest",
        "installerPlanDigest",
        "installerExecutionReceiptDigest",
        "doctorReport",
        "doctorReceiptDigest",
        "canonicalAcceptanceTests",
        "offlineAuthorityBinding",
        "result",
    }:
        _reject("FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID")
    release = value["resolvedRelease"]
    expected_transport = "local-bundle" if profile == "FORMAL_OFFLINE" else "github"
    if (
        type(release) is not dict
        or set(release)
        != {
            "version",
            "sourceSha",
            "releaseManifestIdentity",
            "deploymentContractIdentity",
            "installerMaterialsIdentity",
            "apiDigest",
            "webDigest",
            "publicationIdentity",
            "workflowIdentity",
            "attestationClaimIdentities",
        }
        or release
        != {
            "version": authority.rc_tag,
            "sourceSha": authority.source_sha,
            "releaseManifestIdentity": authority.release_manifest_identity,
            "deploymentContractIdentity": authority.deployment_contract_identity,
            "installerMaterialsIdentity": authority.installer_materials_identity,
            "apiDigest": authority.api_digest,
            "webDigest": authority.web_digest,
            "publicationIdentity": authority.publication_identity,
            "workflowIdentity": authority.workflow_identity,
            "attestationClaimIdentities": dict(authority.attestation_claim_identities),
        }
        or value["transportSource"] != expected_transport
        or value["result"] not in {"PASS", "FAIL"}
    ):
        _reject("FORMAL_PROFILE_PRODUCTION_OBSERVATION_MISMATCH")
    doctor = value["doctorReport"]
    tests = value["canonicalAcceptanceTests"]
    offline_binding = value["offlineAuthorityBinding"]
    offline_binding_valid = offline_binding is None
    if profile == "FORMAL_OFFLINE" and type(offline_binding) is dict:
        unsigned_offline_binding = dict(offline_binding)
        offline_binding_identity = unsigned_offline_binding.pop("identity", None)
        compact_offline_binding = json.dumps(
            unsigned_offline_binding,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        release_execution = offline_binding.get("releaseExecutionReceipt")
    else:
        release_execution = None
    release_execution_valid = False
    if profile == "FORMAL_OFFLINE" and type(release_execution) is dict:
        unsigned_release_execution = dict(release_execution)
        release_execution_identity = unsigned_release_execution.pop("identity", None)
        compact = json.dumps(
            unsigned_release_execution,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        release_execution_valid = (
            set(release_execution)
            == {
                "schema",
                "publicationIdentity",
                "publicationExecutionReceiptIdentity",
                "signedClaimIdentity",
                "signedAt",
                "identity",
            }
            and release_execution["schema"] == "animemo.release-execution-receipt/v1"
            and release_execution["publicationIdentity"]
            == authority.publication_identity
            and release_execution["publicationExecutionReceiptIdentity"]
            == publication["publication_execution_receipt_identity"]
            and release_execution["signedClaimIdentity"]
            == publication["publication_signed_claim_identity"]
            and release_execution["signedAt"] == publication["publication_signed_at"]
            and release_execution_identity
            == "sha256:" + hashlib.sha256(compact).hexdigest()
        )
        offline_binding_valid = (
            set(offline_binding)
            == {
                "schema",
                "version",
                "trustProfileVersion",
                "trustProfileIdentity",
                "releaseExecutionReceipt",
                "identity",
            }
            and offline_binding["schema"]
            == "animemo.offline-release-authority-binding/v1"
            and offline_binding["version"] == authority.rc_tag
            and type(offline_binding["trustProfileVersion"]) is int
            and offline_binding["trustProfileVersion"] >= 1
            and offline_binding["trustProfileIdentity"]
            == publication["offline_release_trust_profile_identity"]
            and offline_binding_identity
            == "sha256:" + hashlib.sha256(compact_offline_binding).hexdigest()
        )
    expected_test_names = [
        "application.journal-crud",
        "service.api.health",
        "service.web.health",
    ]
    if doctor is None:
        doctor_valid = (
            value["result"] == "FAIL"
            and value["doctorReceiptDigest"] is None
            and tests == []
        )
        acceptance_observation_passed = False
    else:
        doctor_valid = (
            type(doctor) is dict
            and doctor.get("overallStatus") in {"PASS", "FAIL"}
            and type(doctor.get("checks")) is list
            and bool(doctor["checks"])
            and all(
                type(item) is dict and item.get("status") in {"PASS", "FAIL"}
                for item in doctor["checks"]
            )
            and value["doctorReceiptDigest"]
            == sha256_bytes(canonical_json_bytes(doctor))
            and type(tests) is list
            and [item.get("name") for item in tests if type(item) is dict]
            == expected_test_names
            and all(
                type(item) is dict
                and item.get("result") in {"PASS", "FAIL"}
                and isinstance(item.get("receiptDigest"), str)
                and _DIGEST.fullmatch(item["receiptDigest"]) is not None
                for item in tests
            )
        )
        acceptance_observation_passed = (
            doctor_valid
            and doctor["overallStatus"] == "PASS"
            and all(item["result"] == "PASS" for item in tests)
        )
    if (
        not doctor_valid
        or (value["result"] == "PASS") is not acceptance_observation_passed
        or any(
            type(value[field]) is not str or _DIGEST.fullmatch(value[field]) is None
            for field in (
                "platformPlanDigest",
                "platformReceiptDigest",
                "installerPlanDigest",
                "installerExecutionReceiptDigest",
            )
        )
        or not offline_binding_valid
        or (profile == "FORMAL_OFFLINE" and not release_execution_valid)
        or (profile != "FORMAL_OFFLINE" and offline_binding is not None)
    ):
        _reject("FORMAL_PROFILE_PRODUCTION_OBSERVATION_INVALID")
    return {
        "schema": "animemo.formal-profile-observation-draft/v1",
        "version": 1,
        "profile": profile,
        "rc_authority_identity": authority_identity,
        "transport_source": expected_transport,
        "resolved_release": {
            "version": release["version"],
            "source_sha": release["sourceSha"],
            "release_manifest_identity": release["releaseManifestIdentity"],
            "deployment_contract_identity": release["deploymentContractIdentity"],
            "installer_materials_identity": release["installerMaterialsIdentity"],
            "api_digest": release["apiDigest"],
            "web_digest": release["webDigest"],
            "publication_identity": release["publicationIdentity"],
            "workflow_identity": release["workflowIdentity"],
            "attestation_claim_identities": release["attestationClaimIdentities"],
        },
        "publication_execution_receipt_identity": publication[
            "publication_execution_receipt_identity"
        ],
        "publication_signed_claim_identity": publication[
            "publication_signed_claim_identity"
        ],
        "publication_signed_at": publication["publication_signed_at"],
        "formal_windows_pretrust_kit_identity": publication[
            "formal_windows_pretrust_kit_identity"
        ],
        "offline_release_trust_profile_identity": publication[
            "offline_release_trust_profile_identity"
        ],
        "pretrusted_profile_identity": publication["pretrusted_profile_identity"],
        "provenance_verifier_identity": publication["provenance_verifier_identity"],
        "github_trusted_root_identity": publication["github_trusted_root_identity"],
        "sigstore_trusted_root_identity": publication["sigstore_trusted_root_identity"],
        "platform_plan_digest": value["platformPlanDigest"],
        "platform_receipt_digest": value["platformReceiptDigest"],
        "installer_plan_digest": value["installerPlanDigest"],
        "installer_execution_receipt_digest": value["installerExecutionReceiptDigest"],
        "doctor_receipt_digest": value["doctorReceiptDigest"],
        "canonical_acceptance_receipt_digests": [
            item["receiptDigest"] for item in tests
        ],
        "release_authority_granted": False,
        "publish_authorized": False,
        "result": value["result"],
    }


def execute_profile(
    *,
    authority_root: Path,
    profile: str,
    context_b64url: str,
    executor: FormalInstallExecutor | None = None,
) -> dict[str, Any]:
    if profile not in FORMAL_PROFILES:
        _reject("FORMAL_PROFILE_INVALID")
    authority, authority_identity, publication = _load_authority(authority_root)
    context = _decode_context(context_b64url)
    if context != {
        "profile": profile,
        "rc_authority_identity": authority_identity,
    }:
        _reject("FORMAL_PROFILE_CONTEXT_MISMATCH")
    try:
        value = (executor or ProductionFormalInstallExecutor()).execute(
            authority=authority,
            authority_root=authority_root,
            profile=profile,
        )
    except FormalProfileRunnerError:
        raise
    except Exception as error:
        raise FormalProfileRunnerError("FORMAL_PROFILE_EXECUTION_FAILED") from error
    return _validated_observation(
        value,
        authority=authority,
        authority_identity=authority_identity,
        publication=publication,
        profile=profile,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AniMemo Formal profile runner")
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--profile", choices=FORMAL_PROFILES, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "PLAN_ONLY",
                    "profile": args.profile,
                    "releaseAuthorityGranted": False,
                    "publishAuthorized": False,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        receipt = execute_profile(
            authority_root=args.authority_root,
            profile=args.profile,
            context_b64url=os.environ.get(CONTEXT_ENV, ""),
        )
        FORMAL_GUEST_RECEIPT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(FORMAL_GUEST_RECEIPT, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json_bytes(receipt))
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"status": receipt["result"]}, sort_keys=True))
        return 0
    except (FormalProfileRunnerError, FormalProducerError) as error:
        print(json.dumps({"code": getattr(error, "code", str(error))}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
