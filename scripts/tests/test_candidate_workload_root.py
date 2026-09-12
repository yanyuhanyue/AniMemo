"""Real POSIX descriptor tests; no VM, sudo credential, or external access."""
from __future__ import annotations

import os
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import closed_runtime_inventory as inventory
from scripts import candidate_workload_root as root
from scripts import candidate_diagnostics as diagnostics, candidate_runtime_entry


def root_namespace():
    namespace = {'__name__': '_animemo_fixed_root'}
    for module in (inventory, root):
        exec(compile(Path(module.__file__).read_text(encoding='utf-8'), module.__file__, 'exec'), namespace)
    return namespace


@unittest.skipUnless(inventory._descriptor_inventory_available(), 'POSIX no-follow descriptor operations required')
class RootStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'
        self.destination = self.base / 'destination'
        self.source.mkdir()
        self.destination.mkdir()
        self.namespace = root_namespace()
        self.copy = self.namespace['_copy_stage']
        # The production seal is owner read/execute. Permit temporary test
        # cleanup without changing the production behavior being verified.
        def allow_cleanup():
            for directory, _, _ in os.walk(self.destination):
                os.chmod(directory, 0o700)
        self.addCleanup(allow_cleanup)

    def transfer(self):
        source = inventory._open_directory_chain(self.source)
        destination = inventory._open_directory_chain(self.destination)
        try:
            self.copy(source, destination)
        finally:
            os.close(source)
            os.close(destination)

    def test_actual_copied_bytes_close_the_same_inventory_and_lose_write_access(self):
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'large.bin').write_bytes(b'qualified-bytes' * 100000)
        (self.source / 'empty').write_bytes(b'')
        self.transfer()
        self.assertEqual(inventory.closed_runtime_inventory_digest(self.source),
                         inventory.closed_runtime_inventory_digest(self.destination))
        for path in self.destination.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o500)

    def test_descriptor_digest_matches_host_full_path_order_for_overlapping_names(self):
        from scripts import candidate_vm_harness as host
        (self.source / 'a').mkdir()
        (self.source / 'a' / 'file').write_bytes(b'nested')
        (self.source / 'a.txt').write_bytes(b'sibling')
        self.transfer()
        self.assertEqual(host._closed_runtime_inventory_digest(self.source),
                         inventory.closed_runtime_inventory_digest(self.destination))

    def test_symlink_hardlink_and_fifo_are_rejected_without_reading_them(self):
        outside = self.base / 'outside'
        outside.write_bytes(b'outside-boundary')
        path = self.source / 'hostile'
        for kind in ('symlink', 'hardlink', 'fifo', 'directory-symlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink': path.symlink_to(outside)
                elif kind == 'hardlink': os.link(outside, path)
                elif kind == 'fifo': os.mkfifo(path)
                else: path.symlink_to(self.base, target_is_directory=True)
                try:
                    with self.assertRaises(ValueError):
                        self.transfer()
                finally:
                    path.unlink()
                self.assertFalse((self.destination / 'hostile').exists())

    def test_source_component_symlink_is_rejected_before_opening_leaf(self):
        link = self.base / 'alias'
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            inventory._open_directory_chain(link)

    def test_preoccupied_destination_is_not_replaced(self):
        (self.source / 'one').write_bytes(b'new')
        (self.destination / 'one').write_bytes(b'previous-evidence')
        with self.assertRaises(FileExistsError):
            self.transfer()
        self.assertEqual((self.destination / 'one').read_bytes(), b'previous-evidence')

    def test_fifo_substitution_between_stat_and_open_is_nonblocking_and_rejected(self):
        path = self.source / 'one'
        path.write_bytes(b'bytes')
        original = os.open
        swapped = False
        def open_file(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == 'one' and flags & os.O_NONBLOCK and not swapped:
                path.unlink()
                os.mkfifo(path)
                swapped = True
            return original(name, flags, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=open_file):
            with self.assertRaisesRegex(ValueError, 'STAGE_CHANGED'):
                self.transfer()

    def test_source_growth_and_directory_change_are_rejected(self):
        path = self.source / 'one'
        path.write_bytes(b'bytes')
        original = os.read
        changed = False
        def read_file(fd, size):
            nonlocal changed
            if not changed:
                with path.open('ab') as output: output.write(b'additional')
                changed = True
            return original(fd, size)
        with mock.patch.object(os, 'read', side_effect=read_file):
            with self.assertRaisesRegex(ValueError, 'STAGE_CHANGED'):
                self.transfer()

    def test_root_parent_rejects_writable_ancestor(self):
        with self.assertRaisesRegex(ValueError, 'PARENT_UNTRUSTED'):
            self.namespace['_root_directory'](self.base / 'must-not-be-created')


class FixedProgramTests(unittest.TestCase):
    def test_embedded_sources_compile_without_importing_staged_modules(self):
        namespace = root_namespace()
        self.assertIn('run_fixed_candidate', namespace)
        self.assertIs(namespace['closed_runtime_inventory_digest'].__globals__, namespace)


@unittest.skipUnless(inventory._descriptor_inventory_available(), 'POSIX fixed-root integration')
class RootChainIntegrationTests(unittest.TestCase):
    """Project only root identity/locations; copying and child execution are real."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.session = 'a' * 32
        self.profile = 'FRESH_BASE'
        self.stage = self.base / 'tmp' / ('animemo-candidate-' + self.session + '-' + self.profile)
        self.receipt = self.base / 'receipt' / 'profile-receipt-draft.json'
        self.marker = self.base / 'runner.started'
        self.authority = self.base / 'authority'
        self.input_digest = 'sha256:' + 'b' * 64
        source = self.stage / 'installer-root'
        (source / 'installer').mkdir(parents=True)
        (source / 'scripts').mkdir()
        for module in (diagnostics, candidate_runtime_entry):
            (source / 'scripts' / Path(module.__file__).name).write_bytes(Path(module.__file__).read_bytes())
        (source / 'wheelhouse').mkdir()
        (source / 'wheelhouse' / 'synthetic.whl').write_bytes(b'public synthetic wheel fixture')
        (source / 'installer' / '__init__.py').write_bytes(b'')
        (source / 'installer' / 'offline_python_runtime.py').write_text(
            "def install_wheel_runtime(wheelhouse,target):\n"
            " assert (wheelhouse/'synthetic.whl').read_bytes()==b'public synthetic wheel fixture'\n"
            " target.mkdir(mode=0o700)\n"
            " (target/'qualified_fixture.py').write_text('verified=True\\n')\n", encoding='utf-8')
        self.runner = source / 'scripts' / 'candidate_profile_runner.py'
        self.runner.write_text(
            "import json,os,sys\nfrom pathlib import Path\nimport qualified_fixture\n"
            "assert qualified_fixture.verified\n"
            "assert sys.argv[1:]==['--verified-candidate-digest','sha256:" + 'c' * 64 +
            "','--profile','FRESH_BASE','--public-origin','https://candidate.invalid','--execute']\n"
            + 'Path(' + repr(str(self.marker)) + ").write_bytes(b'qualified runner reached')\n"
            + 'receipt=Path(' + repr(str(self.receipt)) + ')\n'
            + "with receipt.open('xb') as output: output.write(b'{\"result\":\"PASS\",\"synthetic\":true}\\n')\n"
            + "os.chmod(receipt,0o600)\n", encoding='utf-8')
        self.expected = inventory.closed_runtime_inventory_digest(self.stage)
        self.namespace = root_namespace()
        mapping = {'/tmp': self.base / 'tmp',
            '/var/lib/animemo/prepublication-candidates/v2': self.authority,
            '/var/lib/animemo/candidate-acceptance/profile-receipt-draft.json': self.receipt}
        self.namespace['Path'] = lambda value: mapping.get(str(value), Path(value))
        def test_private_root(path):
            self.assertIn(path, (self.authority, self.receipt.parent))
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            return inventory._open_directory_chain(path)
        self.namespace['_root_directory'] = test_private_root
        self.output = tempfile.TemporaryFile()
        self.addCleanup(self.output.close)
        self.operation = 'sha256:' + 'd' * 64
        self.diagnostic = diagnostics.DiagnosticWriter(self.output.fileno(), self.operation)
        def allow_cleanup():
            for directory, _, _ in os.walk(self.base): os.chmod(directory, 0o700)
        self.addCleanup(allow_cleanup)

    def run_chain(self):
        # The test user owns the projected private roots. Only UID is projected
        # to root; inode, link count, modes, sizes and timestamps remain actual.
        original = os.fstat
        def projected_stat(fd):
            value = original(fd)
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            fields['st_uid'] = 0
            return SimpleNamespace(**fields)
        previous_directory = os.getcwd()
        previous_mask = os.umask(0o077)
        os.umask(previous_mask)
        try:
            with (mock.patch.object(os, 'geteuid', return_value=0), mock.patch.object(os, 'fstat', side_effect=projected_stat),
                  mock.patch.dict(os.environ)):
                self.namespace['run_fixed_candidate'](session_id=self.session, profile=self.profile,
                    input_digest=self.input_digest, verified_digest='sha256:' + 'c' * 64,
                    inventory_digest=self.expected, context={'synthetic': True}, diagnostic=self.diagnostic)
        finally:
            os.chdir(previous_directory)
            os.umask(previous_mask)

    def test_complete_seal_wheel_runner_and_receipt_chain_uses_real_child_and_fds(self):
        self.run_chain()
        self.assertTrue(self.marker.is_file())
        observed = self.read_frames()
        self.assertEqual(observed.receipt, {'result': 'PASS', 'synthetic': True})
        self.assertEqual(observed.public()['exit_codes']['RUNTIME_RUNNER'], 0)
        self.assertEqual(observed.public()['last_stage'], 'DRAFT_RETURNED')
        destination = self.authority / self.input_digest.removeprefix('sha256:')
        self.assertEqual(inventory.closed_runtime_inventory_digest(destination), self.expected)

    def test_inventory_mismatch_never_executes_the_staged_runner(self):
        self.runner.write_text("raise RuntimeError('untrusted replacement')\n", encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'INVENTORY_MISMATCH'):
            self.run_chain()
        self.assertFalse(self.marker.exists())
        self.assertIn('MATERIAL_INVENTORY_MISMATCH', self.read_frames().public()['errors'])

    def read_frames(self):
        self.output.seek(0)
        reader = diagnostics.DiagnosticReader(self.operation)
        while item := diagnostics.read_frame(self.output):
            reader.accept(*item)
        return reader

    def test_actual_runtime_import_failure_reports_stage_and_exit(self):
        self.runner.write_text("import missing_animemo_development_fixture\n", encoding='utf-8')
        self.expected = inventory.closed_runtime_inventory_digest(self.stage)
        with self.assertRaisesRegex(ValueError, 'PROFILE_EXECUTION_FAILED'):
            self.run_chain()
        observed = self.read_frames().public()
        self.assertIn('RUNNER_INITIALIZATION_FAILED', observed['errors'])
        self.assertEqual(observed['exit_codes']['RUNTIME_RUNNER'], 2)
        self.assertEqual(observed['last_stage'], 'RUNNER_STARTING')

    def test_actual_runtime_initialization_failure_never_runs_runner(self):
        wheel = self.stage / 'installer-root' / 'wheelhouse' / 'synthetic.whl'
        wheel.write_bytes(b'invalid development wheel')
        self.expected = inventory.closed_runtime_inventory_digest(self.stage)
        with self.assertRaisesRegex(ValueError, 'PROFILE_EXECUTION_FAILED'):
            self.run_chain()
        observed = self.read_frames().public()
        self.assertIn('RUNTIME_INITIALIZATION_FAILED', observed['errors'])
        self.assertEqual(observed['last_stage'], 'RUNTIME_INITIALIZING')
        self.assertEqual(observed['exit_codes']['RUNTIME_RUNNER'], 2)
        self.assertFalse(self.marker.exists())

    def test_real_runner_nonzero_is_not_inferred_as_authentication_failure(self):
        self.runner.write_text('raise SystemExit(17)\n', encoding='utf-8')
        self.expected = inventory.closed_runtime_inventory_digest(self.stage)
        with self.assertRaisesRegex(ValueError, 'PROFILE_EXECUTION_FAILED'):
            self.run_chain()
        observed = self.read_frames().public()
        self.assertEqual(observed['exit_codes']['RUNTIME_RUNNER'], 17)
        self.assertIn('RUNNER_EXECUTION_FAILED', observed['errors'])

    def test_missing_draft_does_not_turn_successful_child_into_receipt(self):
        self.runner.write_text('raise SystemExit(0)\n', encoding='utf-8')
        self.expected = inventory.closed_runtime_inventory_digest(self.stage)
        with self.assertRaises(FileNotFoundError):
            self.run_chain()
        observed = self.read_frames()
        self.assertIn('DRAFT_MISSING', observed.public()['errors'])
        self.assertIsNone(observed.receipt)

    def test_preexisting_receipt_is_never_reused(self):
        self.receipt.parent.mkdir(mode=0o700)
        self.receipt.write_bytes(b'previous evidence')
        with self.assertRaisesRegex(ValueError, 'RECEIPT_EXISTS'):
            self.run_chain()
        self.assertEqual(self.receipt.read_bytes(), b'previous evidence')
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
