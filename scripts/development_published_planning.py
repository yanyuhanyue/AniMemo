"""Published bytes for a fixed, non-authoritative Fresh planning observation."""
from __future__ import annotations

import json
from pathlib import Path
import re

from release.candidate import canonical_json_bytes, sha256_bytes
from release.materials import read_bounded_release_file, reject_duplicate_json_keys
from scripts.closed_runtime_inventory import closed_runtime_inventory_digest

MODE = 'PUBLISHED_PLATFORM_PLAN'
SCHEMA = 'animemo.development-published-platform-plan/v1'
SUBJECT_SCHEMA = 'animemo.development-published-planning-subject/v1'
FAILURE = 'DEVELOPMENT_PUBLISHED_PLANNING_FAILED'
MAX_REPORT_BYTES = 64 * 1024
MAX_PUBLISHED_ARCHIVE_BYTES = 256 * 1024 * 1024


def _require(condition):
    if not condition:
        raise ValueError('DEVELOPMENT_PUBLISHED_PLANNING_BINDING_INVALID')


def prepare_published_planning_inputs(*, loaded, execution_root, readback_root,
        asset_root, sidecar, sidecar_digest, release_id, windows_gh, linux_gh_package):
    """Read current immutable publication; never issue a Formal capability."""
    from installer.formal_bootstrap import GH_DEB_NAME, GH_DEB_SHA256
    from release.formal_input_readback import read_published_inputs
    from scripts.published_formal_entry import _PinnedWindowsGh
    from updater.source import GitHubPublicRest
    _require(type(release_id) is int and release_id > 0 and type(sidecar_digest) is str
        and re.fullmatch(r'sha256:[0-9a-f]{64}', sidecar_digest))
    raw_sidecar = read_bounded_release_file(Path(sidecar), subject='Original published sidecar',
                                           maximum=16 * 1024 * 1024)
    _require(sha256_bytes(raw_sidecar) == sidecar_digest)
    readback = Path(readback_root)
    read_published_inputs(rest=GitHubPublicRest(runner=_PinnedWindowsGh(windows_gh)), loaded=loaded,
        asset_root=asset_root, sidecar_path=sidecar, root=readback)
    publication = json.loads((readback / 'published-release-readback.json').read_bytes(),
                             object_pairs_hook=reject_duplicate_json_keys)
    _require(publication['id'] == release_id
        and (readback / 'release-attestation.sigstore.json').read_bytes() == raw_sidecar)
    root = Path(execution_root)
    planning = root / 'published-planning'
    planning.mkdir(mode=0o700)
    product = planning / 'published-product'
    additions = {}

    def copy(source, target, expected, maximum):
        raw = read_bounded_release_file(source, subject='Published planning input', maximum=maximum)
        _require(sha256_bytes(raw) == expected)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(raw)
        additions[target.relative_to(root).as_posix()] = expected

    for item in loaded.materials.verified.files:
        copy(loaded.materials.material(item.path), product / item.path, item.sha256, item.size)
    candidate = loaded.candidate_input
    copy(readback / 'installer-materials.tar', planning / 'installer-materials.tar',
         candidate['installer_materials_sha256'], MAX_PUBLISHED_ARCHIVE_BYTES)
    copy(readback / 'release-attestation.sigstore.json', planning / 'release-attestation.sigstore.json',
         sidecar_digest, 16 * 1024 * 1024)
    copy(Path(linux_gh_package), planning / GH_DEB_NAME, 'sha256:' + GH_DEB_SHA256, 20 * 1024 * 1024)
    subject = dict(schema=SUBJECT_SCHEMA, purpose='NON_AUTHORITATIVE_DEVELOPMENT', operation=MODE,
        profile='FRESH_BASE', version=candidate['candidate_version'], release_id=release_id,
        material_source_sha=candidate['source_sha'], material_source_tree=candidate['source_tree'],
        qualification_run_id=candidate['qualification_run_id'], verified_candidate_digest=loaded.verified_digest,
        sidecar_sha256=sidecar_digest, archive_sha256=candidate['installer_materials_sha256'],
        manifest_sha256=candidate['release_manifest_sha256'],
        deployment_contract_sha256=candidate['deployment_contract_sha256'],
        product_inventory_digest=closed_runtime_inventory_digest(product),
        gh_package_sha256='sha256:' + GH_DEB_SHA256)
    raw = canonical_json_bytes(subject)
    target = planning / 'subject.json'
    with target.open('xb') as stream:
        stream.write(raw)
    additions[target.relative_to(root).as_posix()] = sha256_bytes(raw)
    return additions, sha256_bytes(raw)


def validate_published_planning_report(value, *, loaded, expected_binding, expected_context):
    from scripts.development_profile_runner import validate_binding
    from installer.platform_bootstrap import parse_platform_bootstrap_plan, PlatformBootstrapMode
    from installer.runtime import InstallTransportSource
    validate_binding(expected_binding)
    _require(expected_binding['workload_mode'] == MODE and expected_context['profile'] == 'FRESH_BASE')
    fields = {'schema', 'purpose', 'operation', 'result', 'binding', 'context', 'subject',
        'platform_plan', 'failure_code', 'diagnostic_status', 'started_at', 'completed_at',
        'platform_apply_executions', 'installer_executions', 'formal_authority_granted',
        'candidate_acceptance_authority_granted', 'publish_authorized', 'report_digest'}
    _require(type(value) is dict and set(value) == fields and value['schema'] == SCHEMA
        and value['purpose'] == 'NON_AUTHORITATIVE_DEVELOPMENT' and value['operation'] == MODE
        and value['binding'] == expected_binding and value['context'] == expected_context
        and value['result'] in {'PASS', 'FAIL'} and value['platform_apply_executions'] == 0
        and type(value['platform_apply_executions']) is int and value['installer_executions'] == 0
        and type(value['installer_executions']) is int
        and all(value[key] is False for key in ('formal_authority_granted',
            'candidate_acceptance_authority_granted', 'publish_authorized')))
    _require(len(canonical_json_bytes(value)) <= MAX_REPORT_BYTES
        and sha256_bytes(canonical_json_bytes({k: v for k, v in value.items() if k != 'report_digest'}))
            == value['report_digest'])
    subject = value['subject']
    _require(type(subject) is dict and sha256_bytes(canonical_json_bytes(subject))
        == expected_binding['published_subject_digest']
        and subject['schema'] == SUBJECT_SCHEMA and subject['purpose'] == 'NON_AUTHORITATIVE_DEVELOPMENT'
        and subject['operation'] == MODE and subject['profile'] == 'FRESH_BASE'
        and subject['version'] == loaded.candidate_input['candidate_version']
        and subject['archive_sha256'] == loaded.candidate_input['installer_materials_sha256']
        and subject['verified_candidate_digest'] == loaded.verified_digest
        and all(subject[key] == expected_binding[key] for key in
            ('material_source_sha', 'material_source_tree', 'qualification_run_id')))
    from datetime import datetime
    stamps = [value[key] for key in ('started_at', 'completed_at')]
    _require(all(type(stamp) is str and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', stamp) for stamp in stamps)
        and datetime.fromisoformat(stamps[0]) <= datetime.fromisoformat(stamps[1]))
    if value['result'] == 'PASS':
        _require(value['failure_code'] is None and value['diagnostic_status'] == 'NOT_REQUIRED'
            and type(value['platform_plan']) is dict)
        plan = parse_platform_bootstrap_plan(canonical_json_bytes(value['platform_plan']))
        _require(plan.mode is PlatformBootstrapMode.ONLINE_FRESH
            and plan.transport_source is InstallTransportSource.GITHUB)
    else:
        _require(value['failure_code'] == FAILURE and value['platform_plan'] is None
            and value['diagnostic_status'] in {'COMPLETE', 'NO_LOCATION', 'WRITE_FAILED', 'UNAVAILABLE'})
    return value
