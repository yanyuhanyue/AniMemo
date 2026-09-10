from __future__ import annotations

import ctypes
import types
import unittest
from unittest import mock

from scripts import guest_console_capture as c


class Function:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *arguments):
        return self.implementation(*arguments)


class ConsoleFixture:
    """All input in this fixture is synthetic; no native input is read."""

    def __init__(self, text="synthetic-sentinel\r"):
        encoded = text.encode("utf-16-le", errors="surrogatepass")
        self.input = [int.from_bytes(encoded[index:index + 2], "little") for index in range(0, len(encoded), 2)]
        self.original_mode = 0x01F7
        self.mode = self.original_mode
        self.output_mode = 3
        self.handles = {c.STD_INPUT_HANDLE: 0x123456781234, c.STD_OUTPUT_HANDLE: 0x123456785678}
        self.console_handles = set(self.handles.values())
        self.file_type = c.FILE_TYPE_CHAR
        self.window = 0x234567891234
        self.visible = True
        self.current_pid = 0x81234567
        self.clients = [self.current_pid]
        self.set_modes = []
        self.read_modes = []
        self.outputs = []
        self.flushes = 0
        self.read_failure = None
        self.fail_restore = False
        self.fail_encoding = False
        self.kernel32 = types.SimpleNamespace(
            GetStdHandle=Function(lambda kind: self.handles[kind]),
            GetFileType=Function(lambda handle: self.file_type),
            GetConsoleMode=Function(self.get_mode),
            SetConsoleMode=Function(self.set_mode),
            GetConsoleWindow=Function(lambda: self.window),
            GetConsoleProcessList=Function(self.process_list),
            GetCurrentProcessId=Function(lambda: self.current_pid),
            FlushConsoleInputBuffer=Function(self.flush),
            ReadConsoleW=Function(self.read),
            WriteConsoleW=Function(self.write),
            WideCharToMultiByte=Function(self.encode),
        )
        self.user32 = types.SimpleNamespace(IsWindowVisible=Function(lambda window: self.visible))

    def process_list(self, output, capacity):
        if len(self.clients) <= capacity:
            for index, pid in enumerate(self.clients):
                output[index] = pid
        return len(self.clients)

    def get_mode(self, handle, output):
        if handle not in self.console_handles:
            return 0
        output._obj.value = self.mode if handle == self.handles[c.STD_INPUT_HANDLE] else self.output_mode
        return 1

    def set_mode(self, handle, mode):
        self.set_modes.append(mode)
        if self.fail_restore and mode == self.original_mode:
            return 0
        self.mode = mode
        return 1

    def flush(self, handle):
        self.flushes += 1
        # Test input represents keystrokes typed after the public prompt, not
        # typeahead present when the first flush occurs.
        return 1

    def read(self, handle, value, size, count, reserved):
        self.read_modes.append(self.mode)
        if self.read_failure is not None and len(self.read_modes) > 2:
            raise self.read_failure
        if not self.input:
            return 0
        value[0] = self.input.pop(0)
        count._obj.value = 1
        return 1

    def write(self, handle, value, size, count, reserved):
        self.outputs.append(ctypes.string_at(value, size * 2).decode("utf-16-le"))
        count._obj.value = size
        return 1

    def encode(self, codepage, flags, value, length, output, size, default, used_default):
        assert codepage == c.CP_UTF8 and flags == c.WC_ERR_INVALID_CHARS
        assert default is None and used_default is None
        try:
            encoded = ctypes.string_at(value, length * 2).decode("utf-16-le").encode("utf-8")
        except UnicodeError:
            return 0
        if output is not None:
            for index, byte in enumerate(encoded):
                output[index] = byte
            if self.fail_encoding:
                return 0
        return len(encoded)


class ConsoleCaptureTests(unittest.TestCase):
    def assert_mutable_buffers_wiped(self, channel, expected_code):
        buffers = []
        real_memset = ctypes.memset

        def wipe(target, value, size):
            result = real_memset(target, value, size)
            buffers.append((target, size))
            return result

        with mock.patch.object(c.ctypes, "memset", side_effect=wipe):
            with self.assertRaises(c.ConsoleCaptureError) as caught:
                channel.capture()
        self.assertEqual(str(caught.exception), expected_code)
        self.assertGreaterEqual(len(buffers), 2)
        for buffer, size in buffers:
            self.assertEqual(ctypes.string_at(buffer, size), bytes(size))
        with self.assertRaisesRegex(c.ConsoleCaptureError, "CREDENTIAL_CAPTURE_ALREADY_ATTEMPTED"):
            channel.capture()

    def test_preflight_has_no_read_prompt_or_mode_mutation(self):
        api = ConsoleFixture()
        c.WindowsConsoleCapture(_api=api).preflight()
        self.assertEqual((api.set_modes, api.read_modes, api.outputs, api.flushes), ([], [], [], 0))

    def test_no_native_console_or_redirected_handles_fail_before_input(self):
        for failure in ("missing_window", "pseudoconsole", "stdin", "stdout", "pipe", "invalid_handle"):
            with self.subTest(failure=failure):
                api = ConsoleFixture()
                if failure == "missing_window":
                    api.window = None
                elif failure == "pseudoconsole":
                    api.visible = False
                elif failure in {"stdin", "stdout"}:
                    kind = c.STD_INPUT_HANDLE if failure == "stdin" else c.STD_OUTPUT_HANDLE
                    api.console_handles.remove(api.handles[kind])
                elif failure == "pipe":
                    api.file_type = 3
                else:
                    api.handles[c.STD_INPUT_HANDLE] = c.INVALID_HANDLE_VALUE
                channel = c.WindowsConsoleCapture(_api=api)
                with self.assertRaisesRegex(c.ConsoleCaptureError, "CREDENTIAL_CHANNEL_UNAVAILABLE"):
                    channel.preflight()
                self.assert_mutable_buffers_wiped(channel, "CREDENTIAL_CHANNEL_UNAVAILABLE")
                self.assertEqual((api.set_modes, api.read_modes, api.outputs), ([], [], []))

    def test_hidden_capture_returns_mutable_utf8_and_restores_mode(self):
        api = ConsoleFixture("synthetic-密钥-😀\r")
        channel = c.WindowsConsoleCapture(_api=api)
        secret = channel.capture()
        self.assertIs(type(secret), bytearray)
        self.assertEqual(secret, "synthetic-密钥-😀".encode())
        hidden = api.original_mode & ~(c.ENABLE_ECHO_INPUT | c.ENABLE_LINE_INPUT | c.ENABLE_PROCESSED_INPUT)
        self.assertEqual(api.set_modes, [hidden, api.original_mode])
        self.assertTrue(api.read_modes)
        self.assertTrue(all(mode == hidden for mode in api.read_modes))
        self.assertEqual(api.outputs, [c._PROMPT, "\r\n"])
        self.assertEqual(api.flushes, 2)
        self.assertEqual(api.mode, api.original_mode)
        with self.assertRaisesRegex(c.ConsoleCaptureError, "CREDENTIAL_CAPTURE_ALREADY_ATTEMPTED"):
            channel.capture()

    def test_shared_or_unproven_console_client_fails_before_input(self):
        for failure in ("shared", "failed_query", "different_client", "missing_pid"):
            with self.subTest(failure=failure):
                api = ConsoleFixture()
                if failure == "shared":
                    api.clients.append(1234)
                elif failure == "failed_query":
                    api.clients = []
                elif failure == "different_client":
                    api.clients = [1234]
                else:
                    api.current_pid = 0
                channel = c.WindowsConsoleCapture(_api=api)
                with self.assertRaisesRegex(c.ConsoleCaptureError, "CREDENTIAL_CHANNEL_UNAVAILABLE"):
                    channel.preflight()
                self.assert_mutable_buffers_wiped(channel, "CREDENTIAL_CHANNEL_UNAVAILABLE")
                self.assertEqual((api.set_modes, api.read_modes, api.outputs, api.flushes), ([], [], [], 0))

    def test_backspace_clears_complete_surrogate_pair(self):
        api = ConsoleFixture("synthetic😀\b!\r")
        self.assertEqual(c.WindowsConsoleCapture(_api=api).capture(), b"synthetic!")

    def test_exact_byte_limit_is_accepted(self):
        api = ConsoleFixture("x" * c.MAX_SECRET_BYTES + "\r")
        self.assertEqual(c.WindowsConsoleCapture(_api=api).capture(), bytearray(b"x" * c.MAX_SECRET_BYTES))

    def test_non_windows_has_no_fallback(self):
        with mock.patch.object(c.os, "name", "posix"):
            with self.assertRaisesRegex(c.ConsoleCaptureError, "CREDENTIAL_CHANNEL_UNAVAILABLE"):
                c.WindowsConsoleCapture().preflight()

    def test_native_setup_and_io_failures_restore_without_returning_secret(self):
        for failure, code in (
            ("mode", "CREDENTIAL_CONSOLE_MODE_FAILED"),
            ("initial_flush", "CREDENTIAL_CONSOLE_INPUT_FAILED"),
            ("prompt", "CREDENTIAL_CONSOLE_OUTPUT_FAILED"),
            ("zero_read", "CREDENTIAL_CONSOLE_INPUT_FAILED"),
            ("final_flush", "CREDENTIAL_CONSOLE_RESTORE_FAILED"),
        ):
            with self.subTest(failure=failure):
                api = ConsoleFixture()
                if failure == "mode":
                    set_mode = api.set_mode
                    api.kernel32.SetConsoleMode.implementation = lambda handle, mode: (
                        set_mode(handle, mode) if mode == api.original_mode else 0)
                elif failure in {"initial_flush", "final_flush"}:
                    def flush(handle):
                        api.flushes += 1
                        return int(api.flushes != (1 if failure == "initial_flush" else 2))
                    api.kernel32.FlushConsoleInputBuffer.implementation = flush
                elif failure == "prompt":
                    api.kernel32.WriteConsoleW.implementation = lambda *args: 0
                else:
                    def zero_read(handle, value, size, count, reserved):
                        count._obj.value = 0
                        return 1
                    api.kernel32.ReadConsoleW.implementation = zero_read
                self.assert_mutable_buffers_wiped(c.WindowsConsoleCapture(_api=api), code)
                expected_mode = api.original_mode
                if failure == "final_flush":
                    expected_mode &= ~(c.ENABLE_ECHO_INPUT | c.ENABLE_LINE_INPUT | c.ENABLE_PROCESSED_INPUT)
                    self.assertTrue(all(mode & c.ENABLE_ECHO_INPUT == 0 for mode in api.set_modes))
                self.assertEqual(api.mode, expected_mode)

    def test_cancel_and_read_failure_wipe_buffers_and_restore(self):
        for failure in ("escape", "control_c", "interrupt", "read_error"):
            with self.subTest(failure=failure):
                api = ConsoleFixture("synthetic" + ("\x1b" if failure == "escape" else "\x03"))
                if failure == "interrupt":
                    api.read_failure = KeyboardInterrupt()
                elif failure == "read_error":
                    api.read_failure = OSError("must never disclose synthetic-sentinel")
                code = "CREDENTIAL_CHANNEL_UNAVAILABLE" if failure == "read_error" else "CREDENTIAL_CAPTURE_CANCELLED"
                self.assert_mutable_buffers_wiped(c.WindowsConsoleCapture(_api=api), code)
                self.assertEqual(api.mode, api.original_mode)
                self.assertEqual(api.flushes, 2)

    def test_overlong_empty_control_or_invalid_utf16_input_fails_closed(self):
        for text, code in (
            ("x" * (c.MAX_SECRET_BYTES + 1), "CREDENTIAL_INPUT_TOO_LONG"),
            ("密" * 1366 + "\r", "CREDENTIAL_INPUT_TOO_LONG"),
            ("\r", "CREDENTIAL_INPUT_INVALID"),
            ("synthetic\t", "CREDENTIAL_INPUT_INVALID"),
            ("synthetic\ud800\r", "CREDENTIAL_INPUT_INVALID"),
        ):
            with self.subTest(code=code, length=len(text)):
                api = ConsoleFixture(text)
                self.assert_mutable_buffers_wiped(c.WindowsConsoleCapture(_api=api), code)
                self.assertEqual(api.mode, api.original_mode)

    def test_partial_utf8_conversion_failure_wipes_output_and_utf16(self):
        api = ConsoleFixture()
        api.fail_encoding = True
        self.assert_mutable_buffers_wiped(c.WindowsConsoleCapture(_api=api), "CREDENTIAL_INPUT_INVALID")
        self.assertEqual(api.mode, api.original_mode)

    def test_restore_failure_discards_completed_secret(self):
        api = ConsoleFixture()
        api.fail_restore = True
        channel = c.WindowsConsoleCapture(_api=api)
        captured = []
        encode = channel._encode

        def remember(*args):
            result = encode(*args)
            captured.append(result)
            return result

        with mock.patch.object(channel, "_encode", side_effect=remember):
            self.assert_mutable_buffers_wiped(channel, "CREDENTIAL_CONSOLE_RESTORE_FAILED")
        self.assertEqual(captured, [bytearray()])
        self.assertEqual(api.set_modes[-1], api.original_mode)

    def test_echo_mode_change_stops_before_another_character_read(self):
        api = ConsoleFixture()
        original_read = api.kernel32.ReadConsoleW.implementation

        def change_mode(*args):
            result = original_read(*args)
            api.mode |= c.ENABLE_ECHO_INPUT
            return result

        api.kernel32.ReadConsoleW.implementation = change_mode
        self.assert_mutable_buffers_wiped(c.WindowsConsoleCapture(_api=api), "CREDENTIAL_CONSOLE_CHANGED")
        self.assertEqual(len(api.read_modes), 1)
        self.assertEqual(api.mode, api.original_mode)

    def test_echo_mode_change_during_enter_read_rejects_before_encoding(self):
        api = ConsoleFixture()
        original_read = api.kernel32.ReadConsoleW.implementation

        def change_on_enter(handle, value, size, count, reserved):
            result = original_read(handle, value, size, count, reserved)
            if value[0] == 13:
                api.mode |= c.ENABLE_ECHO_INPUT
            return result

        api.kernel32.ReadConsoleW.implementation = change_on_enter
        channel = c.WindowsConsoleCapture(_api=api)
        with mock.patch.object(channel, "_encode", wraps=channel._encode) as encode:
            self.assert_mutable_buffers_wiped(channel, "CREDENTIAL_CONSOLE_CHANGED")
        encode.assert_not_called()
        self.assertEqual(api.outputs, [c._PROMPT])
        self.assertEqual(api.mode, api.original_mode)

    def test_uncertain_final_flush_keeps_hidden_mode_and_discards_secret(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                api = ConsoleFixture()

                def fail_final_flush(handle):
                    api.flushes += 1
                    if api.flushes == 2:
                        if raises:
                            raise OSError("synthetic unknown flush result")
                        return 0
                    return 1

                api.kernel32.FlushConsoleInputBuffer.implementation = fail_final_flush
                channel = c.WindowsConsoleCapture(_api=api)
                captured = []
                original_encode = channel._encode

                def remember(*args):
                    result = original_encode(*args)
                    captured.append(result)
                    return result

                with mock.patch.object(channel, "_encode", side_effect=remember):
                    self.assert_mutable_buffers_wiped(channel, "CREDENTIAL_CONSOLE_RESTORE_FAILED")
                self.assertEqual(captured, [bytearray()])
                self.assertTrue(all(mode & c.ENABLE_ECHO_INPUT == 0 for mode in api.set_modes))
                self.assertEqual(api.mode & c.ENABLE_ECHO_INPUT, 0)

    def test_win32_abi_uses_pointer_sized_handles_and_typed_output_arguments(self):
        fixture = ConsoleFixture()
        api = c._ConsoleAPI(_dll_loader=lambda name, **kwargs: getattr(fixture, name))
        self.assertEqual(api.kernel32.GetStdHandle.restype, ctypes.c_void_p)
        self.assertEqual(api.kernel32.GetConsoleWindow.restype, ctypes.c_void_p)
        self.assertEqual(api.kernel32.GetConsoleProcessList.argtypes, [c.LPDWORD, c.DWORD])
        self.assertEqual(api.kernel32.GetConsoleProcessList.restype, c.DWORD)
        self.assertEqual(api.kernel32.GetCurrentProcessId.argtypes, [])
        self.assertEqual(api.kernel32.GetCurrentProcessId.restype, c.DWORD)
        self.assertEqual(api.kernel32.GetConsoleMode.argtypes, [c.HANDLE, c.LPDWORD])
        self.assertEqual(api.kernel32.ReadConsoleW.argtypes, [c.HANDLE, c.LPWCHAR, c.DWORD, c.LPDWORD, ctypes.c_void_p])
        self.assertEqual(api.kernel32.WideCharToMultiByte.argtypes[2:6], [c.LPWCHAR, ctypes.c_int32, c.LPBYTE, ctypes.c_int32])
        c.WindowsConsoleCapture(_api=api).preflight()


if __name__ == "__main__":
    unittest.main()
