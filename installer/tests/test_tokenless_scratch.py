"""Independent Windows nested-hold regressions; genuine local Actions crypto."""
import json
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from bootstrap_kit import safe_files as files
from installer import tokenless_stage0 as stage0
from release.formal_windows_pretrust import assert_windows_private_acl
from updater.offline import PretrustedTrustMaterial

FIXTURE_ENV = 'ANIMEMO_TOKENLESS_REAL_FIXTURE'
CANARY = 'NONSECRET_SCRATCH_CLEANUP_CANARY'


@unittest.skipUnless(os.name == 'nt' and os.environ.get(FIXTURE_ENV),
                     'Windows with explicitly selected real component fixture')
class NestedScratchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(Path(os.environ[FIXTURE_ENV]).read_bytes())
        if fixture['authority'] != 'NON_AUTHORITATIVE_LOCAL_COMPONENT_FIXTURE':
            raise ValueError('Explicit component fixture required')
        cls.fixture_parent = Path(fixture['scratch_parent'])
        cls.material = PretrustedTrustMaterial.load(Path(fixture['profile_path']).parent)
        source = Path(__file__).resolve().parents[2] / 'release/release_attestation_verifier/testdata/github-actions-public'
        cls.bundle = (source / 'sha256-2588108838c23c9b7e29d70d3a897109bf93b5c52cc4bcf949d5434e51496459.jsonl').read_bytes()
        cls.request = json.loads((source / 'request.json').read_bytes())

    def setUp(self):
        self.root = files.create_private_directory(self.fixture_parent, prefix='nested-scratch-test-')
        self.identity = files.directory_identity(self.root)

    def tearDown(self):
        files.remove_owned_directory(self.root, self.identity)

    def test_real_actions_crypto_runs_beneath_two_already_held_parents(self):
        actual_capture, processes = stage0.capture_process, []
        def capture(*args, **kwargs):
            launch_errors = []
            actual_launch = subprocess.Popen
            def observe_launch(*arguments, **options):
                try:
                    if len(arguments[0][0]) >= 260:
                        self.assertTrue(options.get('executable', '').startswith('\\\\?\\'))
                        self.assertTrue(os.path.samefile(arguments[0][0], options['executable']))
                    return actual_launch(*arguments, **options)
                except OSError as error:
                    launch_errors.append({'winerror': error.winerror, 'errno': error.errno})
                    raise
            with mock.patch('installer.apt_diagnostics.subprocess.Popen', new=observe_launch):
                result = actual_capture(*args, **kwargs)
            processes.append(result)
            self.assertEqual(result.outcome, 'EXITED',
                {'launch_errors': launch_errors, 'returncode': result.returncode,
                 'executable_path_characters': len(args[0][0])})
            return result
        with files.hold_path_chain(self.root), stage0._scratch(self.root) as outer:
            assert_windows_private_acl(outer)
            with mock.patch.object(stage0, 'capture_process', side_effect=capture):
                claim = stage0.TokenlessActionsVerifier(self.material, scratch_parent=outer).verify(
                    bundle=self.bundle, evidence_name=self.request['evidenceName'],
                    subject_name=self.request['subject']['name'], subject_sha256=self.request['subject']['sha256'],
                    workflow=self.request['workflow'], source_commit=self.request['sourceCommit'])
            self.assertEqual(claim.subject_digest, self.request['subject']['sha256'])
            self.assertEqual(claim.source_commit, self.request['sourceCommit'])
            self.assertEqual(list(outer.iterdir()), [])
        self.assertEqual(len(processes), 1)  # Real unchanged Go executable, never a fake success.
        self.assertEqual(processes[0].outcome, 'EXITED')
        self.assertEqual(processes[0].returncode, 0)
        self.assertFalse(processes[0].secondary_errors)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_parent_and_scratch_rename_are_denied_during_holds(self):
        with files.hold_path_chain(self.root):
            with self.assertRaises(PermissionError):
                self.root.rename(self.root.with_name(self.root.name + '-renamed'))
            with stage0._scratch(self.root) as scratch:
                assert_windows_private_acl(scratch)
                with self.assertRaises(PermissionError):
                    scratch.rename(scratch.with_name(scratch.name + '-renamed'))
                self.assertEqual(files.directory_identity(self.root), self.identity)
                with files.exclusive_file(scratch / 'ordinary-output') as output:
                    output.write(b'ordinary nested output')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_create_to_hold_rebind_is_rejected_before_execution(self):
        original_hold = files.hold_path_chain
        swapped = []
        @contextmanager
        def rebind(path):
            path = Path(path)
            if path.name.startswith('tokenless-verifier-') and not swapped:
                previous = files.directory_identity(path)
                moved = path.with_name(path.name + '-original')
                path.rename(moved)
                path.mkdir()  # TEST_ONLY competing directory; no code is executed from it.
                swapped.append((path, moved, previous, files.directory_identity(path)))
            with original_hold(path):
                yield path
        entered = False
        with mock.patch.object(files, 'hold_path_chain', rebind), \
                mock.patch.object(stage0, 'capture_process') as execute:
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SCRATCH_CHANGED'), stage0._scratch(self.root):
                entered = True
                execute()
            execute.assert_not_called()
        self.assertFalse(entered)
        self.assertEqual(len(swapped), 1)
        current, previous, old_id, new_id = swapped[0]
        self.assertNotEqual(old_id, new_id)
        self.assertTrue(current.is_dir())
        self.assertTrue(previous.is_dir())
        # Known test-owned paths remain for ordinary fixture cleanup; _scratch
        # did not delete a directory whose identity differed from its creation.

    def test_cleanup_is_once_and_keeps_original_failure(self):
        failure = stage0.TokenlessStage0Error('BOOTSTRAP_TOKENLESS_SIGNATURE_REJECTED')
        with files.hold_path_chain(self.root), \
                mock.patch.object(files, 'remove_owned_directory', side_effect=OSError(CANARY)) as remove:
            with self.assertRaises(stage0.TokenlessStage0Error) as caught, stage0._scratch(self.root) as scratch:
                raise failure
            self.assertIs(caught.exception, failure)
            self.assertEqual(remove.call_count, 1)
            self.assertEqual(caught.exception.secondary_errors, ('BOOTSTRAP_TOKENLESS_CLEANUP_FAILED',))
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertTrue(scratch.is_dir())
        # Only a simulated OSError occurred. No real policy denial is retried.
