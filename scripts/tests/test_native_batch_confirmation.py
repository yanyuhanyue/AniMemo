"""Synthetic Win32 dialogs only: no Console capture, Provider, entry, or VM."""
import ctypes as C
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import native_batch_confirmation as dialog


class LayoutTests(unittest.TestCase):
    def test_small_and_high_dpi_buttons_and_details_stay_inside_client(self):
        for width, height, dpi in ((760, 600, 96), (640, 440, 144), (768, 520, 192),
                                   (960, 660, 240), (1280, 840, 288)):
            with self.subTest(width=width, height=height, dpi=dpi):
                rectangles = dialog.layout(width, height, dpi)
                for x, y, w, h in rectangles.values():
                    self.assertGreater(w, 0)
                    self.assertGreater(h, 0)
                    self.assertLessEqual(x + w, width)
                    self.assertLessEqual(y + h, height)
                self.assertLess(rectangles[dialog.DETAILS][1] + rectangles[dialog.DETAILS][3],
                                rectangles[dialog.APPROVE][1])
        with self.assertRaisesRegex(RuntimeError, 'DISPLAY_UNAVAILABLE'):
            dialog.layout(200, 180, 192)

    def test_stage_titles_and_word_wrap_keep_full_scope(self):
        self.assertIn('A:', dialog.title_for('Confirm A preparation ONLY; synthetic'))
        self.assertIn('B:', dialog.title_for('B ONLY: synthetic'))
        self.assertEqual(dialog.EDIT_STYLE & 0x80, 0)  # no ES_AUTOHSCROLL
        self.assertEqual(dialog.EDIT_STYLE & 0x100000, 0)  # no WS_HSCROLL
        self.assertTrue(dialog.EDIT_STYLE & 0x200000)  # WS_VSCROLL
        self.assertTrue(dialog.EDIT_STYLE & 0x800)  # ES_READONLY


@unittest.skipUnless(os.name == 'nt', 'Win32 synthetic UI verification')
class NativeDialogTests(unittest.TestCase):
    def _probe(self, action, *, long_text=True, timeout=5):
        # The only text and window in this probe are newly created synthetics.
        summary = ('SYNTHETIC UI ONLY — grants no authority.\n'
                   + ('long-path-' * 18 + '\n') * (60 if long_text else 1)
                   + 'END OF SYNTHETIC DETAILS')
        native = dialog._Dialog(None, summary, timeout, None)
        measurements = {}
        real_message = native._message

        def message(window, message, wparam, lparam):
            answer = real_message(window, message, wparam, lparam)
            if message == 0x110 and not native.failed:
                try:
                    area = native._available(window)
                    actual = dialog.RECT()
                    self.assertTrue(native.user.GetWindowRect(window, C.byref(actual)))
                    self.assertGreaterEqual(actual.left, area.left)
                    self.assertGreaterEqual(actual.top, area.top)
                    self.assertLessEqual(actual.right, area.right)
                    self.assertLessEqual(actual.bottom, area.bottom)
                    client = dialog.RECT()
                    self.assertTrue(native.user.GetClientRect(window, C.byref(client)))
                    positions = dialog.layout(client.right, client.bottom, native.user.GetDpiForWindow(window))
                    measurements['client'] = (client.right, client.bottom)
                    measurements['buttons'] = [positions[dialog.APPROVE], positions[dialog.CANCEL]]
                    native.user.GetWindowTextLengthW.argtypes = [dialog.HWND]
                    native.user.GetWindowTextLengthW.restype = C.c_int
                    expected = summary.replace('\n', '\r\n')
                    self.assertEqual(native.user.GetWindowTextLengthW(native.children[dialog.DETAILS]), len(expected))
                    native.user.GetFocus.argtypes, native.user.GetFocus.restype = [], dialog.HWND
                    self.assertEqual(native.user.GetFocus(), native.children[dialog.CANCEL])
                    native.user.IsWindowEnabled.argtypes, native.user.IsWindowEnabled.restype = [dialog.HWND], C.c_int
                    self.assertFalse(native.user.IsWindowEnabled(native.children[dialog.APPROVE]))
                    action(native, window)
                except BaseException as error:
                    measurements['error'] = error
                    native.user.EndDialog(window, dialog.NO)
            return answer

        native.procedure = native.proc_type(message)
        answer = native.show()
        if 'error' in measurements:
            raise measurements['error']
        self.assertIn('buttons', measurements)
        return answer

    def test_native_full_text_scrolling_then_deliberate_review_and_approve(self):
        def action(native, window):
            self.assertFalse(native._review_state())
            native.user.SendMessageW(native.children[dialog.DETAILS], 0x115, 7, 0)  # SB_BOTTOM
            self.assertFalse(native._review_state())  # scrolling alone is not approval
            native.user.SendMessageW(native.children[dialog.REVIEW], 0xF1, 1, 0)
            self.assertTrue(native._review_state())
            native.user.SendMessageW(window, 0x111, dialog.APPROVE, native.children[dialog.APPROVE])
        self.assertEqual(self._probe(action), dialog.YES)

    def test_native_enter_and_escape_cancel_even_with_approve_focus(self):
        for key in (13, 27):
            with self.subTest(key=key):
                def action(native, window):
                    native.user.SendMessageW(native.children[dialog.DETAILS], 0x115, 7, 0)
                    native._review_state()
                    native.user.SendMessageW(native.children[dialog.REVIEW], 0xF1, 1, 0)
                    native._review_state()
                    native.user.SetFocus(native.children[dialog.APPROVE])
                    native.user.SendMessageW(native.children[dialog.APPROVE], 0x100, key, 0)
                self.assertEqual(self._probe(action), dialog.NO)

    def test_native_timeout_cancels_without_any_user_input(self):
        self.assertEqual(self._probe(lambda native, window: None, timeout=0.25), dialog.NO)

    def test_native_attempt_before_review_cannot_approve(self):
        def action(native, window):
            native.user.SendMessageW(window, 0x111, dialog.APPROVE, native.children[dialog.APPROVE])
            self.assertFalse(native.failed)
            native.user.EndDialog(window, dialog.NO)
        self.assertEqual(self._probe(action), dialog.NO)


class ResultOnlyTests(unittest.TestCase):
    def test_verified_report_export_never_writes_console_and_preserves_exit_code(self):
        from scripts import local_candidate_development as entry
        required = ['--verified-candidate-digest', 'sha256:' + 'a' * 64,
                    '--qualification-run-id', '1', '--material-source-sha', 'b' * 40,
                    '--material-source-tree', 'c' * 40, '--execution-source-sha', 'd' * 40,
                    '--execution-source-tree', 'e' * 40]
        class BlockedConsole:
            def write(self, text):
                raise AssertionError('file-only mode touched Console output')
        with tempfile.TemporaryDirectory(prefix='animemo-synthetic-ui-') as folder:
            for status, expected in (('PASS', 0), ('ERROR', 2)):
                target = Path(folder) / (status + '.json')
                report = {'status': status, 'synthetic': True}
                with mock.patch.object(entry, 'run', return_value=report) as run, redirect_stdout(BlockedConsole()):
                    self.assertEqual(entry.main(required + ['--result', str(target), '--result-only']), expected)
                self.assertEqual(json.loads(target.read_bytes()), report)
                run.assert_called_once()

    def test_result_only_without_result_fails_before_run(self):
        from scripts import local_candidate_development as entry
        from contextlib import redirect_stderr
        required = ['--verified-candidate-digest', 'sha256:' + 'a' * 64,
                    '--qualification-run-id', '1', '--material-source-sha', 'b' * 40,
                    '--material-source-tree', 'c' * 40, '--execution-source-sha', 'd' * 40,
                    '--execution-source-tree', 'e' * 40]
        error = io.StringIO()
        with mock.patch.object(entry, 'run') as run, redirect_stderr(error):
            with self.assertRaises(SystemExit):
                entry.main(required + ['--result-only'])
            run.assert_not_called()
        self.assertIn('--result-only requires --result', error.getvalue())
