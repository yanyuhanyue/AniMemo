"""Synthetic OS/planner fixtures; no VM, credential, crypto claim or system effects."""
import ast
import base64
import json
import shlex
import stat
import subprocess
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.canonical import canonical_json_bytes, sha256_identity
from installer import development_boundary as boundary
from installer.platform_bootstrap import ProductionPlatformBootstrap
from installer.runtime import InstallTransportSource, ReleaseSelector
from installer.tests import test_runtime as fixtures
from installer.tests.test_platform_bootstrap import qualified_existing_facts
from installer.tests.test_runtime import digest
from scripts.runtime_development_boundary import RuntimeBoundaryError, RuntimeDeadline
from updater.local_bundle import LOCAL_BUNDLE_POLICY_IDENTITY


class RuntimePlanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InstallerRuntimeTests()
        self.fixture.setUp()
        release = replace(self.fixture.releases.evidence,
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            transport_policy_identity=LOCAL_BUNDLE_POLICY_IDENTITY)
        self.fixture.releases.evidence = release
        self.synthetic_root = Path(tempfile.gettempdir()) / 'animemo-test-only-runtime'
        self.request = self.fixture.request(selector=ReleaseSelector(version=release.version),
            public_origin='https://candidate.invalid', transport_source=InstallTransportSource.LOCAL_BUNDLE,
            local_bundle_payload=self.synthetic_root / 'portable.tar',
            local_bundle_release_attestation=self.synthetic_root / 'release-attestation.json')
        self.plan = self.fixture.runtime.plan(self.request)
        self.root_socket = {'state': 'KNOWN_LOCAL', 'path': '/var/run/docker.sock',
            'resolved_path': '/run/docker.sock', 'device': 1, 'inode': 2,
            'mode': stat.S_IFSOCK | 0o660, 'uid': 0, 'gid': 999, 'ctime_ns': 3}
        socket_identity = boundary._digest({key: self.root_socket[source] for key, source in (
            ('device', 'device'), ('inode', 'inode'), ('mode', 'mode'), ('uid', 'uid'),
            ('gid', 'gid'), ('ctimeNs', 'ctime_ns'), ('resolvedPath', 'resolved_path'))})
        root_facts = replace(qualified_existing_facts(), docker_socket_identity=socket_identity)
        self.platform = ProductionPlatformBootstrap(facts_collector=lambda: root_facts,
            clock=lambda: '2026-09-29T10:00:00Z').plan(transport_source=InstallTransportSource.LOCAL_BUNDLE)
        self.binding = {'plan_digest': digest('a'), 'session_id': '1' * 32,
            'workload_mode': 'CLEAN_PREACCEPTANCE', 'runtime_offline_only': True,
            'execution_source_sha': '2' * 40, 'execution_source_tree': '3' * 40,
            'execution_inventory_digest': digest('4'), 'verified_candidate_digest': digest('5'),
            'material_source_sha': '6' * 40, 'material_source_tree': '7' * 40,
            'qualification_run_id': 1, 'runtime_trust_selection_digest': digest('8'),
            'candidate_input_digest': digest('9')}
        self.clock = [1000000., 10.]
        self.lifetime = RuntimeDeadline(binding=self.binding,
            authorization_expires_utc_seconds=self.clock[0] + 3600,
            wall_clock=lambda: self.clock[0], monotonic_clock=lambda: self.clock[1])
        self.body = boundary.build_runtime_operation_boundary(binding=self.binding,
            baseline={'baseline_digest': digest('b'), 'guest_identity': {
                'machine_id': 'c' * 32, 'boot_id': '11111111-2222-3333-4444-555555555555',
                'nonce': 'd' * 64, 'clone_identity': digest('e'), 'session_id': self.binding['session_id']},
                'observation': {'tools': {name: {'path': path, 'resolved_path': path, 'sha256': digest('e')}
                    for name, path in [('python', '/usr/bin/python3'), ('docker', '/usr/bin/docker'),
                        ('systemctl', '/usr/bin/systemctl'), ('dpkg', '/usr/bin/dpkg'),
                        ('dpkg-query', '/usr/bin/dpkg-query'), ('pg_dump', '/usr/bin/pg_dump'),
                        ('psql', '/usr/bin/psql'), ('compose', '/usr/libexec/docker/cli-plugins/docker-compose')]},
                    'docker_socket': {'state': 'PERMISSION_UNKNOWN', 'path': '/var/run/docker.sock'},
                    'docker_daemon': {'state': 'PERMISSION_UNKNOWN', 'identity': None}}},
            lifetime=self.lifetime.guest_envelope(), material={'selection_digest': digest('8'),
                'release': release.as_dict(), 'images': {role: digest(str(index))
                    for index, role in enumerate(('api', 'web', 'postgres', 'redis'), 1)}})
        self.binding.update(runtime_operation_boundary=self.body,
            runtime_lifetime=self.lifetime.guest_envelope())

    def redigest(self, plan):
        return replace(plan, plan_digest=sha256_identity(canonical_json_bytes(plan.body())))

    def gate_without_trust(self):
        # No production constructor accepts this explicit synthetic root fixture.
        # It stays unbound: all execution attempts must fail before the sink.
        gate = object.__new__(boundary.DevelopmentExecutionGate)
        gate._binding, gate._boundary, gate._lifetime = self.binding, self.body, self.lifetime
        gate._trust = gate._approved = gate._approved_plan = None
        gate._attempted = gate._closed = False
        gate._authority_fd = None
        gate._root_observation = {'uid': 0, 'euid': 0, 'platform': 'linux',
            'machine_id': self.body['guest_identity']['machine_id'],
            'boot_id': self.body['guest_identity']['boot_id'],
            'docker_storage_root': '/var/lib/docker', 'platform_plan_digest': self.platform.plan_digest,
            'baseline_facts': {'tools': self.body['baseline_observation']['tools'], 'docker_socket': self.root_socket,
                'docker_daemon': {'state': 'OBSERVED', 'identity': digest('c'),
                    'server_version': '28.2.2', 'storage_root': '/var/lib/docker'}}}
        gate._root_platform_plan = self.platform
        return gate

    def test_real_planners_export_complete_safe_expression_with_no_effects(self):
        value = boundary.full_operation_plan(self.body, self.platform, self.plan)
        self.assertEqual(value['installer_plan'], self.plan.as_dict())
        self.assertEqual(value['platform_plan'], self.platform.as_dict())
        self.assertEqual(len(value['effects']), 10)
        self.assertEqual(value['resources']['system_packages'], [])
        self.assertEqual(value['resources']['docker_storage_root'], '/var/lib/docker')
        self.assertEqual(self.fixture.fresh.calls, [])
        self.assertEqual(self.fixture.operations.events, [])
        self.assertEqual(value, json.loads(boundary._canonical(value)))

    def test_unknown_step_rejected_even_with_valid_recomputed_plan_digest(self):
        changed = self.redigest(replace(self.plan, execution_steps=(*self.plan.execution_steps, 'packages.install')))
        with self.assertRaisesRegex(boundary.DevelopmentBoundaryError, 'OUT_OF_SCOPE'):
            boundary.full_operation_plan(self.body, self.platform, changed)
        self.assertEqual(self.fixture.fresh.calls, [])

    def test_other_instance_origin_listen_source_or_target_rejected(self):
        from durability.instance import InstanceName
        from installer.runtime import TargetClass, TargetEvidence
        changes = [replace(self.plan, instance_name=InstanceName('other')),
            replace(self.plan, configuration=replace(self.plan.configuration, public_origin='https://else.invalid')),
            replace(self.plan, configuration=replace(self.plan.configuration, listen_port=9999)),
            replace(self.plan, release=replace(self.plan.release, manifest_digest=digest('f'))),
            replace(self.plan, target=TargetEvidence(TargetClass.FOREIGN, digest('f')))]
        for plan in changes:
            with self.subTest(plan=plan), self.assertRaisesRegex(boundary.DevelopmentBoundaryError, 'OUT_OF_SCOPE'):
                boundary.full_operation_plan(self.body, self.platform, self.redigest(plan))

    def test_online_platform_and_unknown_package_actions_rejected(self):
        online = ProductionPlatformBootstrap(facts_collector=qualified_existing_facts,
            clock=lambda: '2026-09-29T10:00:00Z').plan(transport_source=InstallTransportSource.GITHUB)
        with self.assertRaises(boundary.DevelopmentBoundaryError):
            boundary.full_operation_plan(self.body, online, self.plan)

    def test_changed_boundary_resources_preparation_or_fields_rejected(self):
        for key, value in [('resources', {**self.body['resources'], 'docker_socket': 'tcp://else:2375'}),
                           ('preparation_writes', ['/etc']), ('unknown', True)]:
            changed = {**self.body, key: value}
            changed['boundary_digest'] = boundary._digest({k: v for k, v in changed.items() if k != 'boundary_digest'})
            with self.subTest(key=key), self.assertRaises(boundary.DevelopmentBoundaryError):
                boundary.validate_runtime_operation_boundary(changed, self.binding)

    def test_preparation_rejects_sibling_traversal_and_system_target(self):
        boundary.require_preparation_binding(self.binding, boundary.preparation_paths(self.binding))
        boundary.require_root_preparation_binding(self.binding, boundary.preparation_paths(self.binding))
        for path in ['/etc', '/var/lib/animemo/local-development',
                     '/var/lib/animemo/local-development/runtime-cache/else',
                     '/var/lib/animemo/local-development/python-runtime/../escape']:
            with self.subTest(path=path), self.assertRaises(boundary.DevelopmentBoundaryError):
                boundary.require_preparation_binding(self.binding, [path])
            with self.subTest(path=path), self.assertRaises(boundary.DevelopmentBoundaryError):
                boundary.require_root_preparation_binding(self.binding, [path])

    def test_first_actual_platform_planner_uses_lifetime_clipped_observer(self):
        from installer import platform_bootstrap as platform
        gate = self.gate_without_trust()
        self.clock[0] += 3250
        self.clock[1] += 3250
        calls = []
        def sink(_runner, argv, *, timeout, environment):
            calls.append((argv, timeout))
            return platform.PlatformCommandResult(0, b'SYNTHETIC_DOCKER_VERSION')
        def observed_os(runner):
            runner.run(('/usr/bin/docker', '--version'), timeout=300, environment={})
            return self.platform.initial_capabilities
        with (mock.patch.object(platform.SubprocessPlatformCommandRunner, 'run', sink),
              mock.patch.object(platform, 'collect_bootstrap_host_facts', side_effect=observed_os)):
            value = boundary.observe_root_platform_plan(gate)
            self.assertEqual(value.mode.value, 'OFFLINE_VALIDATE_ONLY')
            self.assertEqual(calls, [(('/usr/bin/docker', '--version'), 50.0)])
            self.clock[0] += 51
            self.clock[1] += 51
            with self.assertRaises(RuntimeBoundaryError):
                boundary.observe_root_platform_plan(gate)
            self.assertEqual(len(calls), 1)

    def test_public_json_cannot_construct_gate_or_bind_trust(self):
        with self.assertRaises(TypeError):
            boundary.DevelopmentExecutionGate(self.body)
        gate = self.gate_without_trust()
        with self.assertRaises(boundary.DevelopmentBoundaryError):
            gate.bind_trust({'verified': True, 'purpose': 'TEST_ONLY'})
        with self.assertRaises(boundary.DevelopmentBoundaryError):
            gate.approve(self.platform, self.plan)

    def test_lower_runtime_execute_rejects_no_trust_and_sink_stays_zero(self):
        self.fixture.runtime._development_execution_gate = self.gate_without_trust()
        with self.assertRaisesRegex(boundary.DevelopmentBoundaryError, 'AUTHORITY_REQUIRED'):
            self.fixture.runtime.execute(self.plan, accepted_plan_digest=self.plan.plan_digest)
        self.assertEqual(self.fixture.fresh.calls, [])
        self.assertEqual(self.fixture.operations.events, [])

    def test_lower_constructor_cannot_omit_gate_for_single_runtime_service(self):
        from installer.runtime import Installer, InstallerError
        self.fixture.fresh._development_service_source = SimpleNamespace(runtime_offline_only=True)
        with self.assertRaisesRegex(InstallerError, 'DEVELOPMENT_GATE_REQUIRED'):
            Installer(releases=self.fixture.releases, target=self.fixture.target,
                platform=self.fixture.platform, compatibility=self.fixture.compatibility,
                configuration=self.fixture.configuration, operations=self.fixture.operations,
                fresh=self.fixture.fresh, restore=self.fixture.restore,
                bootstrap_privilege_gate=self.fixture.bootstrap_gate)

    def test_deadline_exhaustion_rejects_preparation_before_effect(self):
        gate = self.gate_without_trust()
        self.clock[0] += 3301
        self.clock[1] += 3301
        with self.assertRaises(RuntimeBoundaryError):
            gate.require_preparation(boundary.preparation_paths(self.binding))
        self.assertEqual(self.fixture.fresh.calls, [])

    def test_result_consumer_rejects_rehashed_removed_privilege_scope(self):
        from installer.runtime import InstallOutcome
        expression = boundary.full_operation_plan(self.body, self.platform, self.plan)
        observation = {'schema': 'animemo.runtime-development-plan-consumption/v1',
            'boundary_digest': self.body['boundary_digest'], 'session_id': self.binding['session_id'],
            'plan': expression, 'executions': 1,
            'root_observation': self.gate_without_trust()._root_observation}
        output = {'installerPlanDigest': self.plan.plan_digest, 'platformPlan': self.platform.as_dict()}
        output['installerResult'] = self.fixture.runtime._result(self.plan, InstallOutcome.SUCCEEDED,
            state='succeeded', reason_code='INSTALL_FRESH_SUCCEEDED', completed_steps=self.plan.execution_steps).as_dict()
        boundary.validate_operation_result(observation, self.binding, output)
        expression['effects'] = []
        expression['operation_plan_digest'] = boundary._digest({k: v for k, v in expression.items()
            if k != 'operation_plan_digest'})
        with self.assertRaises(boundary.DevelopmentBoundaryError):
            boundary.validate_operation_result(observation, self.binding, output)

    def test_entry_rejection_stops_before_return_and_never_calls_execute(self):
        from scripts.development_installer_entry import run_runtime_composition
        gate = self.gate_without_trust()
        stop_calls = []
        composition = SimpleNamespace(runtime=self.fixture.runtime,
            plan_platform=lambda *args: SimpleNamespace(plan=self.platform),
            stop_development_runtime=lambda plan: stop_calls.append(plan) or {'result': 'NOT_STARTED'})
        with mock.patch.object(self.fixture.runtime, 'execute') as execute:
            value, code = run_runtime_composition(composition=composition, request=self.request,
                gate=gate, service_source=None, diagnostic=mock.Mock())
            execute.assert_not_called()
        self.assertEqual(code, 5)
        self.assertEqual(stop_calls, [None])
        self.assertEqual(value['developmentInstanceStop'], {'result': 'NOT_STARTED'})

    def test_entry_keeps_primary_and_stop_failure_receipt(self):
        from scripts.development_installer_entry import run_runtime_composition
        gate = self.gate_without_trust()
        error = RuntimeError('synthetic stop failure')
        error.receipt = {'result': 'FAIL', 'fixture': 'SYNTHETIC_STOP'}
        composition = SimpleNamespace(runtime=self.fixture.runtime,
            plan_platform=lambda *args: SimpleNamespace(plan=self.platform),
            stop_development_runtime=mock.Mock(side_effect=error))
        value, code = run_runtime_composition(composition=composition, request=self.request,
            gate=gate, service_source=None, diagnostic=mock.Mock())
        self.assertEqual(code, 5)
        self.assertEqual(value['reasonCode'], 'DEVELOPMENT_TRUST_AUTHORITY_REQUIRED')
        self.assertEqual(value['developmentInstanceStop'], error.receipt)
        self.assertEqual(value['secondaryErrors'], ['DEVELOPMENT_INSTANCE_STOP_FAILED'])

    def test_root_channel_acquisition_rejects_wrong_host_before_file_or_daemon(self):
        with (mock.patch.object(boundary.os, 'name', 'nt'),
              self.assertRaises(boundary.DevelopmentBoundaryError)):
            boundary.acquire_development_execution_gate(self.binding)

    def test_runtime_report_consumes_public_bundle_identity_distinct_from_candidate_input(self):
        from datetime import datetime, timezone

        from installer.development_lifecycle import SCHEMA as stop_schema
        from installer.platform_bootstrap import _receipt
        from installer.runtime import InstallOutcome
        from scripts import development_profile_runner as development
        from scripts.tests import test_candidate_profile_runner as reports
        loaded = reports._loaded(self.synthetic_root / 'materials')
        loaded.images.images = tuple(SimpleNamespace(role=role, digest=digest_value)
            for role, digest_value in self.body['material']['images'].items())
        output = reports._installer_output()
        output['platformPlan'] = self.platform.as_dict()
        output['platformBootstrapReceipt'] = _receipt(self.platform, self.platform.initial_capabilities).as_dict()
        output['installerPlanDigest'] = self.plan.plan_digest
        output['installerResult'] = self.fixture.runtime._result(self.plan, InstallOutcome.SUCCEEDED,
            state='succeeded', reason_code='INSTALL_FRESH_SUCCEEDED',
            completed_steps=self.plan.execution_steps).as_dict()
        observation = output['productionExecutionObservation']
        observation['completedSteps'] = list(self.plan.execution_steps)
        observation['networkObservation']['platformPlanDigest'] = self.platform.plan_digest
        egress = observation['networkObservation']['egressIsolation']
        egress.update(service='animemo-updater@default.service', containerNetwork='animemo-default_animemo')
        egress['receiptDigest'] = reports._identity({key: item for key, item in egress.items() if key != 'receiptDigest'})
        observation['doctorReport']['instanceId'] = self.plan.configuration.instance_id
        observation['doctorReceiptDigest'] = reports._identity(observation['doctorReport'])
        observation['doctorExecutionIdentity'] = reports._identity({
            'canonicalAcceptanceReceiptDigests': [item['receiptDigest'] for item in observation['canonicalAcceptanceTests']],
            'completedSteps': list(self.plan.execution_steps),
            'doctorReceiptDigest': observation['doctorReceiptDigest'],
            'installerExecutionReceiptDigest': reports._identity(output['installerResult'])})
        image_receipt = observation['imageAcquisitionReceipt']
        image_receipt['verifiedReleaseIdentity'] = self.plan.release.material_identity_digest
        self.assertNotEqual(image_receipt['verifiedReleaseIdentity'], loaded.materials.identity_digest)
        for image in image_receipt['images']:
            image['canonicalReference'] = 'example.invalid/' + image['role'] + '@' + self.body['material']['images'][image['role']]
            image['observedReference'] = image['canonicalReference']
        observation['imageAcquisitionReceiptDigest'] = reports._identity(image_receipt)
        observation['imageRuntimeReadbackReceipt']['images'] = image_receipt['images']
        observation['imageRuntimeReadbackReceiptDigest'] = reports._identity(observation['imageRuntimeReadbackReceipt'])
        output['developmentOperationPlan'] = {'schema': 'animemo.runtime-development-plan-consumption/v1',
            'boundary_digest': self.body['boundary_digest'], 'session_id': self.binding['session_id'],
            'plan': boundary.full_operation_plan(self.body, self.platform, self.plan), 'executions': 1,
            'root_observation': self.gate_without_trust()._root_observation}
        stop = {'schema': stop_schema, 'sessionId': self.binding['session_id'],
            'parentPlanDigest': self.binding['plan_digest'], 'installerPlanDigest': self.plan.plan_digest,
            'instanceName': 'default', 'composeProject': 'animemo-default',
            'updaterService': 'animemo-updater@default.service', 'containers': [],
            'serviceStopped': True, 'listenerClosed': True, 'dataRetention': {'state': 'RETAINED',
                'identity': {'path': '/data/animemo-instances/default', 'device': 1, 'inode': 2}},
            'result': 'PASS', 'failures': []}
        stop['receiptDigest'] = development.sha256_bytes(development.canonical_json_bytes(stop))
        output['developmentInstanceStop'] = stop
        service = {'execution_inventory_digest': self.binding['execution_inventory_digest']}
        output['developmentServiceSourceObservation'] = service
        context = reports._context()
        report = {'schema': development.SCHEMA, 'purpose': development.PURPOSE,
            'result': 'PASS', 'binding': self.binding, 'context': context, 'installer_output': output,
            'started_at': datetime.fromtimestamp(self.clock[0], timezone.utc).isoformat().replace('+00:00', 'Z'),
            'completed_at': datetime.fromtimestamp(self.clock[0] + 1, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'candidate_acceptance_authority_granted': False, 'publish_authorized': False}
        def seal():
            report['report_digest'] = development.sha256_bytes(development.canonical_json_bytes(
                {key: value for key, value in report.items() if key != 'report_digest'}))
        seal()
        development.validate_development_report(report, loaded=loaded, expected_binding=self.binding,
            expected_context=context, expected_service_source=service)
        # Original Candidate remains bound to its own original input identity.
        with self.assertRaisesRegex(reports.runner.ProfileRunnerError, 'IMAGE_OBSERVATION_MISMATCH'):
            reports.runner.build_profile_receipt(loaded=loaded, profile='RUNTIME_BASE_OFFLINE',
                context=context, installer_output=output, started_at=report['started_at'], completed_at=report['completed_at'])
        image_receipt['verifiedReleaseIdentity'] = loaded.materials.identity_digest
        observation['imageAcquisitionReceiptDigest'] = reports._identity(image_receipt)
        seal()
        with self.assertRaisesRegex(reports.runner.ProfileRunnerError, 'IMAGE_OBSERVATION_MISMATCH'):
            development.validate_development_report(report, loaded=loaded, expected_binding=self.binding,
                expected_context=context, expected_service_source=service)

    def test_synthetic_trust_boundary_full_entry_executes_effect_sink_once_and_stops(self):
        from scripts import runtime_development_boundary as root_observer
        from scripts.development_installer_entry import run_runtime_composition
        gate = self.gate_without_trust()
        # Internal state-machine fixture only; no cryptographic function or
        # output is replaced and no production trust issuer is invoked.
        gate._trust = SimpleNamespace(verify_current=lambda: None)
        self.fixture.runtime._development_execution_gate = gate
        events = []
        composition = SimpleNamespace(runtime=self.fixture.runtime,
            plan_platform=lambda *args: SimpleNamespace(plan=self.platform),
            execute_platform=lambda *args: SimpleNamespace(as_dict=lambda: {'result': 'SYNTHETIC_VALIDATE_ONLY'}),
            candidate_profile_execution_observation=lambda **kwargs: {'fixture': 'EFFECT_SINK_ONLY'},
            stop_development_runtime=lambda plan: events.append(('stop', plan.operation_id)) or {'result': 'SYNTHETIC_STOPPED'})
        source = SimpleNamespace(observe_installed=lambda: {'fixture': 'SYNTHETIC_SOURCE'})
        with mock.patch.object(root_observer, 'observe_runtime_baseline_facts',
                return_value=gate._root_observation['baseline_facts']):
            value, code = run_runtime_composition(composition=composition, request=self.request,
                gate=gate, service_source=source, diagnostic=mock.Mock())
        self.assertEqual(code, 0, value)
        self.assertEqual(self.fixture.fresh.calls, ['roots', 'config', 'release', 'services',
            'migration', 'bootstrap', 'runtime', 'validate', 'adopt', 'doctor'])
        self.assertEqual(events, [('stop', gate.approved_plan.operation_id)])
        self.assertEqual(value['developmentOperationPlan']['executions'], 1)
        boundary.validate_operation_result(value['developmentOperationPlan'], self.binding, value)
        calls = list(self.fixture.fresh.calls)
        with self.assertRaisesRegex(boundary.DevelopmentBoundaryError, 'PERMISSION_REQUIRED'):
            self.fixture.runtime.execute(gate.approved_plan, accepted_plan_digest=gate.approved_plan.plan_digest)
        self.assertEqual(calls, self.fixture.fresh.calls)

    def test_actual_root_constructor_compiles_and_roundtrips_with_fixed_windows_budget(self):
        from installer import development_trust
        from scripts import development_guest_session as guest
        from scripts.development_plan import from_material_plan
        from scripts.tests.test_local_candidate_development import DevelopmentPlanTests
        fixture = DevelopmentPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = from_material_plan(fixture.fixture.plan,
            execution_source_sha='a' * 40, execution_source_tree='b' * 40,
            execution_inventory_digest=digest('c'), runtime_offline_only=True,
            runtime_authorization_deadline='2026-10-02T15:12:20Z',
            runtime_trust_selection_digest=digest('8'), runtime_retention_policy='STOP_AND_RETAIN')
        provider = fixture.fixture.provider
        provider._candidate_material_authority = SimpleNamespace(tree_inventory_identity=digest('d'))
        provider._runtime_development_material = SimpleNamespace(guest_inventory_digest=digest('e'))
        with (mock.patch.object(guest, 'require_development_source', return_value=SimpleNamespace(
                root=Path(guest.__file__).resolve().parents[1])),
              mock.patch.object(development_trust, 'runtime_material_boundary', return_value=self.body['material'])):
            program = guest._root_program(provider, plan, plan.profiles[0], _construction_only=True)
        compile(program, '<synthetic-runtime-root-construction>', 'exec')
        command = guest.c._remote_workload_command(program, digest('e'))
        parsed = ast.parse(shlex.split(command)[4])
        encoded = next(ast.literal_eval(node.args[0]) for node in ast.walk(parsed)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'b64decode')
        transport = zlib.decompress(base64.b64decode(encoded)).decode('utf-8')
        compile(transport, '<synthetic-runtime-transport>', 'exec')
        argv = next(ast.literal_eval(node.args[0]) for node in ast.walk(ast.parse(transport))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'Popen')
        self.assertEqual(argv[-1].encode('utf-8'), program.encode('utf-8'))
        authority = provider._active_profile_authority(plan.profiles[0], plan)
        authority = replace(authority,
            identity_file=authority.identity_file.parent / '中文密钥 空格',
            known_hosts_file=authority.known_hosts_file.parent / '中文主机 空格')
        resolved_argv = provider._ssh_argv(authority, command)
        units = len(subprocess.list2cmdline(resolved_argv).encode('utf-16-le')) // 2 + 1
        self.assertLessEqual(units, 32767, f'root={len(program.encode("utf-8"))} remote={len(command)} units={units}')
        self.assertGreaterEqual(32767 - units, 1024,
            f'Expected headroom for the final observed identities: units={units}')
        budget = guest.c.validate_workload_command_budget(resolved_argv)
        self.assertLessEqual(len(program.encode('utf-8')) + 1, guest.c.MAX_ROOT_PROGRAM_BYTES)
        self.assertLessEqual(budget['windows_command_utf16_units_including_nul'], 32767)
        # Embedded public code remains parseable under Windows' native CP936;
        # no process-global UTF8 mode is changed to make the constructor pass.
        self.assertEqual(program.encode('gbk').decode('gbk'), program)


if __name__ == '__main__':
    unittest.main()
