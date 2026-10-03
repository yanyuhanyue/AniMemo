"""Deterministic public UID/clock/transport fixtures, no native capture or Guest."""
import base64
import copy
import json
import shlex
import threading
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import runtime_development_boundary as boundary


class Clock:
    def __init__(self):
        self.wall, self.mono = 2_000_000_000.0, 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def binding():
    return {'plan_digest': 'sha256:' + '1' * 64, 'session_id': '2' * 32,
        'execution_source_sha': '3' * 40, 'execution_source_tree': '4' * 40,
        'execution_inventory_digest': 'sha256:' + '5' * 64,
        'candidate_input_digest': 'sha256:' + '6' * 64,
        'verified_candidate_digest': 'sha256:' + '7' * 64,
        'runtime_trust_selection_digest': 'sha256:' + '8' * 64,
        'profile': 'RUNTIME_BASE_OFFLINE', 'snapshot_identity': 'sha256:' + '9' * 64,
        'clone_identity': 'sha256:' + 'a' * 64, 'nonce': 'b' * 64,
        'machine_id': 'c' * 32, 'boot_id': 'dddddddd-dddd-dddd-dddd-dddddddddddd'}


def baseline_fixture():
    identity = binding()
    tools = {name: {'path': path, 'resolved_path': path, 'sha256': 'sha256:' + 'e' * 64}
        for name, path in (('python', '/usr/bin/python3'), ('docker', '/usr/bin/docker'),
            ('systemctl', '/usr/bin/systemctl'), ('dpkg', '/usr/bin/dpkg'), ('dpkg-query', '/usr/bin/dpkg-query'),
            ('pg_dump', '/usr/bin/pg_dump'), ('psql', '/usr/bin/psql'),
            ('compose', '/usr/libexec/docker/cli-plugins/docker-compose'))}
    commands = [('/usr/bin/systemctl', 'is-active', '--quiet', 'docker'),
        ('/usr/bin/systemctl', 'show', '--property=MainPID', '--property=ExecMainStartTimestampMonotonic', '--value', 'docker'),
        ('/usr/bin/dpkg', '--print-architecture'), ('/usr/bin/docker', '--version'),
        ('/usr/bin/systemctl', '--version'),
        ('/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'compose', 'version'),
        ('/usr/bin/pg_dump', '--version'), ('/usr/bin/psql', '--version')]
    commands.extend(('/usr/bin/dpkg-query', '--show',
        '--showformat=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n', package)
        for package in ('docker.io', 'docker-compose-v2', 'postgresql-client-16'))
    return {'schema': 'animemo.runtime-unprivileged-baseline/v1', 'binding': identity,
        'machine_id': identity['machine_id'], 'boot_id': identity['boot_id'],
        'uid': 1000, 'euid': 1000, 'platform': 'linux', 'architecture': 'x86_64',
        'distribution': {'ID': 'ubuntu', 'VERSION_ID': '24.04'},
        'observed_utc_seconds': 2_000_000_000.0, 'tools': tools, 'docker_service_active': True,
        'docker_socket': {'state': 'PERMISSION_UNKNOWN', 'path': '/var/run/docker.sock'},
        'docker_daemon': {'state': 'PERMISSION_UNKNOWN', 'identity': None}, 'package_architecture': 'amd64',
        'packages': {name: {'version': '16.1', 'architecture': 'amd64'}
                     for name in ('docker.io', 'docker-compose-v2', 'postgresql-client-16')},
        'versions': {'docker': 'Docker version 26.1', 'systemctl': 'systemd 255',
            'compose': 'Docker Compose version v2.24', 'pg_dump': 'pg_dump (PostgreSQL) 16.4',
            'psql': 'psql (PostgreSQL) 16.4'},
        'command_results': [{'argv': list(argv), 'returncode': 0,
            'started_at': '2033-05-18T03:33:19Z', 'ended_at': '2033-05-18T03:33:20Z',
            'stdout_sha256': 'sha256:' + 'e' * 64, 'stderr_sha256': 'sha256:' + 'f' * 64}
            for argv in commands], 'hard_missing': [],
        'root_observations_pending': ['effective_uid_zero', 'docker_daemon_identity',
            'docker_daemon_access', 'current_full_installer_plan', 'instance_resource_ownership']}


class RuntimeDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def lifetime(self, seconds=20000):
        return boundary.RuntimeDeadline(binding=binding(),
            authorization_expires_utc_seconds=self.clock.wall + seconds,
            wall_clock=lambda: self.clock.wall, monotonic_clock=lambda: self.clock.mono)

    def test_boot_budget_starts_now_and_does_not_refresh_at_baseline_or_reconnect(self):
        lifetime = self.lifetime()
        self.assertEqual(lifetime.check('BOOT'), 14400 - 300)
        original = lifetime.deadline
        self.clock.advance(120)
        self.assertEqual(lifetime.check('BASELINE'), 13980)
        envelope = lifetime.guest_envelope()
        self.assertEqual(lifetime.deadline, original)
        self.assertNotIn('monotonic_deadline', envelope)
        self.assertEqual(envelope['remaining_seconds'], 14280)
        guest = boundary.RuntimeDeadline.from_guest_envelope(envelope, binding=binding(),
            wall_clock=lambda: self.clock.wall + 5, monotonic_clock=lambda: 2.0)
        self.assertEqual(guest.check('ROOT'), 13975)
        self.assertEqual(guest.boot_started_utc_seconds, 2_000_000_000.0)

    def test_before_boot_expiry_and_insufficient_capture_budget_reject(self):
        for seconds in (-1, 0, 299, 300):
            with self.subTest(seconds=seconds), self.assertRaises(boundary.RuntimeBoundaryError):
                self.lifetime(seconds)
        lifetime = self.lifetime(430)
        self.assertEqual(lifetime.clip_timeout(500), 130)
        self.clock.advance(11)
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'BUDGET_EXHAUSTED'):
            lifetime.check('CAPTURE', minimum_seconds=120)
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'CLOSED'):
            lifetime.check('RECONNECT')

    def test_root_plan_execute_and_stop_reserve_share_deadline(self):
        lifetime = self.lifetime(900)
        self.clock.advance(599)
        self.assertEqual(lifetime.clip_timeout(1800), 1)
        self.clock.advance(1)
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'BUDGET_EXHAUSTED'):
            lifetime.check('EXECUTE')
        self.assertEqual(lifetime.clip_timeout(600, cleanup=True), 600)
        self.clock.advance(301)
        self.assertEqual(lifetime.clip_timeout(600, cleanup=True), 299)
        self.clock.advance(299)
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'CLEANUP_BUDGET_EXHAUSTED'):
            lifetime.check('STOP', cleanup=True)
        lifetime.close('ERROR')
        self.assertEqual(lifetime.record['close_observation']['overrun_seconds'], 300)

    def test_outer_workload_allows_instance_stop_then_reserves_vm_soft_stop(self):
        lifetime = self.lifetime(900)
        self.assertEqual(lifetime.clip_timeout(1000), 600)
        self.assertEqual(lifetime.workload_timeout(1000), 780)
        self.clock.advance(600)
        # Guest business expiry is local to its own deadline object; host waits
        # for the already-delivered role to perform its bounded instance stop.
        self.assertEqual(lifetime.workload_timeout(1000), 180)
        with self.assertRaises(boundary.RuntimeBoundaryError):
            lifetime.check('EXECUTE')
        self.assertEqual(lifetime.instance_stop_timeout(1000), 180)
        self.clock.advance(180)
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'INSTANCE_STOP_BUDGET'):
            lifetime.instance_stop_timeout(1000)
        self.assertEqual(lifetime.clip_timeout(120, cleanup=True), 120)

    def test_wall_rollback_cannot_extend_and_close_never_reopens(self):
        lifetime = self.lifetime()
        self.clock.advance(10)
        lifetime.check('BASELINE')
        self.clock.wall -= 3
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'CLOCK_ROLLBACK'):
            lifetime.check('ROOT')
        self.clock.wall += 3
        with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'CLOSED'):
            lifetime.check('RECONNECT')
        lifetime.close('CANCELLED')
        self.assertTrue(lifetime.record['closed'])
        self.assertEqual(lifetime.check('CLOSE_RESOURCES', cleanup=True), 14690)

    def test_guest_envelope_rejects_cross_session_extended_or_untrusted_time(self):
        lifetime = self.lifetime()
        envelope = lifetime.guest_envelope()
        for key, value in (('session_id', 'f' * 32), ('effective_expires_utc_seconds', self.clock.wall + 14401),
                           ('remaining_seconds', 20000), ('stop_reserve_seconds', 0), ('issued_utc_seconds', self.clock.wall + 10)):
            with self.subTest(key=key), self.assertRaises(boundary.RuntimeBoundaryError):
                boundary.RuntimeDeadline.from_guest_envelope({**envelope, key: value}, binding=binding(),
                    wall_clock=lambda: self.clock.wall, monotonic_clock=lambda: 5.0)


class RuntimeBaselineTests(unittest.TestCase):
    def validate(self, value):
        return boundary.validate_runtime_baseline(value, expected_binding=binding(), now=2_000_000_000.0)

    def test_permission_unknown_remains_unprivileged_and_digest_bound(self):
        value = baseline_fixture()
        result = self.validate(value)
        self.assertEqual(result['observation']['euid'], 1000)
        self.assertEqual(result['observation']['docker_daemon']['state'], 'PERMISSION_UNKNOWN')
        self.assertIn('effective_uid_zero', result['observation']['root_observations_pending'])
        value['uid'] = 1001
        self.assertNotEqual(result['baseline_digest'], self.validate(value)['baseline_digest'])

    def test_minor_and_build_numbers_cannot_satisfy_required_tool_major(self):
        cases = (
            ('pg_dump', 'pg_dump (PostgreSQL) 15.16', 15),
            ('psql', 'psql (PostgreSQL) 15.16', 15),
            ('pg_dump', 'pg_dump (PostgreSQL) 15.9 (Ubuntu 16.4)', 15),
            ('compose', 'Docker Compose version v1.2.0', 1),
            ('compose', 'Docker Compose version v3.2.0', 3),
            ('compose', 'Docker Compose version v1.29.2-build.2.0', 1),
            ('psql', 'unexpected wrapper 16.4', None),
            ('compose', 'unexpected wrapper v2.24.6', None),
        )
        for name, text, major in cases:
            with self.subTest(name=name, text=text):
                value = baseline_fixture()
                value['versions'][name] = text
                with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'HARD_PREREQUISITE_MISSING'):
                    self.validate(value)
                diagnostic = boundary.runtime_baseline_failure_diagnostic(value)['versions'][name]
                self.assertEqual(diagnostic['major'], major)
                self.assertFalse(diagnostic['required_major_matches'])

    def test_common_tool_version_formats_keep_the_actual_major(self):
        cases = (
            ('pg_dump', 'pg_dump (PostgreSQL) 16.4', 16),
            ('psql', 'psql (PostgreSQL) 16.4 (Ubuntu 16.4-0ubuntu0.24.04.2)', 16),
            ('pg_dump', 'pg_dump (PostgreSQL) 16.15 (Debian 16.15-1.pgdg120+1)', 16),
            ('compose', 'Docker Compose version v2.24.6', 2),
            ('compose', 'Docker Compose version 2.24.6+ds1-0ubuntu2', 2),
            ('compose', 'Docker Compose version v2.29.2-desktop.2', 2),
        )
        for name, text, major in cases:
            with self.subTest(name=name, text=text):
                value = baseline_fixture()
                value['versions'][name] = text
                self.validate(value)
                diagnostic = boundary.runtime_baseline_failure_diagnostic(value)['versions'][name]
                self.assertEqual(diagnostic['major'], major)
                self.assertTrue(diagnostic['required_major_matches'])

    def test_missing_stale_cross_session_boot_tools_and_hard_prerequisites_reject(self):
        mutations = [lambda value: value.pop('tools'),
            lambda value: value.update(observed_utc_seconds=1_999_999_939.0),
            lambda value: value['binding'].update(session_id='x' * 32),
            lambda value: value.update(boot_id='another-boot'),
            lambda value: value.update(euid=0),
            lambda value: value['tools']['docker'].update(path='docker'),
            lambda value: value['tools']['python'].update(resolved_path='/tmp/python3'),
            lambda value: value['versions'].update(pg_dump='pg_dump (PostgreSQL) 15.9'),
            lambda value: value.update(hard_missing=['RUNTIME_PACKAGE_MISSING']),
            lambda value: value.update(docker_service_active=False),
            lambda value: value['command_results'][0].update(argv=['/usr/bin/systemctl', 'start', 'docker']),
            lambda value: value.update(root_observations_pending=[])]
        for mutation in mutations:
            value = baseline_fixture()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(boundary.RuntimeBoundaryError):
                self.validate(value)

    def test_actual_fixed_command_roundtrips_and_capture_library_compiles_without_observation(self):
        command = boundary.fixed_baseline_command(binding())
        argv = shlex.split(command)
        self.assertEqual(argv[:4], ['/usr/bin/python3', '-I', '-B', '-c'])
        import ast
        tree = ast.parse(argv[4])
        encoded = next(node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 100)
        raw = zlib.decompress(base64.b64decode(encoded))
        compile(raw, '<fixed-runtime-baseline>', 'exec')
        self.assertEqual(zlib.decompress(zlib.compress(raw)), raw)
        # Load definitions only. The actual observer invocation is removed;
        # no command, filesystem probe, socket or Guest is run by this test.
        definitions = raw.decode('utf-8').rsplit('\n_collect_guest_baseline(', 1)[0]
        scope = {'__name__': '__main__'}
        exec(compile(definitions, '<synthetic-construction-only>', 'exec'), scope)  # noqa: S102 - fixed definitions only
        self.assertEqual(scope['capture_scope']['STREAM_LIMIT'], 65536)
        self.assertTrue(callable(scope['_collect_guest_baseline']))
        for name, text, major in (
                ('pg_dump', 'pg_dump (PostgreSQL) 15.16', 15),
                ('psql', 'psql (PostgreSQL) 16.4 (Ubuntu 16.4-0ubuntu0.24.04.2)', 16),
                ('compose', 'Docker Compose version v1.2.0', 1),
                ('compose', 'Docker Compose version 2.24.6+ds1-0ubuntu2', 2)):
            with self.subTest(name=name, text=text):
                self.assertEqual(scope['runtime_tool_version_major'](name, text), major)
        # Exercise the transported collector with fixed, in-memory tool replies.
        # All Guest observations and process calls are replaced before invocation.
        import io
        import sys
        from contextlib import redirect_stdout
        fixture = baseline_fixture()
        fixture['tools']['python']['resolved_path'] = str(Path(sys.executable).resolve())
        files = {'/etc/os-release': 'ID=ubuntu\nVERSION_ID="24.04"\n',
                 '/etc/machine-id': binding()['machine_id'],
                 '/proc/sys/kernel/random/boot_id': binding()['boot_id']}
        for wrong_major in (False, True):
            versions = dict(fixture['versions'])
            if wrong_major:
                versions.update(pg_dump='pg_dump (PostgreSQL) 15.16',
                                psql='psql (PostgreSQL) 15.16',
                                compose='Docker Compose version v1.2.0')
            def capture(argv, **kwargs):
                if argv[0] == '/usr/bin/dpkg-query':
                    output = f'ii \t{argv[-1]}\t16.4\tamd64\n'
                elif argv[0] == '/usr/bin/dpkg':
                    output = 'amd64\n'
                elif argv[1] == 'is-active':
                    output = ''
                elif argv[1] == 'show':
                    output = '123\n456\n'
                else:
                    name = 'compose' if argv[-1] == 'version' else Path(argv[0]).name
                    output = versions[name] + '\n'
                return SimpleNamespace(outcome='EXITED', secondary_errors=[], returncode=0,
                    stdout=output.encode(), stderr=b'', started_at='2033-05-18T03:33:19Z',
                    ended_at='2033-05-18T03:33:20Z', stdout_summary={'truncated': False, 'missing': False},
                    stderr_summary={'truncated': False, 'missing': False})
            output = io.StringIO()
            with self.subTest(wrong_major=wrong_major), redirect_stdout(output), \
                    mock.patch.dict(scope, observe_runtime_tools=lambda: fixture['tools'],
                                    observe_runtime_socket=lambda: fixture['docker_socket']), \
                    mock.patch.object(Path, 'read_text', autospec=True, side_effect=lambda path, **kwargs: files[path.as_posix()]), \
                    mock.patch('os.getuid', return_value=1000, create=True), \
                    mock.patch('os.geteuid', return_value=1000, create=True), mock.patch('sys.platform', 'linux'), \
                    mock.patch('platform.machine', return_value='x86_64'), \
                    mock.patch('time.time', return_value=2_000_000_000.0):
                scope['_collect_guest_baseline'](binding(), capture)
            observed = json.loads(output.getvalue())
            if wrong_major:
                self.assertEqual(observed['hard_missing'], ['COMPOSE_MAJOR_MISMATCH',
                    'POSTGRES_CLIENT_MAJOR_MISMATCH:pg_dump', 'POSTGRES_CLIENT_MAJOR_MISMATCH:psql'])
                with self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'HARD_PREREQUISITE_MISSING'):
                    self.validate(observed)
            else:
                self.assertEqual(observed['hard_missing'], [])

    def test_runtime_capture_order_and_missing_baseline_never_capture(self):
        from scripts import candidate_guest_session as guests
        from scripts import development_plan
        events = []
        plan = SimpleNamespace(runtime_offline_only=True, published_subject_digest=None)
        provider = mock.Mock()
        provider._verify_bootstrap_connection.side_effect = lambda *args, **kwargs: events.append('identity') or object()
        batch = mock.MagicMock()
        batch.capture_after_bootstrap_observation.side_effect = lambda *args: events.append('capture')
        batch.issue.side_effect = RuntimeError('synthetic stop after capture')
        with mock.patch.object(guests, '_batch', return_value=batch), \
                mock.patch.object(development_plan, 'is_development_plan', return_value=True), \
                mock.patch.object(boundary, 'run_before_capture', side_effect=lambda *args: events.append('baseline-confirm')), \
                self.assertRaisesRegex(RuntimeError, 'synthetic stop'):
            guests.bootstrap_candidate(provider, plan, object(), object(), 'disk', 'snapshot')
        self.assertEqual(events, ['identity', 'baseline-confirm', 'capture'])
        batch.capture_after_bootstrap_observation.reset_mock()
        with mock.patch.object(guests, '_batch', return_value=batch), \
                mock.patch.object(development_plan, 'is_development_plan', return_value=True), \
                mock.patch.object(boundary, 'run_before_capture', side_effect=boundary.RuntimeBoundaryError('MISSING')), \
                self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'MISSING'):
            guests.bootstrap_candidate(provider, plan, object(), object(), 'disk', 'snapshot')
        batch.capture_after_bootstrap_observation.assert_not_called()

    def _run_baseline_capture(self, *, capacity_error=None, baseline_mutation=None, expected_failure=None,
                              baseline_only=False, record_sink=None):
        from scripts import candidate_guest_session as guests
        from scripts import candidate_vm_harness as h
        from scripts import (
            development_guest_session,
            development_plan,
            development_source,
        )
        from scripts.guest_console_capture import WindowsConsoleCapture
        identity = binding()
        profile = SimpleNamespace(profile='RUNTIME_BASE_OFFLINE', clone_identity=identity['clone_identity'],
            snapshot_identity=identity['snapshot_identity'])
        plan = SimpleNamespace(**{key: identity[key] for key in ('plan_digest', 'session_id',
            'execution_source_sha', 'execution_source_tree', 'execution_inventory_digest',
            'candidate_input_digest', 'verified_candidate_digest', 'runtime_trust_selection_digest')},
            runtime_offline_only=True, runtime_baseline_only=baseline_only,
            platform_diagnostic=False, published_subject_digest=None,
            profiles=(profile,), source_sha='f' * 40, source_tree='a' * 40, qualification_run_id=1,
            runtime_authorization_deadline='2099-01-01T00:00:00Z', runtime_retention_policy='STOP_AND_RETAIN')
        authority = object()
        guest = SimpleNamespace(**{key: identity[key] for key in ('machine_id', 'boot_id', 'nonce')})
        verified = SimpleNamespace(authority=authority, guest=guest)
        observed = baseline_fixture()
        observed['binding'] = boundary.baseline_binding(plan, profile, verified)
        if baseline_mutation is not None:
            baseline_mutation(observed)
        lifetime = boundary.RuntimeDeadline(binding=identity,
            authorization_expires_utc_seconds=2_000_014_400.0,
            wall_clock=lambda: 2_000_000_000.0, monotonic_clock=lambda: 100.0)
        provider, batch = mock.Mock(), mock.MagicMock()
        provider._runtime_confirmed_boundary = None
        provider._profile_operation_results = {'RUNTIME_BASE_OFFLINE': {}}
        provider._active_profile_authority.return_value = authority
        provider._verify_bootstrap_connection.return_value = verified
        provider._observe_guest_connection.return_value = guest
        provider._tool_path.return_value = h.SSH
        provider._ssh_argv.side_effect = lambda authority, command, **kwargs: [str(h.SSH), command]
        events = []
        provider._run.side_effect = lambda *args, **kwargs: events.append('fixed-observer') or SimpleNamespace(
            stdout=json.dumps(observed).encode(), stderr=b'')
        batch.capture_after_bootstrap_observation.side_effect = lambda *args: events.append('capture')
        batch.issue.side_effect = RuntimeError('synthetic stop after capture')
        lease = mock.Mock(authority=authority)
        source = SimpleNamespace(root=Path(boundary.__file__).resolve().parents[1])
        def capacity(*args, **kwargs):
            self.assertIsNotNone(provider._runtime_confirmed_boundary)
            self.assertEqual(kwargs, {'_construction_only': False})
            events.append('observed-capacity')
            if capacity_error is not None:
                raise capacity_error
            return {'profiles': {profile.profile: {'construction_only': False}}}
        with (mock.patch.object(guests, '_batch', return_value=batch),
                mock.patch.object(development_plan, 'is_development_plan', return_value=True),
                mock.patch.object(development_source, 'require_development_source', return_value=source),
                mock.patch.object(boundary, 'require_runtime_lifetime', return_value=lifetime),
                mock.patch.object(boundary.time, 'time', return_value=2_000_000_000.0),
                mock.patch('installer.development_trust.runtime_material_boundary',
                    return_value={'selection_digest': identity['runtime_trust_selection_digest']}),
                mock.patch.object(WindowsConsoleCapture, 'confirm_batch',
                    side_effect=lambda *args, **kwargs: events.append('native-fixture-confirm')),
                mock.patch.object(development_guest_session, 'preflight_development_workload_commands',
                    side_effect=capacity),
                self.assertRaisesRegex(RuntimeError, expected_failure or (
                    'COMMAND_LIMIT_EXCEEDED' if capacity_error else 'synthetic stop'))):
            guests.bootstrap_candidate(provider, plan, profile, lease, 'disk', 'snapshot')
        if expected_failure:
            self.assertIsNone(provider._runtime_confirmed_boundary)
            self.assertNotIn('runtime_baseline', provider._profile_operation_results[profile.profile])
            self.assertNotIn('confirmed_runtime_boundary', provider._profile_operation_results[profile.profile])
            if expected_failure == 'RUNTIME_BASELINE_DIAGNOSTIC_COMPLETE':
                provider._observe_guest_connection.assert_called_once()
            else:
                provider._observe_guest_connection.assert_not_called()
            batch.capture_after_bootstrap_observation.assert_not_called()
            batch.issue.assert_not_called()
            public = json.loads(json.dumps({'profile_operations': provider._profile_operation_results}))
            if record_sink is not None:
                record_sink.update(public)
            return events, public['profile_operations'][profile.profile].get('runtime_baseline_failure')
        self.assertEqual(provider._runtime_confirmed_boundary.body['baseline_digest'],
            provider._profile_operation_results[profile.profile]['runtime_baseline']['baseline_digest'])
        self.assertEqual(provider._runtime_confirmed_boundary.body['guest_identity']['boot_id'], guest.boot_id)
        return events

    def test_hard_failures_export_diagnostics_without_confirmation_or_capture(self):
        def missing_package(value):
            value['hard_missing'] = ['RUNTIME_PACKAGE_MISSING:docker-compose-v2']
            value['packages'].pop('docker-compose-v2')
            value['versions'].pop('compose')
            value['tools']['compose'] = None
            next(item for item in value['command_results']
                 if item['argv'][-1] == 'docker-compose-v2')['returncode'] = 1
        cases = (
            ('missing-package', missing_package),
            ('inactive-service', lambda value: value.update(docker_service_active=False)),
            ('architecture', lambda value: value.update(package_architecture='arm64')),
            ('package-shape', lambda value: value['packages']['docker.io'].update(extra=True)),
            ('version-set', lambda value: value['versions'].pop('docker')),
            ('postgres-major', lambda value: value['versions'].update(pg_dump='pg_dump (PostgreSQL) 15.9')),
            ('compose-major', lambda value: value['versions'].update(compose='Docker Compose version v1.29')),
            ('commands', lambda value: value.update(command_results=[])),
            ('daemon', lambda value: value['docker_daemon'].update(state='UNAVAILABLE')),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                events, diagnostic = self._run_baseline_capture(baseline_mutation=mutate,
                    expected_failure='RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
                self.assertEqual(events, ['fixed-observer'])
                self.assertEqual(diagnostic['schema'], 'animemo.runtime-baseline-failure/v1')
                self.assertFalse(diagnostic['observation_accepted'])
                self.assertEqual(diagnostic['failure_code'], 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
                if name == 'missing-package':
                    self.assertEqual(diagnostic['hard_missing'], ['RUNTIME_PACKAGE_MISSING:docker-compose-v2'])
                    self.assertEqual(diagnostic['probe_returncodes']['package:docker-compose-v2'], 1)
                    self.assertEqual(diagnostic['tools']['compose'], 'UNAVAILABLE')
                elif name == 'inactive-service':
                    self.assertFalse(diagnostic['docker_service_active'])
                elif name == 'architecture':
                    self.assertEqual(diagnostic['package_architecture'], 'arm64')
                elif name == 'package-shape':
                    self.assertFalse(diagnostic['packages']['docker.io']['fields_valid'])
                elif name == 'version-set':
                    self.assertFalse(diagnostic['version_set_valid'])
                elif name == 'postgres-major':
                    self.assertEqual(diagnostic['versions']['pg_dump']['major'], 15)
                    self.assertFalse(diagnostic['versions']['pg_dump']['required_major_matches'])
                elif name == 'compose-major':
                    self.assertEqual(diagnostic['versions']['compose']['major'], 1)
                    self.assertFalse(diagnostic['versions']['compose']['required_major_matches'])
                elif name == 'commands':
                    self.assertFalse(diagnostic['command_results_present'])
                elif name == 'daemon':
                    self.assertEqual(diagnostic['docker_daemon_state'], 'UNAVAILABLE')

    def test_binding_staleness_and_privilege_failures_never_export_untrusted_diagnostics(self):
        cases = (
            ('BINDING_MISMATCH', lambda value: value['binding'].update(session_id='x' * 32)),
            ('STALE', lambda value: value.update(observed_utc_seconds=1_999_999_939.0)),
            ('UNPRIVILEGED_REQUIRED', lambda value: value.update(euid=0)),
        )
        for code, mutate in cases:
            with self.subTest(code=code):
                events, diagnostic = self._run_baseline_capture(baseline_mutation=mutate,
                    expected_failure='RUNTIME_BASELINE_' + code)
                self.assertEqual(events, ['fixed-observer'])
                self.assertIsNone(diagnostic)

    def test_real_baseline_consumer_and_native_fixture_must_finish_before_capture(self):
        self.assertEqual(self._run_baseline_capture(),
            ['fixed-observer', 'native-fixture-confirm', 'observed-capacity', 'capture'])

    def test_observed_command_overflow_never_reaches_password_capture(self):
        from scripts.guest_sudo_session import ControllerFailure
        self.assertEqual(self._run_baseline_capture(
            capacity_error=ControllerFailure('CANDIDATE_WORKLOAD_COMMAND_LIMIT_EXCEEDED')),
            ['fixed-observer', 'native-fixture-confirm', 'observed-capacity'])


class RuntimeBaselineFailureDiagnosticTests(unittest.TestCase):
    def test_fixed_probes_and_numeric_versions_are_kept_without_mutating_input(self):
        value = baseline_fixture()
        original = copy.deepcopy(value)
        diagnostic = boundary.runtime_baseline_failure_diagnostic(value)
        self.assertEqual(value, original)
        self.assertEqual(diagnostic['probe_returncodes']['package:docker.io'], 0)
        self.assertEqual(diagnostic['probe_returncodes']['docker_daemon_identity'], 0)
        self.assertIsNone(diagnostic['probe_returncodes']['docker_storage_root'])
        self.assertEqual(diagnostic['versions']['compose']['major'], 2)
        self.assertTrue(diagnostic['versions']['pg_dump']['required_major_matches'])
        self.assertFalse(diagnostic['observation_accepted'])

    def test_reason_whitelist_preserves_all_fixed_emitter_categories(self):
        codes = [
            'RUNTIME_TRANSPORT_CODEC_UNAVAILABLE', 'TRUSTED_RUNTIME_TOOL_MISSING',
            'PYTHON_EXECUTABLE_MISMATCH', 'DOCKER_SERVICE_INACTIVE',
            'LOCAL_DOCKER_SOCKET_MISSING_OR_UNTRUSTED', 'PACKAGE_ARCHITECTURE_MISMATCH',
            'COMPOSE_MAJOR_MISMATCH', 'DOCKER_DAEMON_IDENTITY_INVALID',
            'DOCKER_DAEMON_IDENTITY_UNAVAILABLE', 'DOCKER_DAEMON_UNAVAILABLE',
            'RUNTIME_DISTRIBUTION_MISMATCH',
            *('RUNTIME_PACKAGE_MISSING:' + name for name in ('docker.io', 'docker-compose-v2', 'postgresql-client-16')),
            *('RUNTIME_TOOL_VERSION_UNAVAILABLE:' + name for name in ('docker', 'systemctl', 'compose', 'pg_dump', 'psql')),
            'POSTGRES_CLIENT_MAJOR_MISMATCH:pg_dump', 'POSTGRES_CLIENT_MAJOR_MISMATCH:psql',
        ]
        diagnostic = boundary.runtime_baseline_failure_diagnostic({'hard_missing': codes})
        self.assertEqual(diagnostic['hard_missing'], codes)
        self.assertFalse(diagnostic['hard_missing_unrecognized_or_limited'])

    def test_private_text_paths_outputs_and_extra_fields_are_omitted_and_bounded(self):
        marker = 'SYNTHETIC_PRIVATE_CREDENTIAL_AND_PATH_SENTINEL'
        value = baseline_fixture()
        value['hard_missing'] = ['DOCKER_SERVICE_INACTIVE', marker] * 40
        value['environment'] = {'TOKEN': marker}
        value['binding'] = {'private_path': marker}
        for tool in value['tools'].values():
            tool.update(path=marker, resolved_path=marker, sha256=marker)
        for package in value['packages'].values():
            package.update(version=marker * 1000, private_path=marker)
        value['versions'] = {name: marker * 1000 for name in value['versions']}
        value['docker_socket'].update(path=marker, resolved_path=marker)
        value['docker_daemon'].update(identity=marker, storage_root=marker)
        for item in value['command_results']:
            item.update(stdout=marker, stderr=marker, started_at=marker, ended_at=marker)
        value['command_results'] += [{'argv': [marker], 'returncode': 1}] * 40
        diagnostic = boundary.runtime_baseline_failure_diagnostic(value)
        encoded = json.dumps(diagnostic, sort_keys=True).encode()
        self.assertLessEqual(len(encoded), 8192)
        self.assertNotIn(marker.encode(), encoded)
        self.assertLessEqual(len(diagnostic['hard_missing']), 32)
        self.assertTrue(diagnostic['hard_missing_unrecognized_or_limited'])
        self.assertTrue(diagnostic['command_results_limited'])
        self.assertNotIn('binding', diagnostic)
        self.assertNotIn('baseline_digest', diagnostic)
        self.assertFalse(diagnostic['versions']['docker']['text_valid'])

    def test_malformed_fields_and_returncodes_cannot_replace_the_primary_failure(self):
        for value in (None, [], {'hard_missing': 'private'}, {
                'hard_missing': [None, [], {}], 'tools': [], 'packages': [],
                'versions': [], 'command_results': [None, [], {'argv': [{}]}],
                'docker_service_active': 1, 'docker_socket': [], 'docker_daemon': []}):
            with self.subTest(value=value):
                diagnostic = boundary.runtime_baseline_failure_diagnostic(value)
                self.assertEqual(diagnostic['failure_code'], 'RUNTIME_BASELINE_HARD_PREREQUISITE_MISSING')
                self.assertTrue(diagnostic['hard_missing_unrecognized_or_limited'])
                self.assertLessEqual(len(json.dumps(diagnostic).encode()), 8192)
        for code in (True, 'private', 2**64):
            value = baseline_fixture()
            value['command_results'][0]['returncode'] = code
            with self.subTest(code=code):
                diagnostic = boundary.runtime_baseline_failure_diagnostic(value)
                self.assertIsNone(diagnostic['probe_returncodes']['docker_service'])


class RuntimeTimedConfirmationTests(unittest.TestCase):
    def console(self):
        from scripts import guest_console_capture as capture
        from scripts.tests.test_guest_console_capture import ConsoleFixture
        fixture = ConsoleFixture()
        fixture.confirm_answer = 6
        return capture.WindowsConsoleCapture(_api=fixture), fixture, fixture.confirmations

    def test_native_fixture_uses_bounded_timeout_only_for_new_prompt(self):
        console, fixture, calls = self.console()
        cancelled = threading.Event()
        console._confirm_batch('synthetic Runtime boundary', timeout_seconds=12.345, cancelled=cancelled)
        self.assertEqual(calls[0][-2:], (12.345, cancelled))
        self.assertEqual(fixture.read_modes, [])
        console._confirm_batch('synthetic old confirmation', timeout_seconds=None, cancelled=None)
        self.assertEqual(len(fixture.confirmations), 2)
        self.assertEqual(calls[1][-2:], (None, None))

    def test_missing_timer_timeout_and_cancel_fail_without_secret_or_retry(self):
        from scripts.guest_console_capture import ConsoleCaptureError
        console, fixture, calls = self.console()
        fixture.confirm_answer = 32000
        with self.assertRaisesRegex(ConsoleCaptureError, 'CANCELLED'):
            console._confirm_batch('synthetic', timeout_seconds=1, cancelled=threading.Event())
        self.assertEqual(len(calls), 1)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ConsoleCaptureError, 'CANCELLED'):
            console._confirm_batch('synthetic', timeout_seconds=1, cancelled=cancelled)
        self.assertEqual(len(calls), 1)
        def unavailable(*args, **kwargs):
            raise ConsoleCaptureError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        fixture.confirm_public = unavailable
        with self.assertRaisesRegex(ConsoleCaptureError, 'DISPLAY_UNAVAILABLE'):
            console._confirm_batch('synthetic', timeout_seconds=1, cancelled=threading.Event())
        self.assertEqual(fixture.read_modes, [])
        for invalid in (0, -1, 61, True, float('nan')):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ConsoleCaptureError, 'INVALID'):
                console._confirm_batch('synthetic', timeout_seconds=invalid, cancelled=None)


class RuntimeKnownFactsTests(unittest.TestCase):
    def facts(self):
        value = boundary.confirmed_baseline_facts(baseline_fixture())
        value['docker_socket'] = {'state': 'KNOWN_LOCAL', 'path': '/var/run/docker.sock',
            'resolved_path': '/run/docker.sock', 'device': 1, 'inode': 200,
            'mode': 0o140660, 'uid': 0, 'gid': 999, 'ctime_ns': 100000}
        value['docker_daemon'] = {'state': 'OBSERVED',
            'identity': boundary.runtime_daemon_identity(b'100\n200000\n'),
            'server_version': '26.1.0', 'storage_root': '/var/lib/docker'}
        return value

    def test_known_same_boot_tool_socket_and_daemon_replacement_rejected(self):
        expected = self.facts()
        mutations = [lambda value: value['tools']['docker'].update(sha256='sha256:' + 'f' * 64),
            lambda value: value['tools']['compose'].update(sha256='sha256:' + 'f' * 64),
            lambda value: value['docker_socket'].update(inode=201),
            lambda value: value['docker_daemon'].update(identity=boundary.runtime_daemon_identity(b'101\n300000\n')),
            lambda value: value['docker_daemon'].update(server_version='26.1.1')]
        for mutate in mutations:
            actual = copy.deepcopy(expected)
            mutate(actual)
            with self.subTest(actual=actual), self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'CHANGED'):
                boundary.compare_runtime_baseline_facts(expected, actual)

    def test_permission_unknown_is_resolved_by_actual_root_facts_not_promoted(self):
        expected = boundary.confirmed_baseline_facts(baseline_fixture())
        actual = self.facts()
        self.assertEqual(boundary.compare_runtime_baseline_facts(expected, actual), actual)
        self.assertEqual(expected['docker_daemon'], {'state': 'PERMISSION_UNKNOWN', 'identity': None})
        for key in ('docker_socket', 'docker_daemon'):
            unresolved = copy.deepcopy(actual)
            unresolved[key] = expected[key]
            with self.subTest(key=key), self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'REQUIRED'):
                boundary.compare_runtime_baseline_facts(expected, unresolved)

    def test_shared_root_observer_rejects_tool_change_before_any_command(self):
        expected, runner = self.facts(), mock.Mock()
        tools = copy.deepcopy(expected['tools'])
        tools['docker']['sha256'] = 'sha256:' + 'f' * 64
        with (mock.patch.object(boundary, 'observe_runtime_tools', return_value=tools),
              mock.patch.object(boundary, 'observe_runtime_socket', return_value=expected['docker_socket']),
              self.assertRaisesRegex(boundary.RuntimeBoundaryError, 'TOOL_CHANGED')):
            boundary.observe_runtime_baseline_facts(expected=expected, runner=runner, timeout=lambda: 30)
        runner.run.assert_not_called()

    def test_shared_root_observer_queries_fixed_local_daemon_and_rechecks_files(self):
        expected, runner = self.facts(), mock.Mock()
        runner.run.side_effect = [SimpleNamespace(returncode=0, stderr='', stdout=value)
                                 for value in ('100\n200000\n', '26.1.0\n', '/var/lib/docker\n')]
        deadline = mock.Mock(return_value=19)
        with (mock.patch.object(boundary, 'observe_runtime_tools', return_value=expected['tools']) as tools,
              mock.patch.object(boundary, 'observe_runtime_socket', return_value=expected['docker_socket']) as socket):
            actual = boundary.observe_runtime_baseline_facts(expected=expected, runner=runner, timeout=deadline)
        self.assertEqual(actual, expected)
        self.assertEqual(tools.call_count, 2)
        self.assertEqual(socket.call_count, 2)
        self.assertEqual(deadline.call_count, 3)
        self.assertEqual([call.kwargs for call in runner.run.call_args_list], [{'timeout': 19}] * 3)
        for call in runner.run.call_args_list[1:]:
            self.assertEqual(call.args[0][:3], ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock'])


if __name__ == '__main__':
    unittest.main()
