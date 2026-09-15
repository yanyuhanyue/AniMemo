"""Synthetic bytes exercise the real native file boundary, not simulated ACLs."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.tests import formal_test_materials as material
from updater.tests.test_source import stable_manifest


class FormalTestMaterialBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp).resolve()
        self.roots = {role: self.root / role for role in ('candidate', 'assets', 'sidecar')}
        for root in self.roots.values():
            root.mkdir()
        manifest = stable_manifest()
        manifest['release'].update(version='v1.0.0-rc.1', channel='rc', promotedFrom=None)
        manifest['releaseNotes']['tag'] = 'v1.0.0-rc.1'
        manifest['provenance']['workflow'] = '.github/workflows/release.yml'
        self.portable = 'animemo-v1.0.0-rc.1-portable.tar'
        values = {('assets', 'release-manifest.json'): json.dumps(manifest).encode(),
                  ('assets', self.portable): b'portable',
                  ('assets', 'installer-materials.tar'): b'installer',
                  ('sidecar', 'proof.json'): b'proof'}
        values.update({('candidate', 'installer-root/' + name): b'trust fixture'
                       for name in material.KIT_MEMBERS})
        self.members = []
        for (role, relative), value in values.items():
            path = self.roots[role] / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
            self.members.append(material.MaterialMember(role, relative, len(value),
                                'sha256:' + hashlib.sha256(value).hexdigest()))
        self.selection = material.SelectedMaterials(*self.roots.values(), 'proof.json', tuple(self.members))

    def snapshot(self, selection=None, environment=None, destination='snapshot'):
        with material.selected_materials(selection or self.selection):
            return material.snapshot_selected_materials(self.root / destination,
                                                       environment=environment or {})

    def update_bytes(self, role, relative, raw):
        (self.roots[role] / relative).write_bytes(raw)
        self.selection = replace(self.selection, members=tuple(
            replace(item, size=len(raw), sha256='sha256:' + hashlib.sha256(raw).hexdigest())
            if (item.role, item.relative) == (role, relative) else item
            for item in self.selection.members))

    def test_exact_independent_roots_snapshot_only_declared_members(self):
        ignored = self.roots['candidate'] / 'installer-root/unselected.txt'
        ignored.write_bytes(b'unselected')
        snapshot = self.snapshot()
        self.assertEqual(snapshot.payload.read_bytes(), b'portable')
        self.assertEqual(snapshot.installer_archive.read_bytes(), b'installer')
        self.assertEqual(sum(path.is_file() for path in snapshot.root.rglob('*')), len(self.members))
        self.assertFalse((snapshot.root / 'unselected.txt').exists())
        (self.roots['assets'] / self.portable).write_bytes(b'changed source')
        self.assertEqual(snapshot.payload.read_bytes(), b'portable')

    def test_default_optional_skip_and_selected_missing_block(self):
        self.assertIsNone(material.snapshot_selected_materials(self.root / 'unused', environment={}))
        with self.assertRaises(material.TestMaterialsBlocked):
            material.snapshot_selected_materials(self.root / 'unused', environment={material.SELECTORS[0]: str(self.roots['candidate'])})
        (self.roots['sidecar'] / 'proof.json').unlink()
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot()

    def test_all_environment_sources_require_exact_independent_selection(self):
        with mock.patch.object(material, '_root', side_effect=AssertionError('unsafe input touched disk')):
            for name in material.SELECTORS:
                for value in ('../escape', str(self.root / 'candidate-evil'), 'Z:/wrong-drive',
                              r'\\host\share\file', r'\\.\pipe\x', str(self.root / 'x:stream')):
                    with self.subTest(name=name, value=value), self.assertRaises(material.TestMaterialsBlocked):
                        self.snapshot(environment={name: value})

    def test_member_traversal_absolute_ads_and_duplicates_reject_before_copy(self):
        for relative in ('../escape', '/absolute', 'D:/foreign', r'..\escape', 'name:stream',
                         'dir//file', 'dir/./file', 'NUL', 'bad. '):
            selection = replace(self.selection, members=(replace(self.members[0], relative=relative), *self.members[1:]))
            with self.subTest(relative=relative), mock.patch.object(material, '_copy_member') as copy:
                with self.assertRaises(material.TestMaterialsBlocked):
                    self.snapshot(selection)
                copy.assert_not_called()
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(replace(self.selection, members=(*self.members, self.members[0])))

    def test_duplicate_json_and_malformed_versions_never_form_portable_path(self):
        path = self.roots['assets'] / 'release-manifest.json'
        original = path.read_bytes()
        cases = [b'{"release":{},"release":{}}', b'[]']
        for version in ('../../escape', 'v1.0.0-rc.1/escape', 'v1.0.0-rc.0', 'v1.0.0:ads'):
            value = json.loads(original)
            value['release']['version'] = version
            cases.append(json.dumps(value).encode())
        for index, raw in enumerate(cases):
            self.update_bytes('assets', 'release-manifest.json', raw)
            with self.subTest(index=index), mock.patch.object(material, '_copy_member', wraps=material._copy_member) as copy:
                with self.assertRaises(material.TestMaterialsBlocked):
                    self.snapshot(destination=f'bad-manifest-{index}')
                self.assertEqual(copy.call_count, 1)

    def test_undeclared_and_changed_installer_archive_rejected(self):
        selection = replace(self.selection, members=tuple(item for item in self.members
                                                        if item.relative != 'installer-materials.tar'))
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(selection)
        (self.roots['assets'] / 'installer-materials.tar').write_bytes(b'corrupted')
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(destination='changed-archive')

    def test_hardlink_rejected_before_source_open(self):
        path = self.roots['assets'] / self.portable
        os.link(path, self.root / 'hardlink')
        with mock.patch.object(material, '_source', wraps=material._source) as source:
            with self.assertRaises(material.TestMaterialsBlocked):
                self.snapshot()
            self.assertEqual(source.call_count, 1)  # manifest only

    def test_directory_in_place_of_file_rejected(self):
        path = self.roots['sidecar'] / 'proof.json'
        path.unlink()
        path.mkdir()
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot()

    def test_size_and_digest_bound_to_independent_inventory(self):
        for index, changed in enumerate((replace(self.members[0], size=material.MAX_ARCHIVE + 1),
                                         replace(self.members[0], sha256='sha256:' + '0' * 64))):
            with self.subTest(index=index), self.assertRaises(material.TestMaterialsBlocked):
                self.snapshot(replace(self.selection, members=(changed, *self.members[1:])), destination=f'size-{index}')

    def test_leaf_replacement_between_precheck_and_open_fails_closed(self):
        original = material._source
        path = self.roots['sidecar'] / 'proof.json'
        @contextmanager
        def replace_leaf(source, maximum, **kwargs):
            if source == path:
                replacement = self.root / 'replacement'
                replacement.write_bytes(b'other')
                os.replace(replacement, path)
            with original(source, maximum, **kwargs) as stream:
                yield stream
        with mock.patch.object(material, '_source', replace_leaf), self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot()

    @unittest.skipUnless(os.name == 'nt', 'Real Windows junction semantics')
    def test_windows_junction_root_and_nested_member_rejected(self):
        link = self.root / 'junction'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(self.roots['candidate'])],
                       check=True, capture_output=True)
        self.addCleanup(link.rmdir)
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(replace(self.selection, candidate_root=link))
        nested = self.roots['candidate'] / 'installer-root' / material.INITIAL_TRUST_KIT_PREFIX
        moved = self.root / 'moved-kit'
        nested.rename(moved)
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(nested), str(moved)],
                       check=True, capture_output=True)
        self.addCleanup(nested.rmdir)
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(destination='nested-junction')

    @unittest.skipUnless(os.name == 'nt', 'Real Windows file sharing semantics')
    def test_windows_held_source_denies_concurrent_replacement(self):
        path = self.roots['assets'] / self.portable
        with material._source(path, material.MAX_ARCHIVE):
            with self.assertRaises(OSError):
                path.write_bytes(b'evil')
            with self.assertRaises(OSError):
                path.unlink()
            with self.assertRaises(OSError):
                path.parent.rename(self.root / 'rebound-assets')

    @unittest.skipUnless(os.name == 'posix', 'Real POSIX symlink and FIFO semantics')
    def test_posix_symlink_and_fifo_rejected_without_open(self):
        path = self.roots['sidecar'] / 'proof.json'
        path.unlink()
        path.symlink_to(self.roots['assets'] / self.portable)
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(material.TestMaterialsBlocked):
            self.snapshot(destination='fifo')


if __name__ == '__main__':
    unittest.main()
