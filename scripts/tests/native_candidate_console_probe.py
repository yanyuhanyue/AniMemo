"""Explicit native Console test with generated public text, never a password.

Run directly in a dedicated visible conhost, outside test discovery. It does
not import the VM provider or credential ledger and performs no Guest action.
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import guest_console_capture as capture


class Character(ctypes.Union):
    _fields_ = [('UnicodeChar', ctypes.c_wchar), ('AsciiChar', ctypes.c_char)]


class KeyEvent(ctypes.Structure):
    _fields_ = [('bKeyDown', ctypes.c_int32), ('wRepeatCount', ctypes.c_uint16),
                ('wVirtualKeyCode', ctypes.c_uint16), ('wVirtualScanCode', ctypes.c_uint16),
                ('uChar', Character), ('dwControlKeyState', ctypes.c_uint32)]


class EventUnion(ctypes.Union):
    _fields_ = [('KeyEvent', KeyEvent), ('padding', ctypes.c_byte * 16)]


class InputRecord(ctypes.Structure):
    _fields_ = [('EventType', ctypes.c_uint16), ('Event', EventUnion)]


def run_case(text, cancel=False):
    channel = capture.WindowsConsoleCapture()
    channel.preflight()
    ready = threading.Event()
    failures = []
    original = channel._write_public
    def write_public(output, value):
        original(output, 'SYNTHETIC CONSOLE TEST (automatic public input): ' if value == capture._PROMPT else value)
        if value == capture._PROMPT:
            ready.set()
    channel._write_public = write_public
    kernel = channel._api.kernel32
    kernel.WriteConsoleInputW.argtypes = [capture.HANDLE, ctypes.POINTER(InputRecord), capture.DWORD, capture.LPDWORD]
    kernel.WriteConsoleInputW.restype = capture.BOOL
    def type_public_fixture():
        try:
            if not ready.wait(10):
                raise RuntimeError('PROMPT_NOT_OBSERVED')
            records = (InputRecord * len(text))()
            for record, character in zip(records, text):
                record.EventType = 1
                record.Event.KeyEvent.bKeyDown = 1
                record.Event.KeyEvent.wRepeatCount = 1
                record.Event.KeyEvent.uChar.UnicodeChar = character
            written = capture.DWORD()
            if not kernel.WriteConsoleInputW(kernel.GetStdHandle(capture.STD_INPUT_HANDLE),
                    records, len(records), ctypes.byref(written)) or written.value != len(records):
                raise RuntimeError('PUBLIC_FIXTURE_INPUT_FAILED')
        except BaseException:
            failures.append('PUBLIC_FIXTURE_INPUT_FAILED')
    worker = threading.Thread(target=type_public_fixture, daemon=True)
    worker.start()
    secret = None
    try:
        secret = channel.capture()
        if cancel or secret != bytearray(b'ANIMEMO_TEST'):
            raise RuntimeError('SYNTHETIC_CAPTURE_MISMATCH')
    except capture.ConsoleCaptureError as error:
        if not cancel or error.code != 'CREDENTIAL_CAPTURE_CANCELLED':
            raise
    finally:
        if secret is not None:
            secret[:] = b'\0' * len(secret)
            secret.clear()
    worker.join(5)
    if worker.is_alive() or failures:
        raise RuntimeError('PUBLIC_FIXTURE_INPUT_FAILED')
    channel.preflight()
    return {'result': 'PASS', 'preflight': True, 'native_input': True,
            'backspace': not cancel, 'cancel': cancel, 'buffer_cleared': True,
            'guest_delivery_count': 0, 'real_capture_ledger_access_count': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    with args.result.open('x', encoding='utf-8') as output:
        result = {'status': 'ERROR', 'cases': []}
        try:
            for label, text, cancel in (
                ('SINGLE_CAPTURE_CHANNEL', 'ANIMEMO_TEX\bST\r', False),
                ('CANCEL_CHANNEL', 'ANIMEMO_TEST\x1b', True),
            ):
                result['cases'].append({'case': label, **run_case(text, cancel)})
            result['status'] = 'PASS'
        except BaseException as error:
            result['failure_type'] = type(error).__name__
        finally:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write('\n')
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
