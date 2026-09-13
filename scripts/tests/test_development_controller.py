import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import development_controller as controller
from scripts import development_session_owner as owners


class DevelopmentControllerTests(unittest.TestCase):
    def test_status_failure_restart_requires_closed_owner_zero_use_and_full_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = ('FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE')
            role = {'delivery_attempts': 0, 'delivery_completed': 0, 'operation_result': 'NOT_RUN',
                'target_verified': False, 'lease_verified': False}
            material = {'verified_candidate_digest': 'sha256:' + 'a' * 64,
                'source_sha': 'b' * 40, 'source_tree': 'c' * 40, 'qualification_run_id': 123}
            owner = {'schema': 'animemo.local-development-memory-owner/v1', 'state': 'CLOSED',
                'close_reason': 'DEVELOPMENT_CONTROLLER_STATUS_FAILED', 'secret_cleanup': 'BEST_EFFORT_COMPLETED',
                'capture_attempts': 1, 'capture_completed': 1, 'owner_id': 'd' * 32, 'last_reserved_round': 9,
                'material_identity': material}
            previous = {'status': 'FAIL', 'source_preserved': True, 'cleanup_errors': [],
                'private_material_root_released': True, 'private_execution_source_root_released': True,
                'private_material_root': str(root / 'absent-material'), 'private_execution_source_root': str(root / 'absent-source'),
                'failure_code': 'DEVELOPMENT_SESSION_CLOSED', 'credential_session': {
                    'session_capture_attempts': 0, 'session_capture_completed': 0, 'development_owner_id': owner['owner_id'],
                    'profiles': {profile: {name: dict(role) for name in ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO', 'CANDIDATE_WORKLOAD')}
                        for profile in profiles}},
                'profile_results': {profile: {'status': 'ERROR' if profile == 'FRESH_BASE' else 'NOT_RUN_SHARED_BLOCKER'}
                    for profile in profiles},
                'profile_operations': {'FRESH_BASE': {'power_state': 'STOPPED', 'clone_disposition': 'QUARANTINED',
                    'cleanup_errors': [], 'session_keys_removed': True, 'known_hosts_removed': True, 'lease_released': True}},
                'plan': {'verifiedCandidateDigest': material['verified_candidate_digest'], 'materialSourceSha': material['source_sha'],
                    'materialSourceTree': material['source_tree'], 'qualificationRunId': material['qualification_run_id']}}
            result = root / 'round-0010-result.json'
            def validate(report, final):
                result.write_text(json.dumps(report), encoding='utf-8')
                (root / 'owner-final.json').write_text(json.dumps(final), encoding='utf-8')
                return controller.validate_previous_session(result)
            self.assertEqual(validate(previous, owner)[2], 9)
            for key, value in (('state', 'READY'), ('close_reason', 'DEVELOPMENT_SESSION_EXPIRED'),
                    ('secret_cleanup', 'UNKNOWN'), ('last_reserved_round', 12), ('owner_id', 'e' * 32)):
                with self.subTest(key=key), self.assertRaises(owners.DevelopmentOwnerError):
                    validate(previous, {**owner, key: value})
            for path, value in (
                (('credential_session', 'session_capture_attempts'), 1),
                (('credential_session', 'profiles', 'FRESH_BASE', 'BOOTSTRAP_ROTATION', 'delivery_attempts'), 1),
                (('profile_operations', 'FRESH_BASE', 'power_state'), 'RUNNING'),
                (('profile_operations', 'FRESH_BASE', 'lease_released'), False),
                (('profile_results', 'DOCKER_BASE', 'status'), 'PASS'),
                (('plan', 'materialSourceSha'), 'f' * 40),
            ):
                changed = copy.deepcopy(previous)
                entry = changed
                for part in path[:-1]:
                    entry = entry[part]
                entry[path[-1]] = value
                with self.subTest(path=path), self.assertRaises(owners.DevelopmentOwnerError):
                    validate(changed, owner)
            (root / 'absent-source').mkdir()
            with self.assertRaises(owners.DevelopmentOwnerError):
                validate(previous, owner)

    def test_status_retry_is_bounded_and_unknown_errors_are_not_retried(self):
        for code, attempts in ((5, 51), (32, 51), (33, 51), (2, 1), (None, 1)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                failure = OSError('synthetic status replacement error')
                if code is not None:
                    failure.winerror = code
                stopped = mock.Mock()
                stopped.wait.return_value = False
                with (
                    mock.patch.object(controller.os, 'replace', side_effect=failure) as replace,
                    self.assertRaises(OSError),
                ):
                    controller.publish_status(Path(directory), {'synthetic': True}, stopped=stopped)
                self.assertEqual(replace.call_count, attempts)
                self.assertEqual(stopped.wait.call_count, attempts - 1)

    def test_status_retry_stops_without_rewriting_or_a_tail_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            failure = OSError('synthetic sharing error')
            failure.winerror = 5
            stopped = threading.Event()
            stopped.set()
            root = Path(directory)
            with mock.patch.object(controller.os, 'replace', side_effect=failure) as replace:
                controller.publish_status(root, {'synthetic': True}, stopped=stopped)
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(json.loads((root / 'status.next.json').read_bytes()), {'synthetic': True})

    @unittest.skipUnless(os.name == 'nt', 'Windows atomic replacement sharing semantics')
    def test_status_publication_survives_a_brief_ordinary_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'status.json'
            target.write_bytes(b'{"synthetic":1}\n')
            reader = target.open('rb')
            released = threading.Timer(0.2, reader.close)
            released.start()
            try:
                controller.publish_status(root, {'synthetic': 2}, stopped=threading.Event())
                self.assertEqual(json.loads(target.read_bytes()), {'synthetic': 2})
                self.assertFalse((root / 'status.next.json').exists())
            finally:
                released.join()
                reader.close()

    def inventory(self, checkout):
        return {path.relative_to(checkout).as_posix(): hashlib.sha1(
            b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            for path in checkout.rglob('*.py') if (data := path.read_bytes()) is not None}

    def test_round_modules_load_new_root_and_keep_only_fixed_owner_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name in ('first', 'second'):
                checkout = Path(temporary) / name
                (checkout / 'scripts').mkdir(parents=True)
                (checkout / 'scripts/round_helper.py').write_text('VALUE = ' + repr(name), encoding='utf-8')
                (checkout / 'scripts/local_candidate_development.py').write_text(
                    'from scripts import round_helper\n'
                    'from scripts.development_session_owner import DevelopmentSessionOwner\n'
                    'from scripts.development_capture_scope import DevelopmentScopeOwner\n'
                    'VALUE = round_helper.VALUE\n', encoding='utf-8')
                from scripts import development_capture_scope as scope
                with controller.round_modules(checkout, self.inventory(checkout), {}) as entry:
                    self.assertEqual(entry.VALUE, name)
                    self.assertIs(entry.DevelopmentSessionOwner, owners.DevelopmentSessionOwner)
                    self.assertIs(entry.DevelopmentScopeOwner, scope.DevelopmentScopeOwner)
                    self.assertEqual(Path(entry.__file__), checkout / 'scripts/local_candidate_development.py')

    def test_importer_rejects_replacement_and_untracked_module_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            entry = root / 'scripts/local_candidate_development.py'
            entry.write_text('VALUE = "verified"\n', encoding='utf-8')
            inventory = self.inventory(root)
            entry.write_text('raise AssertionError("unverified code executed")\n', encoding='utf-8')
            with self.assertRaises(owners.DevelopmentOwnerError), controller.round_modules(root, inventory, {}):
                self.fail('changed code imported')
            entry.write_text('from scripts import untracked\n', encoding='utf-8')
            inventory = self.inventory(root)
            (root / 'scripts/untracked.py').write_text('raise AssertionError("untracked code executed")\n', encoding='utf-8')
            with self.assertRaises(ModuleNotFoundError), controller.round_modules(root, inventory, {}):
                self.fail('untracked code imported')

    @unittest.skipUnless(Path(controller.GIT).is_file(), 'fixed Windows git')
    def test_preimport_checkout_check_requires_exact_clean_git_bytes_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            source = root / 'scripts/local_candidate_development.py'
            source.write_bytes(b'VALUE = 1\n')
            controller._git(root, 'init', '-q')
            controller._git(root, 'add', '.')
            controller._git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'synthetic source')
            sha = controller._git(root, 'rev-parse', 'HEAD').decode().strip()
            tree = controller._git(root, 'rev-parse', 'HEAD^{tree}').decode().strip()
            def verify():
                return controller.verify_checkout(root, source_sha=sha, source_tree=tree,
                    common_directory=root / '.git', stable={'scripts/local_candidate_development.py': hashlib.sha256(b'VALUE = 1\n').hexdigest()})
            self.assertIn('scripts/local_candidate_development.py', verify())
            source.write_bytes(b'raise AssertionError("must never execute")\n')
            with self.assertRaises(owners.DevelopmentOwnerError):
                verify()
            source.write_bytes(b'VALUE = 1\n')
            controller._git(root, 'rm', '--cached', '--', 'scripts/local_candidate_development.py')
            with self.assertRaises(owners.DevelopmentOwnerError):
                verify()

    def test_stable_identity_is_bound_to_the_actual_bytes_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            path = root / 'scripts/local_candidate_development.py'
            path.write_bytes(b'VALUE = "changed control source"\n')
            stable = {'scripts/local_candidate_development.py': hashlib.sha256(b'VALUE = "original"\n').hexdigest()}
            # Even an internally consistent new Git blob cannot replace a
            # fixed controller module accepted by a previous path observation.
            with self.assertRaises(owners.DevelopmentOwnerError), controller.round_modules(root, self.inventory(root), stable):
                self.fail('changed stable code imported')

    def test_requests_bind_owner_next_round_previous_result_and_frozen_control_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            checkout = parent / 'next'
            for name in controller.STABLE_FILES:
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixed controller source\n')
            stable = controller.stable_identities(checkout)
            owner = {'owner_id': 'a' * 32, 'last_reserved_round': 9, 'capture_limit': 12}
            previous = 'sha256:' + hashlib.sha256(b'previous result').hexdigest()
            value = {'schema': 'animemo.local-development-round-request/v1', 'owner_id': owner['owner_id'],
                'round_index': 10, 'previous_result_sha256': previous, 'checkout': str(checkout),
                'source_sha': 'b' * 40, 'source_tree': 'c' * 40}
            def validate(value):
                return controller.validate_request(value, owner_record=owner, previous_digest=previous,
                    checkout_parent=parent, stable=stable)
            self.assertEqual(validate(value), checkout)
            for changed in ({'owner_id': 'd' * 32}, {'round_index': 9}, {'round_index': True},
                    {'round_index': 13}, {'previous_result_sha256': 'sha256:' + 'f' * 64},
                    {'command': 'arbitrary command'}, {'checkout': str(parent.parent)}, {'source_sha': 'HEAD'}):
                with self.assertRaises(owners.DevelopmentOwnerError):
                    validate({**value, **changed})
            (checkout / controller.STABLE_FILES[0]).write_bytes(b'changed controller source\n')
            with self.assertRaises(owners.DevelopmentOwnerError):
                validate(value)

    def test_public_requests_reject_duplicate_keys_and_output_cannot_overwrite(self):
        with self.assertRaises(owners.DevelopmentOwnerError):
            controller._json(b'{"owner_id":"first","owner_id":"second"}')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'result.json'
            digest = controller._write_new(path, {'result': 'PASS'})
            self.assertEqual(digest, 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(json.loads(path.read_bytes()), {'result': 'PASS'})
            with self.assertRaises(FileExistsError):
                controller._write_new(path, {'result': 'FAIL'})
