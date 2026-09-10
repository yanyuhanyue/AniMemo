"""One hidden input in a visible native Windows console, without input fallback.

The trusted caller must first validate the Guest and consume its task-wide
capture allowance. This channel owns no Guest authority and no persistent
allowance. Console recording must be excluded by the operator's launch path;
Windows console APIs cannot establish that no external recorder exists.
"""
from __future__ import annotations

import ctypes
import os
import threading


DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
WCHAR = ctypes.c_uint16
LPDWORD = ctypes.POINTER(DWORD)
LPWCHAR = ctypes.POINTER(WCHAR)
LPBYTE = ctypes.POINTER(ctypes.c_ubyte)
STD_INPUT_HANDLE = DWORD(-10).value
STD_OUTPUT_HANDLE = DWORD(-11).value
INVALID_HANDLE_VALUE = HANDLE(-1).value
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
FILE_TYPE_CHAR = 0x0002
CP_UTF8 = 65001
WC_ERR_INVALID_CHARS = 0x0080
MAX_SECRET_BYTES = 4096
_PROMPT = "Guest sudo password (hidden; Enter confirms, Esc cancels): "


class ConsoleCaptureError(RuntimeError):
    """Only a fixed public failure category may cross the channel boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _ConsoleAPI:
    def __init__(self, *, _dll_loader=None):
        try:
            if _dll_loader is None:
                if os.name != "nt":
                    raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE")
                _dll_loader = ctypes.WinDLL
            self.kernel32 = _dll_loader("kernel32", use_last_error=True)
            self.user32 = _dll_loader("user32", use_last_error=True)
            prototypes = (
                (self.kernel32.GetStdHandle, [DWORD], HANDLE),
                (self.kernel32.GetFileType, [HANDLE], DWORD),
                (self.kernel32.GetConsoleMode, [HANDLE, LPDWORD], BOOL),
                (self.kernel32.SetConsoleMode, [HANDLE, DWORD], BOOL),
                (self.kernel32.GetConsoleWindow, [], HANDLE),
                (self.kernel32.GetConsoleProcessList, [LPDWORD, DWORD], DWORD),
                (self.kernel32.GetCurrentProcessId, [], DWORD),
                (self.user32.IsWindowVisible, [HANDLE], BOOL),
                (self.kernel32.FlushConsoleInputBuffer, [HANDLE], BOOL),
                (self.kernel32.ReadConsoleW, [HANDLE, LPWCHAR, DWORD, LPDWORD, ctypes.c_void_p], BOOL),
                (self.kernel32.WriteConsoleW, [HANDLE, LPWCHAR, DWORD, LPDWORD, ctypes.c_void_p], BOOL),
                (self.kernel32.WideCharToMultiByte,
                 [DWORD, DWORD, LPWCHAR, ctypes.c_int32, LPBYTE, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p],
                 ctypes.c_int32),
            )
            for function, arguments, result in prototypes:
                function.argtypes = arguments
                function.restype = result
        except ConsoleCaptureError:
            raise
        except Exception:
            raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE") from None


class WindowsConsoleCapture:
    """Fail-closed console channel; one capture attempt per instance."""

    def __init__(self, *, _api=None):
        self._api = _api
        self._attempted = False
        self._lock = threading.Lock()

    def _snapshot(self):
        if self._api is None:
            self._api = _ConsoleAPI()
        kernel = self._api.kernel32
        stdin = kernel.GetStdHandle(STD_INPUT_HANDLE)
        stdout = kernel.GetStdHandle(STD_OUTPUT_HANDLE)
        window = kernel.GetConsoleWindow()
        # Pseudoconsole GetConsoleWindow handles are message-only and do not
        # represent a locally displayed native console window.
        if not window or not self._api.user32.IsWindowVisible(window):
            raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE")
        clients = (DWORD * 1)()
        current_pid = kernel.GetCurrentProcessId()
        # A dedicated native console must have this process as its only client.
        # A larger return value means the one-element buffer was too small;
        # no client identity may be inferred from its contents in that case.
        if not current_pid or kernel.GetConsoleProcessList(clients, 1) != 1 or clients[0] != current_pid:
            raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE")
        modes = []
        for handle in (stdin, stdout):
            mode = DWORD()
            if handle in (None, 0, INVALID_HANDLE_VALUE) or kernel.GetFileType(handle) != FILE_TYPE_CHAR or not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE")
            modes.append(mode.value)
        return stdin, stdout, window, modes[0], modes[1]

    def preflight(self) -> None:
        """Check channel availability without changing modes or reading input."""
        try:
            self._snapshot()
        except ConsoleCaptureError:
            raise
        except Exception:
            raise ConsoleCaptureError("CREDENTIAL_CHANNEL_UNAVAILABLE") from None

    def _write_public(self, stdout, text: str) -> None:
        encoded = text.encode("utf-16-le")
        length = len(encoded) // 2
        value = (WCHAR * length).from_buffer_copy(encoded)
        written = DWORD()
        if not self._api.kernel32.WriteConsoleW(stdout, value, length, ctypes.byref(written), None) or written.value != length:
            raise ConsoleCaptureError("CREDENTIAL_CONSOLE_OUTPUT_FAILED")

    def _encode(self, value, length: int) -> bytearray:
        kernel = self._api.kernel32
        size = kernel.WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, length, None, 0, None, None)
        if size <= 0:
            raise ConsoleCaptureError("CREDENTIAL_INPUT_INVALID")
        if size > MAX_SECRET_BYTES:
            raise ConsoleCaptureError("CREDENTIAL_INPUT_TOO_LONG")
        result = bytearray(size)
        view = (ctypes.c_ubyte * size).from_buffer(result)
        try:
            if kernel.WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, length, view, size, None, None) != size:
                raise ConsoleCaptureError("CREDENTIAL_INPUT_INVALID")
            return result
        except BaseException:
            ctypes.memset(view, 0, size)
            raise
        finally:
            del view

    def capture(self) -> bytearray:
        """Read once into mutable storage; no argv/env/file/stdin-stream input."""
        with self._lock:
            if self._attempted:
                raise ConsoleCaptureError("CREDENTIAL_CAPTURE_ALREADY_ATTEMPTED")
            self._attempted = True
        secret = bytearray()
        value = (WCHAR * MAX_SECRET_BYTES)()
        character = (WCHAR * 1)()
        restore = None
        error = None
        try:
            stdin, stdout, window, original_mode, output_mode = self._snapshot()
            kernel = self._api.kernel32
            hidden_mode = original_mode & ~(ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT)
            # Retain restoration ownership even when SetConsoleMode fails.
            restore = (stdin, original_mode)
            if not kernel.SetConsoleMode(stdin, hidden_mode):
                raise ConsoleCaptureError("CREDENTIAL_CONSOLE_MODE_FAILED")
            if not kernel.FlushConsoleInputBuffer(stdin):
                raise ConsoleCaptureError("CREDENTIAL_CONSOLE_INPUT_FAILED")
            self._write_public(stdout, _PROMPT)
            length = 0
            while True:
                if self._snapshot() != (stdin, stdout, window, hidden_mode, output_mode):
                    raise ConsoleCaptureError("CREDENTIAL_CONSOLE_CHANGED")
                count = DWORD()
                if not kernel.ReadConsoleW(stdin, character, 1, ctypes.byref(count), None) or count.value != 1:
                    raise ConsoleCaptureError("CREDENTIAL_CONSOLE_INPUT_FAILED")
                if self._snapshot() != (stdin, stdout, window, hidden_mode, output_mode):
                    raise ConsoleCaptureError("CREDENTIAL_CONSOLE_CHANGED")
                codepoint = character[0]
                character[0] = 0
                if codepoint in (3, 27):
                    raise ConsoleCaptureError("CREDENTIAL_CAPTURE_CANCELLED")
                if codepoint in (10, 13):
                    if length == 0:
                        raise ConsoleCaptureError("CREDENTIAL_INPUT_INVALID")
                    secret = self._encode(value, length)
                    self._write_public(stdout, "\r\n")
                    break
                if codepoint == 8:
                    if length:
                        length -= 1
                        low_surrogate = 0xDC00 <= value[length] <= 0xDFFF
                        value[length] = 0
                        if low_surrogate and length and 0xD800 <= value[length - 1] <= 0xDBFF:
                            length -= 1
                            value[length] = 0
                    continue
                if codepoint < 32 or codepoint == 127:
                    raise ConsoleCaptureError("CREDENTIAL_INPUT_INVALID")
                if length == MAX_SECRET_BYTES:
                    raise ConsoleCaptureError("CREDENTIAL_INPUT_TOO_LONG")
                value[length] = codepoint
                length += 1
        except ConsoleCaptureError as failure:
            error = failure.code
        except (KeyboardInterrupt, SystemExit):
            error = "CREDENTIAL_CAPTURE_CANCELLED"
        except BaseException:
            error = "CREDENTIAL_CHANNEL_UNAVAILABLE"
        finally:
            ctypes.memset(value, 0, ctypes.sizeof(value))
            ctypes.memset(character, 0, ctypes.sizeof(character))
            if restore is not None:
                stdin, original_mode = restore
                # Discard residual typeahead before echo can be restored.
                input_flushed = False
                try:
                    input_flushed = bool(self._api.kernel32.FlushConsoleInputBuffer(stdin))
                except BaseException:
                    pass
                if not input_flushed:
                    # The native console must be closed by the trusted caller.
                    # Returning it to an echoing shell could expose residual input.
                    error = "CREDENTIAL_CONSOLE_RESTORE_FAILED"
                final_mode = original_mode if input_flushed else hidden_mode
                try:
                    restored = DWORD()
                    if (not self._api.kernel32.SetConsoleMode(stdin, final_mode)
                            or not self._api.kernel32.GetConsoleMode(stdin, ctypes.byref(restored))
                            or restored.value != final_mode):
                        error = "CREDENTIAL_CONSOLE_RESTORE_FAILED"
                except BaseException:
                    error = "CREDENTIAL_CONSOLE_RESTORE_FAILED"
            if error is not None:
                for index in range(len(secret)):
                    secret[index] = 0
                secret.clear()
        if error is not None:
            raise ConsoleCaptureError(error) from None
        return secret
