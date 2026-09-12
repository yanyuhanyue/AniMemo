import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from installer import bootstrap, development
from scripts.closed_runtime_inventory import closed_runtime_inventory_digest


class DevelopmentBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'execution'
        archive = self.root.parent / 'materials.tar'
        self.modules = {}
        with tarfile.open(archive, 'w:') as output:
            for name in bootstrap._REQUIRED_RUNTIME_MODULES:
                relative = name.replace('.', '/') + '.py'
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                original = (name + ' baseline\n').encode()
                path.write_bytes(original)
                member = tarfile.TarInfo(relative)
                member.size = len(original)
                output.addfile(member, io.BytesIO(original))
                self.modules[name] = SimpleNamespace(__file__=str(path))
        self.capability = bootstrap.VerifiedPrepublicationCandidateCapability(
            bootstrap._CANDIDATE_CAPABILITY_TOKEN,
            candidate_input_digest='sha256:' + '1' * 64,
            installer_materials_path=archive,
            installer_materials_sha256='sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest(),
            release_commit='2' * 40, verified_candidate_digest='sha256:' + '3' * 64, version='v2.0.0-rc.1')
        patch = mock.patch.object(bootstrap.importlib, 'import_module', side_effect=self.modules.__getitem__)
        patch.start()
        self.addCleanup(patch.stop)

    def verify(self, gate):
        return gate.verify_runtime_source(version=self.capability.version,
            release_commit=self.capability.release_commit)

    def development_source(self):
        value = object.__new__(development.DevelopmentServiceSource)
        value.root = self.root
        value.inventory_digest = closed_runtime_inventory_digest(self.root)
        value.verified_candidate_digest = self.capability.verified_candidate_digest
        # OS root acquisition is synthetic on Windows. Real closed inventory
        # and loaded-module path checks still run for every verification.
        def verify_source(actual):
            if closed_runtime_inventory_digest(actual.root) != actual.inventory_digest:
                raise development.DevelopmentServiceError()
        patch = mock.patch.object(development.DevelopmentServiceSource, 'verify_source', verify_source)
        patch.start()
        self.addCleanup(patch.stop)
        return value

    def test_formal_default_rejects_changed_code_while_development_verifies_exact_new_tree(self):
        formal = bootstrap.CandidateBootstrapPrivilegeGate(self.capability)
        original_authority = self.verify(formal)
        (self.root / 'installer/production.py').write_bytes(b'current changed Installer\n')
        with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, 'RUNTIME_MODULE_IDENTITY_MISMATCH'):
            self.verify(formal)
        source = self.development_source()
        gate = bootstrap.CandidateBootstrapPrivilegeGate(self.capability, _development_source=source)
        authority = self.verify(gate)
        self.assertNotEqual(authority.authorization_identity, original_authority.authorization_identity)
        self.assertEqual(authority.materials_sha256, self.capability.installer_materials_sha256)
        self.assertEqual(authority.materials_path, self.capability.installer_materials_path)
        (self.root / 'installer/production.py').write_bytes(b'changed after sealing\n')
        with self.assertRaises(development.DevelopmentServiceError):
            self.verify(gate)

    def test_development_rejects_wrong_loaded_module_path_and_changed_baseline_archive(self):
        source = self.development_source()
        gate = bootstrap.CandidateBootstrapPrivilegeGate(self.capability, _development_source=source)
        self.modules['installer.production'] = SimpleNamespace(__file__=str(self.capability.installer_materials_path))
        with self.assertRaises(development.DevelopmentServiceError):
            self.verify(gate)
        self.capability.installer_materials_path.write_bytes(b'altered qualification material')
        with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, 'MATERIAL_IDENTITY_MISMATCH'):
            self.verify(gate)

    def test_development_requires_exact_capability_and_material_binding(self):
        with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, 'DEVELOPMENT_SOURCE_INVALID'):
            bootstrap.CandidateBootstrapPrivilegeGate(self.capability, _development_source=object())
        source = self.development_source()
        source.verified_candidate_digest = 'sha256:' + '4' * 64
        with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, 'DEVELOPMENT_SOURCE_INVALID'):
            bootstrap.CandidateBootstrapPrivilegeGate(self.capability, _development_source=source)


if __name__ == '__main__':
    unittest.main()
