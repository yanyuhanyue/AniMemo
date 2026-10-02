"""DEV lower command adapter constraints; recording sink only."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from installer.production import (
    CandidatePlatformCommandObserver,
    LocalDockerCommandRunner,
)


class DevelopmentCommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        from installer.development_boundary import _resource_scope
        self.gate = SimpleNamespace(boundary={'resources': _resource_scope()},
            binding={'execution_inventory_digest': 'sha256:' + 'a' * 64},
            execution_started=True, check_live=lambda *args: None,
            lifetime=SimpleNamespace(clip_timeout=lambda value, **kw: value))
        self.sink = mock.Mock()
        self.sink.run.return_value = SimpleNamespace(returncode=0, stdout='')
        self.runner = LocalDockerCommandRunner(self.sink, development_gate=self.gate)

    def test_wrong_socket_service_instance_and_volume_never_reach_sink(self):
        for argv in (
            ['/usr/bin/docker', '--host', 'tcp://other:2375', 'info'],
            ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', '-H=tcp://other', 'info'],
            ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'info', '-Htcp://other:2375'],
            ['/usr/bin/systemctl', 'stop', 'ssh.service'],
            ['/usr/bin/systemctl', 'enable', '--now', 'other.service'],
            ['/usr/bin/docker', 'compose', '--project-name', 'other', 'up', '--pull', 'never'],
            ['/usr/bin/docker', 'volume', 'rm', 'unrelated-data'],
            ['/usr/bin/docker', 'volume', 'ls', '--quiet', '--filter', 'label=com.docker.compose.project=other'],
            ['/usr/bin/docker', 'stop', '--time', '20', 'a' * 64],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                self.runner.run(argv)
        self.sink.run.assert_not_called()

    def test_required_readonly_local_inventory_remains_available(self):
        self.runner.run(['/usr/bin/docker', 'volume', 'ls', '--quiet', '--filter',
            'label=com.docker.compose.project=animemo-default'])
        self.runner.run(['/usr/bin/systemctl', 'show', 'animemo-updater@default.service',
            '--property=LoadState', '--value'])
        self.assertEqual(self.sink.run.call_count, 2)

    def test_platform_adapter_rejects_system_service_mutation(self):
        observer = CandidatePlatformCommandObserver(self.sink)
        observer._development_gate = self.gate
        with self.assertRaises(ValueError):
            observer.run(('/usr/bin/systemctl', 'start', 'ssh.service'), timeout=30, environment={})
        self.sink.run.assert_not_called()

    def test_real_capability_collector_keeps_fixed_compose_help(self):
        from installer import production as p
        fixed = {'compose_profiles', 'compose_v2', 'compose_wait', 'docker_daemon',
            'immutable_image_digest', 'loopback_port_binding', 'postgres_plain_dump',
            'postgres_psql_restore', 'systemd_unit_lifecycle'}
        qualification = SimpleNamespace(profile='v1.1-standard-linux-amd64', database_path=SimpleNamespace(
            source_server_major=16, pg_dump_major=16, psql_major=16, target_server_major=16))
        with mock.patch.object(p, '_filesystem_capabilities', return_value={key: True for key in set(p.REQUIRED_CAPABILITIES) - fixed}), \
                mock.patch.object(p.socket, 'socket', return_value=mock.MagicMock()), \
                mock.patch.object(p.host_platform, 'machine', return_value='x86_64'), \
                mock.patch.object(p.host_platform, 'system', return_value='Linux'):
            result = p.collect_host_capabilities(qualification, runner=self.runner)
        self.assertTrue(result.capabilities['compose_wait'])
        self.assertIn(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'compose', 'up', '--help'],
            [call.args[0] for call in self.sink.run.call_args_list])

    def test_real_gateway_observer_accepts_exact_network_id_and_rejects_foreign_labels(self):
        from updater.deployment import ImmutableComposeDeployment
        from updater.errors import StateError
        identifier, container = 'a' * 64, 'b' * 64
        name = 'animemo-default_animemo'
        network = {'Id': identifier, 'Name': name, 'Driver': 'bridge', 'Scope': 'local',
            'Internal': True, 'EnableIPv6': False, 'Labels': {'com.docker.compose.project': 'animemo-default',
            'com.docker.compose.network': 'animemo'}, 'IPAM': {'Driver': 'default',
            'Config': [{'Subnet': '172.25.0.0/16', 'Gateway': '172.25.0.1'}]},
            'Containers': {container: {'IPv4Address': '172.25.0.2/16', 'EndpointID': 'synthetic-endpoint'}}}
        deployment = object.__new__(ImmutableComposeDeployment)
        deployment.paths = SimpleNamespace(compose_project='animemo-default', instance_name='default', instance_id='fixture-id')
        deployment.candidate_network_override = 'synthetic-bound-override'
        deployment.runner = self.runner
        deployment._compose = lambda *args, **kwargs: SimpleNamespace(stdout=container)
        def effect(argv, **kwargs):
            if argv[3:5] == ['network', 'inspect']:
                result = [network]
            elif argv[5] == '{{json .NetworkSettings.Networks}}':
                result = {name: {'NetworkID': identifier, 'IPAddress': '172.25.0.2', 'EndpointID': 'synthetic-endpoint'}}
            else:
                result = ['default', 'fixture-id', 'animemo-default']
            return SimpleNamespace(returncode=0, stdout=json.dumps(result))
        self.sink.run.side_effect = effect
        result = deployment.candidate_internal_gateway({}, 'postgres')
        self.assertEqual(result['network_id'], identifier)
        network['Labels']['com.docker.compose.project'] = 'foreign'
        with self.assertRaises(StateError):
            deployment.candidate_internal_gateway({}, 'postgres')

    def test_capability_preparation_is_bound_before_filesystem_and_loopback_probe(self):
        from pathlib import Path

        from installer import production as p
        from installer.tests.test_development_boundary import RuntimePlanBoundaryTests
        fixture = RuntimePlanBoundaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        gate = fixture.gate_without_trust()
        runner = LocalDockerCommandRunner(self.sink, development_gate=gate)
        fixed = {'compose_profiles', 'compose_v2', 'compose_wait', 'docker_daemon',
            'immutable_image_digest', 'loopback_port_binding', 'postgres_plain_dump',
            'postgres_psql_restore', 'systemd_unit_lifecycle'}
        qualification = SimpleNamespace(profile='v1.1-standard-linux-amd64', database_path=SimpleNamespace(
            source_server_major=16, pg_dump_major=16, psql_major=16, target_server_major=16))
        socket_fixture = mock.MagicMock()
        with mock.patch.object(p, '_filesystem_capabilities', return_value={key: True for key in set(p.REQUIRED_CAPABILITIES) - fixed}) as files, \
                mock.patch.object(p.socket, 'socket', return_value=socket_fixture), \
                mock.patch.object(p.host_platform, 'machine', return_value='x86_64'), \
                mock.patch.object(p.host_platform, 'system', return_value='Linux'):
            p.collect_host_capabilities(qualification, runner=runner, _development_gate=gate)
            files.assert_called_once_with(probe_root=Path('/var/lib/animemo/local-development/runtime-cache')
                / gate.binding['runtime_trust_selection_digest'][7:] / gate.binding['session_id'])
            socket_fixture.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', 0))
            files.reset_mock()
            gate._boundary['preparation_writes'] = ['/etc']
            with self.assertRaises(ValueError):
                p.collect_host_capabilities(qualification, runner=runner, _development_gate=gate)
            files.assert_not_called()
