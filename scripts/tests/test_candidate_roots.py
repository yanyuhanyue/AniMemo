from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import tempfile
import unittest
from unittest import mock

from release import candidate
from scripts import candidate_vm_harness


class CandidateRootTests(unittest.TestCase):
    def test_native_default_is_fully_qualified_and_guest_stays_posix(self):
        self.assertTrue(candidate.VERIFIED_CANDIDATE_ROOT.is_absolute())
        expected = ('E:/var/lib/animemo/prepublication-candidates/v2'
                    if os.name == 'nt' else '/var/lib/animemo/prepublication-candidates/v2')
        self.assertEqual(candidate.VERIFIED_CANDIDATE_ROOT, Path(expected))
        self.assertEqual(candidate_vm_harness.GUEST_CANDIDATE_ROOT,
                         '/var/lib/animemo/prepublication-candidates/v2')

    def test_default_does_not_depend_on_current_directory(self):
        with mock.patch('pathlib.Path.cwd', side_effect=AssertionError('cwd is not authority')):
            self.assertEqual(candidate._candidate_state_root(None), candidate.VERIFIED_CANDIDATE_ROOT)

    def test_windows_lexical_inputs_are_not_promoted_to_trusted_roots(self):
        for raw in ('/var/lib/animemo', r'\var\lib\animemo', 'E:materials', 'materials',
                    'E:/safe/../outside', r'\\server\share\materials', r'\\?\E:\materials',
                    r'\\.\E:\materials', 'E:/MATERI~1', 'E:/materials.', 'E:/materials ',
                    'E:/materials:stream', 'E:/NUL', 'E:/wild*card', 'E:/bad\x01name'):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                candidate._validate_candidate_state_root_path(PureWindowsPath(raw))

    def test_supported_windows_spelling_and_posix_roots(self):
        for path in (PureWindowsPath('E:/材料/Valid Root'), PureWindowsPath(r'e:\材料\Valid Root'),
                     PurePosixPath('/var/lib/animemo/prepublication-candidates/v2')):
            with self.subTest(path=path):
                candidate._validate_candidate_state_root_path(path)
        for path in (PurePosixPath('relative'), PurePosixPath('/safe/../outside'),
                     PurePosixPath('//ambiguous/root')):
            with self.subTest(path=path), self.assertRaises(candidate.CandidateContractError):
                candidate._validate_candidate_state_root_path(path)

    def test_loader_rejects_relative_root_before_lookup(self):
        with mock.patch.object(candidate, '_locate_verified_candidate_root') as locate:
            with self.assertRaisesRegex(candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                candidate.load_verified_candidate('sha256:' + 'a' * 64, _state_root=Path('materials'))
            locate.assert_not_called()

    def test_writer_rejects_relative_root_before_filesystem_mutation(self):
        digest = 'sha256:' + 'a' * 64
        with mock.patch.object(candidate, 'validate_qualification_run_metadata',
                               return_value={'artifactId': 1, 'digest': digest}), \
                mock.patch('pathlib.Path.mkdir') as mkdir:
            with self.assertRaisesRegex(candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                candidate.verify_prepublication_candidate(archive=Path('unused.zip'), run_metadata={},
                    jobs_metadata={}, artifacts_metadata={}, containing_artifact_id=1,
                    containing_artifact_api_digest=digest, expected_run_id=1,
                    expected_source_sha='b' * 40, expected_source_tree='c' * 40,
                    expected_candidate_version='v2.0.0-rc.1', verified_at='2026-09-10T00:00:00Z',
                    _state_root=Path('materials'))
            mkdir.assert_not_called()

    def test_existing_non_directory_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ancestor = Path(temporary) / 'file'
            ancestor.write_bytes(b'not a directory')
            with self.assertRaisesRegex(candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                candidate._candidate_state_root(ancestor / 'state')

    @unittest.skipUnless(os.name == 'nt', 'real Windows junction semantics')
    def test_windows_junction_ancestor_is_rejected_before_write_or_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            link = root / 'junction'
            script = root / 'create-junction.ps1'
            script.write_text('param($LinkPath, $TargetPath)\n'
                'New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath -ErrorAction Stop | Out-Null\n',
                encoding='utf-8')
            created = subprocess.run(['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
                '-NoProfile', '-NonInteractive', '-File', str(script), str(link), str(target)],
                capture_output=True)
            self.assertEqual(created.returncode, 0, (created.stdout + created.stderr).decode('mbcs'))
            try:
                self.assertTrue(link.is_junction())
                for state in (link, link / 'state'):
                    with self.subTest(state=state), self.assertRaisesRegex(
                            candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                        candidate._candidate_state_root(state)
                self.assertFalse((target / 'state').exists())
            finally:
                # Remove this directory junction itself, never its target.
                os.rmdir(link)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink filesystem semantics')
    def test_symlink_ancestor_is_rejected_without_resolving_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.mkdir()
            link = root / 'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(candidate.CandidateContractError, '^VERIFIED_CANDIDATE_ROOT_INVALID$'):
                candidate._candidate_state_root(link / 'state')
            self.assertFalse((target / 'state').exists())


if __name__ == '__main__':
    unittest.main()
