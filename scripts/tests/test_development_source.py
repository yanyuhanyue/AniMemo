import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class DevelopmentMaterialCompatibilityTests(unittest.TestCase):
    MATERIAL = '1' * 40
    EXECUTION = '2' * 40
    PERMITTED_PATHS = (
        'deploy/install-updater.sh',
        'sites/install-portal/app.js',
        'tests/install-portal-static.test.mjs',
        'tests/test_install_bootstrap.py',
    )

    def evaluate(self, paths, *, ancestor_error=None, material=None, execution=None):
        with mock.patch.object(source.subprocess, 'run', side_effect=ancestor_error) as ancestor, \
                mock.patch.object(source.subprocess, 'check_output',
                    return_value=('\0'.join(paths) + '\0').encode('utf-8')) as changed:
            try:
                return source.require_material_compatibility(
                    self.MATERIAL if material is None else material,
                    self.EXECUTION if execution is None else execution)
            finally:
                self.ancestor, self.changed = ancestor, changed

    def test_each_exact_projected_or_non_runtime_path_is_compatible(self):
        for name in self.PERMITTED_PATHS:
            with self.subTest(path=name):
                self.assertIsNone(self.evaluate([name]))

    def test_combined_paths_keep_exact_ancestry_and_diff_checks(self):
        self.assertIsNone(self.evaluate(self.PERMITTED_PATHS))
        self.ancestor.assert_called_once_with(
            ['git', '-C', str(source.ROOT), 'merge-base', '--is-ancestor',
             self.MATERIAL, self.EXECUTION], check=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        self.changed.assert_called_once_with(
            ['git', '-C', str(source.ROOT), 'diff', '--name-only', '-z',
             self.MATERIAL, self.EXECUTION], timeout=30)

    def test_staging_script_must_remain_in_exact_projection(self):
        fixed = tuple(name for name in source._FIXED_DEPLOYMENT_FILES
                      if name != 'deploy/install-updater.sh')
        with mock.patch.object(source, '_FIXED_DEPLOYMENT_FILES', fixed), \
                self.assertRaisesRegex(source.h.CandidateHarnessError,
                                       'DEVELOPMENT_LOCAL_MATERIAL_REBUILD_REQUIRED'):
            self.evaluate(self.PERMITTED_PATHS)

    def test_mixed_unsafe_material_or_unknown_path_stays_rejected(self):
        unsafe = (
            'deploy/unknown.sh', 'deploy/install-updater.sh.bak',
            'deploy/release-producer.Dockerfile', 'deploy/docker-compose.yml',
            'deploy/updater/animemo', 'deploy/updater/animemo-updater@.service',
            'sites/install-portal/other.js', 'tests/other.py', 'tests/other.test.mjs',
            'release/release_attestation_verifier/main.go',
            'release/release_attestation_verifier/go.mod',
            'release/release_attestation_verifier/go.sum',
            'release/requirements.txt', 'durability/requirements.txt',
            'wheelhouse/frozen.whl', 'release/wheels/frozen.whl',
            'oci/app.tar', 'Dockerfile', 'api/app.py', 'backend/app.py',
        )
        for name in unsafe:
            with self.subTest(path=name), self.assertRaisesRegex(
                    source.h.CandidateHarnessError, 'DEVELOPMENT_LOCAL_MATERIAL_REBUILD_REQUIRED'):
                self.evaluate([*self.PERMITTED_PATHS, name])

    def test_existing_source_documentation_and_ci_stay_compatible(self):
        self.assertIsNone(self.evaluate([
            'installer/runtime.py', 'bootstrap_kit/manifest.py',
            'scripts/development_source.py', 'docs/installer.md',
            '.github/workflows/check.yml', '.gitattributes']))

    def test_non_ancestor_stops_before_changed_path_lookup(self):
        error = subprocess.CalledProcessError(1, ['git', 'merge-base'])
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.evaluate(self.PERMITTED_PATHS, ancestor_error=error)
        self.assertIs(caught.exception, error)
        self.changed.assert_not_called()

    def test_git_ancestry_failure_stops_before_changed_path_lookup(self):
        error = subprocess.TimeoutExpired(['git', 'merge-base'], 30)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.evaluate(self.PERMITTED_PATHS, ancestor_error=error)
        self.assertIs(caught.exception, error)
        self.changed.assert_not_called()

    def test_invalid_source_identity_is_rejected_before_git(self):
        for field in ('material', 'execution'):
            for value in ('', 'a' * 39, 'G' * 40, 123, True, b'a' * 40):
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(source.h.CandidateHarnessError,
                                                'DEVELOPMENT_SOURCE_BINDING_INVALID'):
                        self.evaluate(self.PERMITTED_PATHS, **{field: value})
                    self.ancestor.assert_not_called()
                    self.changed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
