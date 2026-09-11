"""Credential-free process diagnostic and host lifecycle regressions."""
import subprocess
import locale
import ctypes
from ctypes import wintypes
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import candidate_vm_harness as h
from scripts.tests.test_candidate_vm_harness import FakeWindowsPlatform
from release import formal_windows_pretrust as win


class VmHostDiagnosticsTests(unittest.TestCase):
    def provider(self, *, completed=None, error=None):
        runner = mock.Mock()
        runner.run.side_effect = error
        runner.run.return_value = completed
        return h.ClosedVmwareProvider(runner=runner, windows_platform=FakeWindowsPlatform(), environment={})

    def test_revert_nonzero_is_bounded_and_preserves_real_returncode(self):
        provider = self.provider(completed=subprocess.CompletedProcess([], 57,
            b"Error: The file is already in use\n" + b"secret-sentinel" * 2000, b""))
        with self.assertRaises(h.VmHostCommandError) as caught:
            provider._revert_clone(Path("E:/test-owned/clone.vmx"), h.SNAPSHOT_ALLOWLIST["FRESH_BASE"])
        value = caught.exception.public_diagnostic()
        self.assertEqual(caught.exception.code, "CANDIDATE_VM_CLONE_REVERT_FAILED")
        self.assertEqual(value["returncode"], 57)
        self.assertEqual(value["operation"], "revertToSnapshot")
        self.assertTrue(value["stdout"]["truncated"])
        self.assertEqual(value["stdout"]["excerpt"], "Error: The file is already in use")
        self.assertNotIn("secret-sentinel", str(value))
        self.assertTrue(value["stderr"]["empty"])
        self.assertLessEqual(value["started_utc"], value["ended_utc"])

    def test_timeout_cancel_and_launch_error_do_not_invent_exit_codes(self):
        for error, kind in ((subprocess.TimeoutExpired("untrusted", 1), "TIMEOUT"),
                            (KeyboardInterrupt(), "CANCELLED"), (OSError("untrusted"), "PROCESS_START_OR_WAIT_FAILED")):
            with self.subTest(kind=kind):
                provider = self.provider(error=error)
                with self.assertRaises(h.VmHostCommandError) as caught:
                    provider._revert_clone(Path("E:/test-owned/clone.vmx"), h.SNAPSHOT_ALLOWLIST["FRESH_BASE"])
                value = caught.exception.public_diagnostic()
                self.assertEqual(value["kind"], kind)
                self.assertIsNone(value["returncode"])
                self.assertIsNone(value["stdout"]["empty"])
                self.assertNotIn("untrusted", str(value))

    def test_success_is_observed_and_unknown_stream_content_is_omitted(self):
        provider = self.provider(completed=subprocess.CompletedProcess([], 0, b"arbitrary-output", b""))
        provider._revert_clone(Path("E:/test-owned/clone.vmx"), h.SNAPSHOT_ALLOWLIST["FRESH_BASE"])
        value = provider._host_lifecycle_observations[0]
        self.assertEqual(value["returncode"], 0)
        self.assertEqual(value["kind"], "SUCCESS")
        self.assertEqual(value["stdout"]["limitation"], "UNRECOGNIZED_CONTENT_OMITTED")
        self.assertNotIn("arbitrary-output", str(value))

    def test_locale_output_redacts_exact_unicode_clone_path(self):
        path = 'E:/private/中文 VM/clone.vmx'
        output = ('Error: Cannot open VM: ' + path + ', The virtual machine cannot be found\r\n').encode('gbk')
        with mock.patch.object(locale, 'getpreferredencoding', return_value='gbk'):
            value = h._vmware_error_excerpt(output, clone_vmx=path)
        self.assertEqual(value['excerpt'], 'Error: Cannot open VM: <CLONE_VMX>, The virtual machine cannot be found')
        self.assertNotIn(path, str(value))

    def test_overlong_vmware_path_is_rejected_before_process_launch(self):
        provider = self.provider()
        with self.assertRaisesRegex(h.CandidateHarnessError, 'CANDIDATE_VM_PRIVATE_PATH_TOO_LONG'):
            provider._revert_clone(Path('E:/' + 'x' * 240 + '/clone.vmx'), h.SNAPSHOT_ALLOWLIST['FRESH_BASE'])
        provider._runner.run.assert_not_called()
        # Windows counts UTF-16 code units, including surrogate pairs.
        with self.assertRaisesRegex(h.CandidateHarnessError, 'CANDIDATE_VM_PRIVATE_PATH_TOO_LONG'):
            h.ClosedVmwareProvider._assert_vmware_path_budget(Path('E:/' + '\U0001f600' * 120))


@unittest.skipUnless(os.name == 'nt', 'Actual Windows cwd and anti-replacement sharing')
class WindowsWorkingDirectoryHoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='animemo-cwd-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = win.create_windows_private_directory(Path(self.temporary.name), prefix='animemo-cwd-hold-test')
        self.directory = self.root / 'vm'
        self.directory.mkdir()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def tearDown(self):
        self.assertEqual(self.root.resolve().parent, Path(self.temporary.name).resolve())
        self.assertTrue(self.root.name.startswith('animemo-cwd-hold-test-'))
        shutil.rmtree(self.root)

    def test_working_directory_and_child_writes_succeed_while_rename_delete_fail(self):
        original = Path.cwd()
        with win.hold_windows_private_working_directory(self.directory):
            try:
                os.chdir(self.directory)
                self.assertEqual(Path.cwd(), self.directory)
                Path('own-test.txt').write_bytes(b'nonsecret fixture')
            finally:
                os.chdir(original)
            with self.assertRaises(OSError) as renamed:
                self.directory.rename(self.root / 'replacement')
            self.assertEqual(renamed.exception.winerror, 32)
            (self.directory / 'own-test.txt').unlink()
            with self.assertRaises(OSError) as deleted:
                self.directory.rmdir()
            self.assertEqual(deleted.exception.winerror, 32)
            self.assertTrue(self.directory.is_dir())

    def test_preexisting_delete_handle_is_rejected_before_yield(self):
        handle = self.kernel.CreateFileW(str(self.directory), 0x10080, 7, None, 3, 0x02200000, None)
        self.assertNotIn(handle, (None, 0, ctypes.c_void_p(-1).value))
        try:
            with self.assertRaises(win.FormalWindowsPretrustError):
                with win.hold_windows_private_working_directory(self.directory):
                    self.fail('Existing DELETE access must prevent obtaining the authority')
        finally:
            self.assertTrue(self.kernel.CloseHandle(handle))

    def test_junction_cannot_substitute_the_working_directory(self):
        target = self.root / 'junction-target'
        target.mkdir()
        junction = self.root / 'junction'
        completed = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(target)],
            capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0)
        with self.assertRaises(win.FormalWindowsPretrustError):
            with win.hold_windows_private_working_directory(junction):
                self.fail('Reparse path must fail before execution')
        junction.rmdir()
