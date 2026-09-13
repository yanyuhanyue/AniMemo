import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import development_controller as controller
from scripts import development_session_owner as owners


class DevelopmentControllerTests(unittest.TestCase):
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
                with controller.round_modules(checkout, self.inventory(checkout)) as entry:
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
            with self.assertRaises(owners.DevelopmentOwnerError), controller.round_modules(root, inventory):
                self.fail('changed code imported')
            entry.write_text('from scripts import untracked\n', encoding='utf-8')
            inventory = self.inventory(root)
            (root / 'scripts/untracked.py').write_text('raise AssertionError("untracked code executed")\n', encoding='utf-8')
            with self.assertRaises(ModuleNotFoundError), controller.round_modules(root, inventory):
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
                return controller.verify_checkout(root, source_sha=sha, source_tree=tree, common_directory=root / '.git')
            self.assertIn('scripts/local_candidate_development.py', verify())
            source.write_bytes(b'raise AssertionError("must never execute")\n')
            with self.assertRaises(owners.DevelopmentOwnerError):
                verify()
            source.write_bytes(b'VALUE = 1\n')
            controller._git(root, 'rm', '--cached', '--', 'scripts/local_candidate_development.py')
            with self.assertRaises(owners.DevelopmentOwnerError):
                verify()

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
