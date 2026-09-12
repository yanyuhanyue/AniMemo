import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts import development_source as source


class DevelopmentProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = self.root / 'code'
        self.material = self.root / 'qualified'
        self.destination = self.root / 'projected'

    def write(self, root, files):
        result = {}
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            result[name] = 'sha256:' + hashlib.sha256(data).hexdigest()
        return result

    def project(self, code, material, tracked):
        return source.project_execution_tree(code_root=self.code, code_identities=code,
            material_root=self.material, material_identities=material,
            baseline_tracked_paths=set(tracked), destination=self.destination)

    def test_current_code_replaces_old_code_and_removals_preserve_only_qualified_extras(self):
        original = {'installer/production.py': b'old installer', 'updater/removed.py': b'deleted',
            'wheelhouse/frozen.whl': b'qualified wheel', 'release/pretrust.json': b'qualified trust'}
        material = self.write(self.material, original)
        current = {'installer/production.py': b'current installer', 'updater/new.py': b'new updater'}
        code = self.write(self.code, current)
        result = self.project(code, material, ['installer/production.py', 'updater/removed.py'])
        expected = {**current, **{name: data for name, data in original.items()
                                 if name.startswith(('wheelhouse/', 'release/'))}}
        self.assertEqual(set(result), set(expected))
        for name, data in expected.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        self.assertFalse((self.destination / 'updater/removed.py').exists())
        self.assertEqual({name: (self.material / name).read_bytes() for name in original}, original)

    def test_current_code_cannot_override_qualified_extra(self):
        material = self.write(self.material, {'wheelhouse/frozen.whl': b'qualified'})
        code = self.write(self.code, {'wheelhouse/frozen.whl': b'replaced'})
        with self.assertRaisesRegex(source.h.CandidateHarnessError, 'IMMUTABLE_MATERIAL_OVERRIDE'):
            self.project(code, material, [])
        self.assertFalse(self.destination.exists())

    def test_copy_rejects_unmatched_source_bytes(self):
        code = self.write(self.code, {'installer/current.py': b'current'})
        (self.code / 'installer/current.py').write_bytes(b'changed')
        with self.assertRaisesRegex(source.h.CandidateHarnessError, 'COPY_CHANGED'):
            self.project(code, {}, [])


if __name__ == '__main__':
    unittest.main()
