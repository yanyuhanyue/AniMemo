from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from installer import production
from installer.production import ProductionFreshInstallPort
from updater.deployment import HostPaths, ImmutableComposeDeployment
from updater.errors import StateError


class CandidateGatewayObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = HostPaths.testing(app=root/'app', data=root/'data', state=root/'state')
        self.identifier, self.container, self.endpoint = 'a'*64, 'b'*64, 'c'*64
        self.name = self.paths.compose_project + '_animemo'
        self.networks = {self.name: {'NetworkID': self.identifier, 'IPAddress': '172.30.0.2',
                                    'EndpointID': self.endpoint, 'Gateway': ''}}
        self.network = {'Id': self.identifier, 'Name': self.name, 'Driver': 'bridge', 'Scope': 'local',
            'Internal': True, 'EnableIPv6': False,
            'Labels': {'com.docker.compose.project': self.paths.compose_project, 'com.docker.compose.network': 'animemo'},
            'IPAM': {'Driver': 'default', 'Config': [{'Subnet': '172.30.0.0/24', 'Gateway': '172.30.0.1'}]},
            'Containers': {self.container: {'IPv4Address': '172.30.0.2/24', 'EndpointID': self.endpoint}}}
        self.runner = mock.Mock()
        self.deployment = ImmutableComposeDeployment(self.paths, runner=self.runner,
                                                     candidate_network_override=root/'override.yml')
        self.deployment._container_id = mock.Mock(return_value=self.container)
        self.deployment._inspect_container = mock.Mock(side_effect=lambda *_: json.dumps(self.networks))
        self.runner.run.side_effect = lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps([self.network]))

    def test_internal_gateway_comes_from_owned_network_id_without_default_route(self):
        result = self.deployment.candidate_internal_gateway({}, 'postgres')
        self.assertEqual(result, {'network_id':self.identifier, 'subnet':'172.30.0.0/24', 'gateway':'172.30.0.1'})
        self.deployment._container_id.assert_called_once_with({}, 'postgres', include_stopped=False)
        self.runner.run.assert_called_once_with(['/usr/bin/docker','network','inspect',self.identifier], timeout=30)

    def test_rejects_foreign_ambiguous_or_nonisolated_networks(self):
        mutations = (
            lambda n: n.update(Id='d'*64), lambda n: n.update(Internal=False),
            lambda n: n.update(EnableIPv6=True), lambda n: n.update(Driver='host'),
            lambda n: n['Labels'].update({'com.docker.compose.project':'foreign'}),
            lambda n: n['IPAM']['Config'].append({'Subnet':'10.0.0.0/24','Gateway':'10.0.0.1'}),
            lambda n: n['IPAM']['Config'][0].update(Gateway='10.0.0.1'),
            lambda n: n['IPAM']['Config'][0].update(Gateway='172.30.0.0'),
            lambda n: n['IPAM']['Config'][0].update(Gateway='172.30.0.2'),
            lambda n: n['Containers'].clear(),
            lambda n: n['Containers'][self.container].update(EndpointID='d'*64),
        )
        original = copy.deepcopy(self.network)
        for change in mutations:
            with self.subTest(change=change):
                self.network = copy.deepcopy(original); change(self.network)
                with self.assertRaises((StateError, ValueError)):
                    self.deployment.candidate_internal_gateway({}, 'postgres')
        self.network = original
        self.networks['foreign'] = copy.deepcopy(next(iter(self.networks.values())))
        with self.assertRaises(StateError):
            self.deployment.candidate_internal_gateway({}, 'postgres')


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'actual root ownership and O_NOFOLLOW required')
class CandidateEdgeFileTests(unittest.TestCase):
    def test_owned_readonly_file_and_live_network_binding(self):
        # /run is root-owned; temporary /tmp ancestors would correctly fail.
        with tempfile.TemporaryDirectory(dir='/run') as directory:
            root=Path(directory)/'candidate'
            with mock.patch.object(production,'_CANDIDATE_EDGE_CONTROL_BASE',root):
                fresh=ProductionFreshInstallPort(releases=mock.Mock(), configuration=mock.Mock(),
                    namespace=SimpleNamespace(name='default'), candidate_network_isolation=True)
                deployment=mock.Mock()
                binding={'network_id':'a'*64,'subnet':'172.30.0.0/24','gateway':'172.30.0.1'}
                deployment.candidate_internal_gateway.return_value=binding
                fresh._publish_candidate_edge_proxy(deployment,{})
                path=fresh._candidate_edge_path()
                self.assertEqual(path.read_bytes(),b'172.30.0.1\n')
                self.assertEqual(path.stat().st_mode&0o777,0o444)
                fresh._validate_candidate_edge_proxy(deployment,{},'web')
                deployment.candidate_internal_gateway.return_value={**binding,'network_id':'b'*64}
                with self.assertRaises(OSError): fresh._validate_candidate_edge_proxy(deployment,{},'web')
                deployment.candidate_internal_gateway.return_value=binding
                path.unlink(); path.symlink_to('/dev/null')
                with self.assertRaises(OSError): fresh._validate_candidate_edge_proxy(deployment,{},'web')
                path.unlink()

    def test_publication_rejects_writable_control_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(production,'_CANDIDATE_EDGE_CONTROL_BASE',Path(directory)/'candidate'):
                fresh=ProductionFreshInstallPort(releases=mock.Mock(),configuration=mock.Mock(),
                    namespace=SimpleNamespace(name='default'),candidate_network_isolation=True)
                deployment=mock.Mock()
                deployment.candidate_internal_gateway.return_value={'network_id':'a'*64,'subnet':'172.30.0.0/24','gateway':'172.30.0.1'}
                with self.assertRaises(OSError): fresh._publish_candidate_edge_proxy(deployment,{})


if __name__ == '__main__': unittest.main()
