"""Fixed local Installer composition with an independently sealed service tree."""
import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from installer import cli
from installer.development import (
    DevelopmentServiceError,
    acquire_development_service_source,
)
from installer.runtime import InstallerError
from release.materials import reject_duplicate_json_keys
from scripts.candidate_diagnostics import inherited_writer
from scripts.development_profile_runner import validate_binding


def run_runtime_composition(*, composition, request, gate, service_source, diagnostic):
    """One role owns planning, conditional execution, acceptance and stop."""
    from installer.development_boundary import DevelopmentBoundaryError
    value, code, primary = None, 5, None
    try:
        gate.check_live('RUNTIME_PLATFORM_PLAN')
        session = composition.plan_platform(request,
            datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        # Both actual planners run before the one-use permission is issued.
        plan = composition.runtime.plan(request)
        gate.approve(session.plan, plan)
        gate.require_preparation(('/run/lock/animemo-platform-bootstrap.lock',))
        platform_receipt = composition.execute_platform(session, session.plan.plan_digest)
        diagnostic.stage('INSTALLER_RUNNING')
        result = composition.runtime.execute(plan, accepted_plan_digest=plan.plan_digest)
        diagnostic.stage('INSTALLER_COMPLETED')
        observation = composition.candidate_profile_execution_observation(
            platform_plan=session.plan, platform_receipt=platform_receipt,
            installer_plan=plan, installer_result=result)
        value = {'mode': 'EXECUTE', 'profile': 'OFFLINE_VALIDATE_ONLY',
            'platformPlan': session.plan.as_dict(),
            'platformBootstrapReceipt': platform_receipt.as_dict(),
            'strictPostProvisionQualification': True,
            'installerPlanDigest': plan.plan_digest,
            'installerResult': result.as_dict(),
            'productionExecutionObservation': observation,
            'developmentOperationPlan': gate.result_observation(),
            'developmentServiceSourceObservation': service_source.observe_installed(),
            'releaseAuthorityGranted': False, 'publishAuthorized': False}
        code = 0 if result.outcome.value == 'SUCCEEDED' else 5
    except BaseException as error:  # noqa: BLE001 - cancellation must still reach instance stop.
        primary = error
        value = {'outcome': 'ENVIRONMENT_FAILED', 'reasonCode':
            getattr(error, 'code', None) or (str(error) if isinstance(error, DevelopmentBoundaryError)
                else 'DEVELOPMENT_INSTALLER_ENTRY_FAILED')}
    finally:
        try:
            # This must precede the root process/role ending, including failure.
            stop = composition.stop_development_runtime(gate.approved_plan)
            value['developmentInstanceStop'] = stop
        except BaseException as error:  # noqa: BLE001 - preserve primary failure and stop evidence.
            if type(getattr(error, 'receipt', None)) is dict:
                value['developmentInstanceStop'] = error.receipt
            value['secondaryErrors'] = ['DEVELOPMENT_INSTANCE_STOP_FAILED'] if primary else []
            if primary is None:
                value.update(outcome='ENVIRONMENT_FAILED', reasonCode='DEVELOPMENT_INSTANCE_STOP_FAILED')
            code = 5
            diagnostic.error('INSTALLER_EXECUTION_FAILED')
    return value, code


def _runtime_inputs(binding, service_source):
    from installer.development_boundary import acquire_development_execution_gate
    from installer.development_trust import (
        consume_runtime_local_bundle,
        load_runtime_trust_inputs,
    )
    from installer.runtime import (
        InstallerMode,
        InstallRequest,
        InstallTransportSource,
        ReleaseSelector,
    )
    gate = acquire_development_execution_gate(binding)
    root = Path('/var/lib/animemo/local-development/runtime-inputs') / binding['runtime_trust_selection_digest'][7:]
    inputs = None
    try:
        inputs = load_runtime_trust_inputs(root, expected_digest=binding['runtime_trust_selection_digest'], binding=binding)
        authority = consume_runtime_local_bundle(inputs, service_source=service_source,
            preparation_gate=gate, binding=binding)
        gate.bind_trust(authority)
        request = InstallRequest(mode=InstallerMode.FRESH,
            selector=ReleaseSelector(version=authority.material_boundary['release']['version']),
            public_origin=gate.boundary['resources']['public_origin'],
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            local_bundle_payload=root / 'media/portable.tar',
            local_bundle_release_attestation=root / 'media/release-attestation.json',
            non_interactive=True)
        return gate, authority, request
    except BaseException:
        if inputs is not None:
            inputs.close()
        gate.close()
        raise


class _BoundedJsonOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.count = 0

    def write(self, value):
        self.count += len(value.encode('utf-8'))
        if self.count > 8 * 1024 * 1024:
            raise ValueError('DEVELOPMENT_INSTALLER_OUTPUT_LIMIT')
        return super().write(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--profile', required=True,
        choices=('ONLINE_FRESH', 'ONLINE_EXISTING_DOCKER', 'OFFLINE_VALIDATE_ONLY'))
    parser.add_argument('--public-origin', required=True)
    arguments = parser.parse_args(argv)
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    composition = None
    runtime_authority = None
    runtime_gate = None
    try:
        if len(arguments.binding.encode('utf-8')) > 128 * 1024:
            raise DevelopmentServiceError()
        binding = validate_binding(json.loads(arguments.binding, object_pairs_hook=reject_duplicate_json_keys))
        if binding.get('runtime_offline_only') and arguments.profile != 'OFFLINE_VALIDATE_ONLY':
            raise DevelopmentServiceError()
        service_source = acquire_development_service_source(binding)
        if binding.get('runtime_offline_only'):
            from installer.production import build_runtime_development_composition
            gate, runtime_authority, request = _runtime_inputs(binding, service_source)
            runtime_gate = gate
            composition = build_runtime_development_composition(
                _development_service_source=service_source,
                runtime_development_gate=gate,
                runtime_development_materials=runtime_authority)
            value, code = run_runtime_composition(composition=composition, request=request,
                gate=gate, service_source=service_source, diagnostic=diagnostic)
            print(json.dumps(value, sort_keys=True))
            return code
        args = cli._parser().parse_args(['candidate', '--verified-candidate-digest', binding['verified_candidate_digest'],
            '--profile', arguments.profile, '--public-origin', arguments.public_origin, '--execute', '--accept', '--json'])
        request = cli._candidate_request(args)
        from installer.production import build_candidate_composition
        composition = build_candidate_composition(binding['verified_candidate_digest'],
            profile=arguments.profile, instance_name=request.instance_name,
            _development_service_source=service_source)
        output = _BoundedJsonOutput()
        with redirect_stdout(output):
            code = cli._run_candidate_composition(args, request, composition, diagnostic)
        value = json.loads(output.getvalue(), object_pairs_hook=reject_duplicate_json_keys)
        if code == 0:
            value['developmentServiceSourceObservation'] = service_source.observe_installed()
        print(json.dumps(value, sort_keys=True))
        return code
    except InstallerError as error:
        print(json.dumps({'outcome': error.outcome.value, 'reasonCode': error.code}))
        return cli._exit_code(error)
    except DevelopmentServiceError:
        diagnostic.error('DEVELOPMENT_SERVICE_SOURCE_MISMATCH')
        print(json.dumps({'outcome': 'ENVIRONMENT_FAILED', 'reasonCode': 'DEVELOPMENT_SERVICE_SOURCE_MISMATCH'}))
        return 5
    except BaseException:
        diagnostic.error('DEVELOPMENT_INSTALLER_ENTRY_FAILED')
        print(json.dumps({'outcome': 'ENVIRONMENT_FAILED', 'reasonCode': 'DEVELOPMENT_INSTALLER_ENTRY_FAILED'}))
        return 5
    finally:
        try:
            if composition is not None:
                composition.close_candidate_runtime()
        finally:
            try:
                if runtime_authority is not None:
                    runtime_authority.close()
            finally:
                if runtime_gate is not None:
                    runtime_gate.close()


if __name__ == '__main__':
    raise SystemExit(main())
