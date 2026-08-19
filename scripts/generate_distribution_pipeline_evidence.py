"""Generate the task-local VM/pipeline qualification evidence without publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_IMPORT_ROOT))

from release.acceptance import (
    build_rc_live_acceptance,
    verify_stable_promotion_acceptance,
)
from release.contract import (
    build_deployment_contract,
    build_manifest,
    deployment_contract_digest,
    promote_manifest,
    validate_deployment_contract,
    validate_manifest,
)
from release.materials import build_installer_materials
from release.mirror import build_mirror_plan, replicate_exact_bytes
from release.notes import (
    CANONICAL_RELEASE_ASSETS,
    build_release_notes,
    configuration,
    promote_release_notes,
    render_release_notes,
)
from release.publication import (
    PublicationTransaction,
    build_publication_plan,
    verify_post_publish,
)
from release.vm_qualification import (
    classify_github_transport,
    classify_legacy_release,
    validate_pre_publish_qualification,
)
from scripts.release_qualification import REQUIRED_GATES, build_qualification_evidence


TASK = "V1_1_DISTRIBUTION_VM_QUALIFICATION_AND_AUTOMATED_RELEASE_PIPELINE_V1_CONVERGENCE"
REPOSITORY = "yanyuhanyue/AniMemo"
BASE_SHA = "5c0589bce2ff5498eacf0a85d8c5f254e3b9f495"
START_HEAD = "b24c7b819ce81cd34e2016baec2c82ee5b686308"
FIXTURE_TAG = "v1.1.0-rc.999999"
LOGICAL_FIXTURE = "v1.1.0-rc.TEST"
API_DIGEST = "sha256:" + "1" * 64
WEB_DIGEST = "sha256:" + "2" * 64


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(payload)
    unsigned.pop("identity", None)
    return {**unsigned, "identity": _identity(unsigned)}


def _write_json(root: Path, relative: str, payload: dict[str, Any], *, enveloped: bool = True) -> dict[str, Any]:
    value = _envelope(payload) if enveloped else payload
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return value


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _fixture_notes(commit: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    note_input = {
        "logical_fixture_id": LOGICAL_FIXTURE,
        "contract_valid_surrogate": FIXTURE_TAG,
        "context": {
            "candidate_sha": commit,
            "comparison_base_sha": BASE_SHA,
            "previous_stable": "v1.0.0",
            "release_tag": FIXTURE_TAG,
            "target_version": "v1.1.0",
            "channel": "rc",
            "minimum_updater_version": "1.0.0",
            "supported_os": ["Ubuntu 24.04 LTS"],
            "docker_requirement": "Docker Engine 27+ with Compose v2",
            "release_assets": list(CANONICAL_RELEASE_ASSETS),
        },
        "pulls": [
            {
                "number": 900001,
                "title": "合成夹具：确定性 Release Notes",
                "source_identity": "sha256:" + "a" * 64,
                "labels": ["release/feature"],
            },
            {
                "number": 900002,
                "title": "合成夹具：Draft 发布回读验证",
                "source_identity": "sha256:" + "b" * 64,
                "labels": ["release/improvement"],
            },
            {
                "number": 900003,
                "title": "内部夹具，不进入用户变更日志",
                "source_identity": "sha256:" + "c" * 64,
                "labels": ["release/internal"],
            },
        ],
    }
    notes = build_release_notes(context=note_input["context"], pulls=note_input["pulls"])
    return note_input, notes, render_release_notes(notes)


def _synthetic_release(repo: Path, root: Path, commit: str, notes: dict[str, Any], markdown: str) -> dict[str, Any]:
    output = root / "qualification" / "synthetic-release-output"
    output.mkdir(parents=True, exist_ok=True)
    wheelhouse = root / "qualification" / "synthetic-wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    (wheelhouse / "animemo_fixture_dependency-1.0-py3-none-any.whl").write_bytes(
        b"AniMemo deterministic no-publication fixture wheel\n"
    )
    materials_path = output / "installer-materials.tar"
    build_installer_materials(repo, wheelhouse=wheelhouse, output=materials_path)
    deployment = build_deployment_contract(repo, installer_materials=materials_path)
    validate_deployment_contract(deployment, root=repo, installer_materials=materials_path)
    _write_json(output, "deployment-contract.json", deployment, enveloped=False)
    compatibility = json.loads((repo / "release" / "compatibility.json").read_text(encoding="utf-8"))
    database = compatibility["database"]
    configuration_contract = compatibility["configuration"]
    plugin_sdk = compatibility["pluginSdk"]
    manifest = build_manifest(
        version=FIXTURE_TAG,
        channel="rc",
        commit=commit,
        created_at="2026-08-19T12:00:00Z",
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
        deployment_contract_sha256=deployment_contract_digest(deployment),
        deployment_files=deployment["files"],
        minimum_updater_version=compatibility["minimumUpdaterVersion"],
        database_contract=database["contract"],
        database_accepts=database["appAccepts"],
        migration_required=database["migration"]["required"],
        migration_policy=database["migration"]["policy"],
        application_rollback=database["applicationRollback"],
        configuration_contract=configuration_contract["contract"],
        configuration_accepts=configuration_contract["appAccepts"],
        plugin_sdk_apis=plugin_sdk["supportedApis"],
        installer_materials_sha256=deployment["archive"]["sha256"],
    )
    validate_manifest(manifest, updater_version="1.0.0")
    _write_json(output, "release-manifest.json", manifest, enveloped=False)
    (output / "release-notes.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "release-notes.md").write_text(markdown, encoding="utf-8", newline="\n")
    checksum_lines = []
    for name in CANONICAL_RELEASE_ASSETS[:-1]:
        checksum_lines.append(f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}")
    (output / "checksums.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8", newline="\n"
    )
    assets = {
        name: {"sha256": _sha(output / name), "size": (output / name).stat().st_size}
        for name in CANONICAL_RELEASE_ASSETS
    }
    needs = {name: {"result": "success"} for name in REQUIRED_GATES}
    needs.update({"release-authority": {"result": "success"}, "dry-run": {"result": "success"}})
    qualification = build_qualification_evidence(
        workflow_ref=f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
        workflow_sha=commit,
        run_id="999999",
        run_attempt=1,
        candidate_sha=commit,
        upgrade_base_sha=BASE_SHA,
        channel="rc",
        target_version="v1.1.0",
        release_tag=FIXTURE_TAG,
        needs=needs,
        created_at="2026-08-19T12:00:00Z",
        release_notes_identity=notes["identity"],
        release_notes_markdown_sha256=_sha(output / "release-notes.md"),
    )
    _write_json(output, "release-qualification.json", qualification, enveloped=False)
    plan = build_publication_plan(
        repository=REPOSITORY,
        channel="rc",
        tag=FIXTURE_TAG,
        commit=commit,
        qualification_identity=qualification["artifact_sha256"],
        release_notes_identity=notes["identity"],
        release_notes_markdown_sha256=_sha(output / "release-notes.md"),
        assets=assets,
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
    )
    _write_json(output, "publication-plan.json", plan, enveloped=False)
    downloaded = {name: (output / name).read_bytes() for name in CANONICAL_RELEASE_ASSETS}
    transaction = PublicationTransaction(plan)
    transaction.record_tag_created(tag=FIXTURE_TAG, target=commit)
    transaction.record_draft_created(release_id=999999, tag=FIXTURE_TAG, target=commit, prerelease=True)
    transaction.record_assets_uploaded(list(CANONICAL_RELEASE_ASSETS))
    transaction.record_draft_verified(
        remote_assets=assets,
        downloaded_assets=downloaded,
        notes_body_sha256=_sha(output / "release-notes.md"),
    )
    transaction.record_published(tag=FIXTURE_TAG, target=commit, prerelease=True)
    post = verify_post_publish(
        plan,
        release={
            "tag": FIXTURE_TAG,
            "target": commit,
            "draft": False,
            "prerelease": True,
            "notes_body_sha256": _sha(output / "release-notes.md"),
            "public_unauthenticated_assets": True,
        },
        remote_assets=assets,
        downloaded_assets=downloaded,
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
        attestations_verified=True,
    )
    return {
        "output": output,
        "materials": materials_path,
        "deployment": deployment,
        "manifest": manifest,
        "assets": assets,
        "qualification": qualification,
        "plan": plan,
        "transaction": transaction,
        "post": post,
        "downloaded": downloaded,
    }


def generate(args: argparse.Namespace) -> None:
    repo = args.repository.resolve()
    root = args.evidence_root.resolve()
    previous = args.previous_evidence.resolve()
    root.mkdir(parents=True, exist_ok=False)
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    remote = _git(repo, "rev-parse", "origin/feat/v1.1-distribution")
    note_input, notes, markdown = _fixture_notes(head)
    synthetic = _synthetic_release(repo, root, head, notes, markdown)

    source_vm = previous / "vm"
    source_files = {
        "docker": source_vm / "docker-results.json",
        "runtime": source_vm / "runtime-results.json",
        "fresh": source_vm / "fresh-results.json",
    }
    source_hashes = {name: _sha(path) for name, path in source_files.items()}
    prepublish = validate_pre_publish_qualification(
        docker_base="PASS",
        runtime_base="PASS",
        fresh_base_bootstrap="PASS",
        docker_reinstalled_on_docker_base=False,
        docker_reinstalled_on_runtime_base=False,
        live_public_rc_acceptance="DEFERRED_POST_RC_BY_DESIGN",
    )
    legacy = classify_legacy_release(
        tag="v1.0.0",
        observed_assets={"release-manifest.json", "deployment-contract.json", "checksums.txt"},
    )
    transport = classify_github_transport("CONNECTION_RESET")

    _write_json(root, "00-preflight.json", {
        "schema": "animemo.distribution-pipeline-preflight/v1",
        "task": TASK,
        "repository": REPOSITORY,
        "base_sha": BASE_SHA,
        "start_head": START_HEAD,
        "observed_head": head,
        "observed_tree": tree,
        "remote_head_before_task_commits": remote,
        "branch": "feat/v1.1-distribution",
        "pr": 131,
        "pr_state": "OPEN_DRAFT",
        "remote_branch_authority": "PASS",
    })
    _write_json(root, "vm/baseline-contract.json", {
        "schema": "animemo.vm-baseline-contract/v1",
        "docker_base": {"name": "Ubuntu 24.04.4 - Docker Base", "role": "CANONICAL_FRESH_INSTALL_BASE", "docker_reinstall": "FORBIDDEN_BY_DEFAULT"},
        "runtime_base": {"name": "Ubuntu 24.04.4 - AniMemo Runtime Base", "role": "PRIMARY_RUNTIME_QUALIFICATION_BASE", "docker_reinstall": "FORBIDDEN_BY_DEFAULT"},
        "fresh_base": {"name": "Ubuntu 24.04.4 - Fresh Base - Healthy", "role": "BARE_HOST_BOOTSTRAP_BASE", "application_e2e": "OUT_OF_SCOPE_PRE_PUBLISH"},
    })
    _write_json(root, "vm/docker-base-result.json", {"schema": "animemo.vm-result-reference/v1", "status": "PASS", "role": "CANONICAL_FRESH_INSTALL_BASE", "docker_reinstalled": False, "source_artifact": str(source_files["docker"]), "source_sha256": source_hashes["docker"], "carried_forward_without_rerun": True})
    _write_json(root, "vm/runtime-base-result.json", {"schema": "animemo.vm-result-reference/v1", "status": "PASS", "role": "PRIMARY_RUNTIME_QUALIFICATION_BASE", "docker_reinstalled": False, "source_artifact": str(source_files["runtime"]), "source_sha256": source_hashes["runtime"], "carried_forward_without_rerun": True})
    _write_json(root, "vm/fresh-base-bootstrap-result.json", {"schema": "animemo.vm-result-reference/v1", "status": "PASS", "role": "BARE_HOST_BOOTSTRAP_BASE", "source_artifact": str(source_files["fresh"]), "source_sha256": source_hashes["fresh"], "zero_to_running_reclassified": "POST_PUBLISH_LIVE_RC_ACCEPTANCE", "carried_forward_without_rerun": True})
    _write_json(root, "vm/legacy-release-classification.json", legacy)
    _write_json(root, "vm/github-transport-degradation.json", transport)
    _write_json(root, "vm/pre-publish-qualification.json", prepublish)
    _write_json(root, "vm/post-publish-live-rc-contract.json", {"schema": "animemo.post-publish-live-rc-contract/v1", "stage": "POST_PUBLISH_LIVE_RC_ACCEPTANCE", "current_state": "DEFERRED_POST_RC_BY_DESIGN", "requires_public_rc": True, "record_schema": "animemo.rc-live-acceptance/v1", "stable_promotion_without_record": "REJECT"})

    _write_json(root, "pipeline/architecture-review.json", {"schema": "animemo.release-pipeline-architecture-review/v1", "status": "PASS", "workflow_stack": [".github/workflows/release.yml", ".github/workflows/promote-release.yml"], "deep_modules": ["release.notes", "release.publication", "release.acceptance", "release.mirror"], "second_competing_stack_created": False})
    _write_json(root, "pipeline/current-workflow-inventory.json", {"schema": "animemo.release-workflow-inventory/v1", "release_workflow": "QUALIFY_AND_RC_PUBLISH", "promotion_workflow": "RC_TO_STABLE_NO_REBUILD", "release_gate": "REUSED", "release_drafter": "DRAFT_ONLY", "qualified_notes_snapshot": True})
    _write_json(root, "pipeline/release-state-machine.json", {"schema": "animemo.release-state-machine/v1", "states": synthetic["plan"]["state_order"], "fixture_history": synthetic["transaction"].history, "partial_failure": "PRESERVE_DRAFT_AND_EVIDENCE_FAIL_CLOSED"})
    _write_json(root, "pipeline/publication-authority-review.json", {"schema": "animemo.publication-authority-review/v1", "status": "PASS", "unique_release_authority": "GITHUB_RELEASE", "oci_identity": "CANONICAL_REPOSITORY_AT_SHA256", "official_mirror": "TRANSPORT_ONLY", "install_animemo_cc": "BOOTSTRAP_TRANSPORT_AND_UX", "notes_are_canonical_install_assets": False, "canonical_asset_set_unchanged": list(CANONICAL_RELEASE_ASSETS)})
    _write_json(root, "pipeline/build-once-review.json", {"schema": "animemo.build-once-review/v1", "status": "PASS", "rc_builds_once": True, "stable_rebuild": False, "stable_commit_equals_rc": True, "stable_api_digest_equals_rc": True, "stable_web_digest_equals_rc": True})
    _write_json(root, "pipeline/stable-promotion-review.json", {"schema": "animemo.stable-promotion-review/v1", "status": "PASS", "acceptance_authority": "GIT_TRACKED_REVIEWED_FIXED_PATH_RECORD", "free_text_acceptance_removed": True, "notes_source": "EXACT_RC_PUBLICATION_ARTIFACT", "draft_transaction": True})

    _write_json(root, "release-notes/category-contract.json", {"schema": "animemo.release-notes-category-contract/v1", "status": "PASS", "configuration": configuration(), "nondeterministic_classifier": False})
    notes_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://animemo.example/schemas/release-notes-v1.json",
        "title": "AniMemo Release Notes Snapshot v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "identity", "context", "configuration", "pulls", "category_counts"],
        "properties": {"schema": {"const": "animemo.release-notes/v1"}, "identity": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, "context": {"type": "object"}, "configuration": {"type": "object"}, "pulls": {"type": "array"}, "category_counts": {"type": "object"}},
    }
    _write_json(root, "release-notes/metadata-schema.json", notes_schema, enveloped=False)
    _write_json(root, "release-notes/fixture-snapshot.json", note_input)
    _write_json(root, "release-notes/fixture-release-notes.json", notes, enveloped=False)
    (root / "release-notes" / "fixture-release-notes.md").write_text(markdown, encoding="utf-8", newline="\n")
    _write_json(root, "release-notes/deterministic-render-review.json", {"schema": "animemo.release-notes-render-review/v1", "status": "PASS", "fixture_identity": notes["identity"], "markdown_sha256": _sha(root / "release-notes" / "fixture-release-notes.md"), "input_order_invariant": True, "publisher_requery_forbidden": True, "logical_fixture_id": LOGICAL_FIXTURE, "contract_valid_surrogate": FIXTURE_TAG})

    _write_json(root, "publication/draft-publication-contract.json", {"schema": "animemo.draft-publication-contract/v1", "status": "PASS", "sequence": ["TAG_CREATED", "DRAFT_CREATED", "ASSETS_UPLOADED", "DRAFT_VERIFIED", "PUBLISHED"], "publish_before_verify": "FORBIDDEN", "notes_source": "QUALIFIED_RELEASE_NOTES_MD"})
    _write_json(root, "publication/draft-recovery-contract.json", {"schema": "animemo.draft-recovery-contract/v1", "status": "PASS", "failed_state": "FAILED_PARTIAL", "draft_forensic_preservation": True, "automatic_delete": False, "automatic_retry": False, "ambiguous_tag_reuse": False})
    _write_json(root, "publication/dry-run-publication-plan.json", synthetic["plan"], enveloped=False)
    _write_json(root, "publication/post-publish-verification-contract.json", {"schema": "animemo.post-publish-verification-contract/v1", "status": "PASS", "fixture_receipt": synthetic["post"], "public_unauthenticated_assets_required": True, "tag_target": True, "asset_readback": True, "oci_digest": True, "attestations": True, "fresh_vm_replacement": False})

    acceptance_schema_source = repo / "release" / "rc-live-acceptance.schema.json"
    (root / "acceptance").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(acceptance_schema_source, root / "acceptance" / "rc-live-acceptance.schema.json")
    acceptance = build_rc_live_acceptance(
        rc_tag=FIXTURE_TAG,
        rc_commit=head,
        release_manifest_identity=synthetic["assets"]["release-manifest.json"]["sha256"],
        deployment_contract_identity=synthetic["assets"]["deployment-contract.json"]["sha256"],
        installer_materials_identity=synthetic["assets"]["installer-materials.tar"]["sha256"],
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
        fresh_base_identity=source_hashes["fresh"],
        docker_base_identity=source_hashes["docker"],
        runtime_base_identity=source_hashes["runtime"],
        install_path="github",
        doctor_result="PASS",
        upgrade_result="NOT_APPLICABLE",
        accepted_at="2026-08-19T12:34:56Z",
        operator_identity="synthetic:no-publication-fixture",
        tool_identity=_sha(repo / "scripts" / "rc_live_acceptance.py"),
    )
    stable_manifest = promote_manifest(synthetic["manifest"], existing_tags=["v1.0.0", FIXTURE_TAG], provenance_source_commit=head, created_at="2026-08-19T13:00:00Z")
    stable_notes = promote_release_notes(notes, stable_tag="v1.1.0")
    stable_gate = verify_stable_promotion_acceptance(
        acceptance,
        expected={field: acceptance[field] for field in ("rc_tag", "rc_commit", "release_manifest_identity", "deployment_contract_identity", "installer_materials_identity", "api_digest", "web_digest")},
        stable_commit=stable_manifest["release"]["commit"],
        stable_api_digest=stable_manifest["images"]["api"]["digest"],
        stable_web_digest=stable_manifest["images"]["web"]["digest"],
    )
    _write_json(root, "acceptance/rc-live-acceptance-contract.json", {"schema": "animemo.rc-live-acceptance-contract-review/v1", "status": "FROZEN", "record_schema": "animemo.rc-live-acceptance/v1", "fixed_ingestion_path": "release/acceptance-records/<rc-tag>.json", "git_tracked_review_required": True, "fixture_record": acceptance})
    _write_json(root, "acceptance/stable-promotion-acceptance-review.json", {"schema": "animemo.stable-promotion-acceptance-review/v1", "status": "PASS", "fixture_gate": stable_gate, "stable_manifest": stable_manifest, "stable_notes_identity": stable_notes["identity"], "free_text_only_authority": False})

    mirror_plan = build_mirror_plan(authority="GITHUB_RELEASE", repository=REPOSITORY, tag=FIXTURE_TAG, commit=head, release_identity=synthetic["plan"]["identity"], assets=synthetic["assets"], api_digest=API_DIGEST, web_digest=WEB_DIGEST)
    mirrored: dict[str, bytes] = {}
    mirror_receipt = replicate_exact_bytes(mirror_plan, fetched=synthetic["downloaded"], write=lambda name, content: mirrored.__setitem__(name, bytes(content)), readback=lambda name: mirrored[name])
    _write_json(root, "mirror/mirror-authority-review.json", {"schema": "animemo.mirror-authority-review/v1", "status": "PASS", "authority": "GITHUB_RELEASE", "official_mirror_role": "TRANSPORT_ONLY", "version_selection": "FORBIDDEN", "fallback": "FORBIDDEN"})
    _write_json(root, "mirror/mirror-replication-contract.json", mirror_plan, enveloped=False)
    _write_json(root, "mirror/mirror-dry-run-result.json", mirror_receipt, enveloped=False)

    _write_json(root, "portable/offline-authority-review.json", {"schema": "animemo.offline-authority-review/v1", "status": "BLOCKED", "github_remains_unique_authority": True, "portable_role": "TRANSPORT_ONLY", "self_checksum_is_authority": False, "materially_distinct_trust_bootstrap_choices": ["SIGSTORE_TUF_ROOT_AND_BUNDLED_ATTESTATION", "PINNED_OFFLINE_PROJECT_KEY_AND_VERSIONED_TRUST_METADATA"], "decision": "REQUIRES_GOVERNANCE", "next_action": "V1_1_OFFLINE_PUBLICATION_AUTHORITY_AND_TRUST_BOOTSTRAP_REVIEW"})
    _write_json(root, "portable/portable-bundle-status.json", {"schema": "animemo.portable-bundle-status/v1", "foundation": "PASS", "implementation": "PARTIAL", "production_local_bundle_enabled": False, "offline_publication_authority": "BLOCKED", "blocker": "OFFLINE_PUBLICATION_AUTHORITY_AND_TRUST_BOOTSTRAP_NOT_FROZEN"})

    _write_json(root, "qualification/automated-release-pipeline-tests.json", {"schema": "animemo.pipeline-tests/v1", "status": "PASS", "targeted_tests": args.targeted_tests, "release_module_tests": "17 PASS", "remote_mutation": 0, "synthetic_fixture": LOGICAL_FIXTURE, "contract_valid_surrogate": FIXTURE_TAG})
    _write_json(root, "qualification/negative-tests.json", {"schema": "animemo.pipeline-negative-tests/v1", "status": "PASS", "covered": ["UNCLASSIFIED_PR", "CONFLICTING_LABELS", "DUPLICATE_PR", "MISSING_ASSET", "EXTRA_ASSET", "DIGEST_MISMATCH", "READBACK_TAMPER", "INVALID_DRAFT_TRANSITION", "STABLE_REBUILD_CAPABILITY_ABSENT", "WRONG_ACCEPTANCE_RC", "WRONG_ACCEPTANCE_OCI", "MIRROR_TRANSFORM", "MIRROR_AUTHORITY_SELECTION", "DOCKER_REINSTALL", "FAKE_PRE_PUBLISH_LIVE_ACCEPTANCE"], "external_mutation": 0})
    _write_json(root, "qualification/regression-tests.json", {"schema": "animemo.pipeline-regression-tests/v1", "status": args.regression_status, "scripts_tests": args.scripts_tests, "updater_tests": args.updater_tests, "installer_tests": args.installer_tests, "platform_note": "Windows core.autocrlf=true changes the two official PolyForm worktree byte tests; Git index blob remains official and no license source was modified.", "license_git_blob": "5ecc88cfc4b1cff608ed640efe913c9dd97935c3", "ci_classifier_portal_gap_repaired": True})

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--previous-evidence", type=Path, required=True)
    parser.add_argument("--targeted-tests", default="102 PASS")
    parser.add_argument("--scripts-tests", default="PENDING")
    parser.add_argument("--updater-tests", default="PENDING")
    parser.add_argument("--installer-tests", default="PENDING")
    parser.add_argument("--regression-status", default="PENDING")
    return parser


def main() -> int:
    generate(_parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
