import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from installer import development as service
from installer.production import ProductionFreshInstallPort
from installer.runtime import InstallerAdapterError
from installer.tests import test_candidate_input as fixtures
from scripts import development_installer_entry as entry


class DevelopmentServiceTests(unittest.TestCase):
    def test_source_cannot_be_constructed_or_used_by_default_composition(self):
        with self.assertRaises(TypeError):
            service.DevelopmentServiceSource()
        with self.assertRaisesRegex(ValueError, 'SOURCE_INVALID'):
            ProductionFreshInstallPort(releases=mock.Mock(), configuration=mock.Mock(),
                runner=mock.Mock(), _development_service_source=object())

    def test_prepare_services_uses_current_source_only_for_development_capability(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                fresh = object.__new__(ProductionFreshInstallPort)
                fresh.namespace = SimpleNamespace(app_root=Path('/qualified'), name='default')
                source = SimpleNamespace(root=Path('/sealed-development'), verify_source=mock.Mock())
                fresh._development_service_source = source if enabled else None
                fresh.runner = mock.Mock()
                fresh.runner.run.side_effect = RuntimeError('stop after first command')
                with self.assertRaisesRegex(InstallerAdapterError, 'INSTALL_SERVICE_PREPARATION_FAILED'):
                    fresh.prepare_services(object())
                expected = source.root if enabled else fresh.namespace.app_root
                self.assertEqual(fresh.runner.run.call_args.args[0],
                    [str(expected / 'deploy/install-updater.sh'), '--instance', 'default'])
                self.assertEqual(source.verify_source.call_count, int(enabled))

    def test_installed_inventory_reads_real_bytes_and_rejects_old_updater(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, installed = root / 'source', root / 'installed'
            for package in service.PACKAGES:
                path = source_root / package / '__init__.py'
                path.parent.mkdir(parents=True)
                path.write_text('__version__ = "1.0.0"\n', encoding='utf-8')
            assets = {}
            for index, name in enumerate(service.ASSETS):
                path = source_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
                target = root / ('asset-' + str(index))
                shutil.copyfile(path, target)
                assets[name] = str(target)
            for package in service.PACKAGES:
                shutil.copytree(source_root / package, installed / package)
            with mock.patch.object(service, 'ASSETS', assets):
                expected = service.expected_service_observation(source_root, 'sha256:' + '1' * 64)
                expected['installed_root'] = str(installed)
                authority = object.__new__(service.DevelopmentServiceSource)
                authority.inventory_digest = expected['execution_inventory_digest']
                authority._expected = expected
                current = SimpleNamespace(is_symlink=lambda: True, resolve=lambda **kw: installed)
                def paths(value):
                    return current if value == '/opt/animemo-updater/current' else Path(value)
                with (mock.patch.object(service.DevelopmentServiceSource, 'verify_source'),
                      mock.patch.object(service, 'Path', side_effect=paths)):
                    self.assertEqual(authority.observe_installed(), expected)
                    (installed / 'updater/__init__.py').write_bytes(b'old updater')
                    with self.assertRaises(service.DevelopmentServiceError):
                        authority.observe_installed()


class DevelopmentInstallerEntryTests(unittest.TestCase):
    def test_shared_complete_installer_requires_service_readback_and_closes_runtime(self):
        for failed_readback in (False, True):
            with self.subTest(failed_readback=failed_readback):
                composition = fixtures._ExecutingComposition()
                source = mock.Mock()
                proof = {'actual-service-source': 'observed'}
                source.observe_installed.return_value = proof
                if failed_readback:
                    source.observe_installed.side_effect = service.DevelopmentServiceError()
                output = io.StringIO()
                with (mock.patch.object(entry, 'inherited_writer', return_value=mock.Mock()),
                      mock.patch.object(entry, 'validate_binding', return_value={'verified_candidate_digest': fixtures.DIGEST}),
                      mock.patch.object(entry, 'acquire_development_service_source', return_value=source),
                      mock.patch.object(entry, 'build_candidate_composition', return_value=composition) as build,
                      redirect_stdout(output)):
                    code = entry.main(['--binding', '{}', '--profile', 'ONLINE_FRESH',
                                       '--public-origin', 'https://candidate.invalid'])
                self.assertIs(build.call_args.kwargs['_development_service_source'], source)
                composition.runtime.execute.assert_called_once()
                composition.close_candidate_runtime.assert_called_once_with()
                value = json.loads(output.getvalue())
                if failed_readback:
                    self.assertEqual(code, 5)
                    self.assertEqual(value['reasonCode'], 'DEVELOPMENT_SERVICE_SOURCE_MISMATCH')
                else:
                    self.assertEqual(code, 0)
                    self.assertEqual(value['developmentServiceSourceObservation'], proof)


if __name__ == '__main__':
    unittest.main()
