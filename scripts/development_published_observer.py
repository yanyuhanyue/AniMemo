"""Fixed DEV observer around original published production plan_platform only.

Executed as an explicit file in an isolated interpreter. Its execution tree is
not placed on sys.path: installer/release/updater imports belong to the original
protected product, and its diagnostic writer is loaded under a separate name.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

PRODUCT_ROOT = Path('/var/lib/animemo/bootstrap-authority/v1/materials')
OUTPUT = Path('/var/lib/animemo/local-development/profile-report.json')
MODE = 'PUBLISHED_PLATFORM_PLAN'


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


def _digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError('DEVELOPMENT_PUBLISHED_PLANNING_CONTEXT_INVALID')


def _module_origin(name):
    module = sys.modules.get(name)
    _require(module is not None and Path(module.__file__).resolve(strict=True)
        == PRODUCT_ROOT / (name.replace('.', '/') + '.py'))


def observe(subject, diagnostic):
    """Actual production composition and order; no executor injection or apply."""
    from durability.instance import DEFAULT_INSTANCE_NAME
    from installer.production import build_production_composition
    from installer.runtime import (
        InstallRequest, InstallerMode, ReleaseSelector, InstallTransportSource, explicit_transport_policy,
    )
    for name in ('installer.production', 'installer.runtime'):
        _module_origin(name)
    transport = InstallTransportSource.GITHUB
    composition = build_production_composition(instance_name=DEFAULT_INSTANCE_NAME,
        transport_source=transport, transport_policy=explicit_transport_policy(transport))
    try:
        request = InstallRequest(mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version=subject['version']), public_origin='https://formal.invalid',
            transport_source=transport, non_interactive=True)
        diagnostic.stage('PLATFORM_PREPARING')
        session = composition.plan_platform(request, datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        diagnostic.stage('PLATFORM_PLANNED')
        _require(session.release.version == subject['version']
            and session.release.commit == subject['material_source_sha']
            and session.release.manifest_digest == subject['manifest_sha256']
            and session.release.deployment_identity_digest == subject['deployment_contract_sha256'])
        # Validate Fresh after the production plan is complete. The returned
        # action list remains a plan and is never passed to execute_platform.
        from installer.platform_bootstrap import PlatformBootstrapMode
        _require(session.plan.mode is PlatformBootstrapMode.ONLINE_FRESH)
        return session.plan.as_dict()
    finally:
        composition.close_formal_authority()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, choices=('FRESH_BASE',))
    parser.add_argument('--binding', required=True)
    args = parser.parse_args(argv)
    execution = Path(__file__).resolve().parents[1]
    # This module has no package imports before selecting the published tree.
    specification = importlib.util.spec_from_file_location('_animemo_development_diagnostic',
        execution / 'scripts/candidate_diagnostics.py')
    diagnostic_module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(diagnostic_module)
    diagnostic = diagnostic_module.inherited_writer()
    if diagnostic is None:
        return 2
    try:
        _require(sys.platform == 'linux' and os.geteuid() == 0 and len(args.binding) <= 8192)
        binding = json.loads(args.binding)
        _require(binding['workload_mode'] == MODE
            and execution == Path('/var/lib/animemo/local-development') /
                binding['execution_inventory_digest'].removeprefix('sha256:'))
        raw = (execution / 'published-planning/subject.json').read_bytes()
        _require(_digest(raw) == binding['published_subject_digest'])
        subject = json.loads(raw)
        _require(_canonical(subject) == raw and subject['operation'] == MODE
            and subject['profile'] == args.profile
            and all(subject[key] == binding[key] for key in (
                'material_source_sha', 'material_source_tree', 'qualification_run_id', 'verified_candidate_digest')))
        context_value = os.environ['ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL']
        context = json.loads(base64.urlsafe_b64decode(context_value + '=' * (-len(context_value) % 4)))
        _require(context['profile'] == args.profile and context['initial_platform_state'] == {
            'docker_present': False, 'network_allowed': True, 'runtime_dependencies_present': False})
    except Exception as error:  # noqa: BLE001 - emit only the closed diagnostic projection.
        diagnostic_module.best_effort_fault(error, code='RUNNER_CONTEXT_INVALID')
        return 2
    started = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    plan, failure, diagnostic_status = None, None, 'NOT_REQUIRED'
    runner_started = False
    try:
        diagnostic.stage('RUNTIME_INITIALIZING')
        sys.path.insert(0, str(PRODUCT_ROOT))
        from installer.offline_python_runtime import install_wheel_runtime
        _module_origin('installer.offline_python_runtime')
        runtime = PRODUCT_ROOT.parent / 'installer-runtime'
        install_wheel_runtime(PRODUCT_ROOT / 'wheelhouse', runtime)
        sys.path.insert(1, str(runtime))
        from installer.formal_bootstrap import prepare_online_gh
        _module_origin('installer.formal_bootstrap')
        prepare_online_gh(PRODUCT_ROOT)
        diagnostic.stage('RUNTIME_READY')
        diagnostic.stage('RUNNER_STARTING')
        diagnostic.stage('RUNNER_STARTED')
        runner_started = True
        plan = observe(subject, diagnostic)
    except Exception as error:  # noqa: BLE001 - fixed DEV failure report; cancellation propagates.
        failure = 'DEVELOPMENT_PUBLISHED_PLANNING_FAILED'
        diagnostic_status = diagnostic_module.best_effort_fault(error)
        if not runner_started:
            return 2
    value = dict(schema='animemo.development-published-platform-plan/v1',
        purpose='NON_AUTHORITATIVE_DEVELOPMENT', operation=MODE, result='FAIL' if failure else 'PASS',
        binding=binding, context=context, subject=subject, platform_plan=plan, failure_code=failure,
        diagnostic_status=diagnostic_status, started_at=started,
        completed_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        platform_apply_executions=0, installer_executions=0, formal_authority_granted=False,
        candidate_acceptance_authority_granted=False, publish_authorized=False)
    value['report_digest'] = _digest(_canonical(value))
    try:
        diagnostic.stage('DRAFT_WRITING')
        with OUTPUT.open('xb') as output:
            os.chmod(OUTPUT, 0o600)
            output.write(_canonical(value))
            output.flush()
            os.fsync(output.fileno())
        diagnostic.stage('DRAFT_WRITTEN')
    except Exception as error:  # noqa: BLE001 - never replace a failed write with an accepted report.
        diagnostic_module.best_effort_fault(error, code='PROFILE_RECEIPT_INVALID')
        return 2
    # Zero describes the fixed diagnostic transport, never installation success.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
