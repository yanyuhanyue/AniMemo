"""Bounded Win32 confirmation of public scope; no credential or batch authority."""
from __future__ import annotations

import ctypes as C
import os
import struct
import time

HWND = C.c_void_p
UINT = C.c_uint32
LONG = C.c_int32
WPARAM = C.c_size_t
LPARAM = C.c_ssize_t
YES, NO = 6, 7
APPROVE, CANCEL, DETAILS, REVIEW, HEADER = 100, 2, 101, 102, 103
DIALOG_STYLE = 0x80C80080  # popup, caption, system menu, modal frame
EDIT_STYLE = 0x50A10844  # child, visible, tabstop, border, vertical scroll, readonly, multiline


class RECT(C.Structure):
    _fields_ = [(name, LONG) for name in ('left', 'top', 'right', 'bottom')]


class MONITORINFO(C.Structure):
    _fields_ = [('size', UINT), ('monitor', RECT), ('work', RECT), ('flags', UINT)]


class SCROLLINFO(C.Structure):
    _fields_ = [('size', UINT), ('mask', UINT), ('minimum', LONG), ('maximum', LONG),
               ('page', UINT), ('position', LONG), ('track', LONG)]


class MSG(C.Structure):
    _fields_ = [('window', HWND), ('message', UINT), ('wparam', WPARAM),
               ('lparam', LPARAM), ('time', UINT), ('x', LONG), ('y', LONG)]


def layout(width, height, dpi):
    """Client pixel rectangles, with the action row independent of text length."""
    scale = lambda value: max(1, round(value * dpi / 96))
    margin, head, check, button = map(scale, (8, 36, 36, 32))
    content_width = width - 2 * margin
    text_height = height - head - check - button - 5 * margin
    if content_width < scale(260) or text_height < scale(40):
        raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
    button_width = (content_width - margin) // 2
    return {
        HEADER: (margin, margin, content_width, head),
        DETAILS: (margin, 2 * margin + head, content_width, text_height),
        REVIEW: (margin, 3 * margin + head + text_height, content_width, check),
        APPROVE: (margin, height - margin - button, button_width, button),
        CANCEL: (2 * margin + button_width, height - margin - button, button_width, button),
    }


def title_for(summary):
    if summary.startswith('Confirm A preparation ONLY;'):
        return 'AniMemo — A: prepare only / 仅准备'
    if summary.startswith('B ONLY:'):
        return 'AniMemo — B: exact target / 实际目标'
    return 'AniMemo — isolated batch / 隔离批次'


class _Dialog:
    def __init__(self, owner, summary, timeout_seconds, cancelled):
        self.owner, self.summary, self.cancelled = owner, summary, cancelled
        self.deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        self.failed = False
        self.seen_end = False
        self.children, self.old_procedures = {}, {}
        self.font = None
        self.user = C.WinDLL('user32', use_last_error=True)
        self.gdi = C.WinDLL('gdi32', use_last_error=True)
        self.proc_type = C.WINFUNCTYPE(LPARAM, HWND, UINT, WPARAM, LPARAM)
        prototypes = {
            'DialogBoxIndirectParamW': ([HWND, C.c_void_p, HWND, self.proc_type, LPARAM], LPARAM),
            'EndDialog': ([HWND, LPARAM], C.c_int),
            'CreateWindowExW': ([UINT, C.c_wchar_p, C.c_wchar_p, UINT, C.c_int, C.c_int,
                                 C.c_int, C.c_int, HWND, HWND, HWND, C.c_void_p], HWND),
            'SetWindowPos': ([HWND, HWND, C.c_int, C.c_int, C.c_int, C.c_int, UINT], C.c_int),
            'GetClientRect': ([HWND, C.POINTER(RECT)], C.c_int),
            'GetWindowRect': ([HWND, C.POINTER(RECT)], C.c_int),
            'MonitorFromWindow': ([HWND, UINT], HWND),
            'GetMonitorInfoW': ([HWND, C.POINTER(MONITORINFO)], C.c_int),
            'GetDpiForWindow': ([HWND], UINT),
            'SetThreadDpiAwarenessContext': ([HWND], HWND),
            'SendMessageW': ([HWND, UINT, WPARAM, LPARAM], LPARAM),
            'SetFocus': ([HWND], HWND),
            'EnableWindow': ([HWND, C.c_int], C.c_int),
            'SetTimer': ([HWND, WPARAM, UINT, C.c_void_p], WPARAM),
            'KillTimer': ([HWND, WPARAM], C.c_int),
            'GetScrollInfo': ([HWND, C.c_int, C.POINTER(SCROLLINFO)], C.c_int),
            'SetWindowLongPtrW': ([HWND, C.c_int, LPARAM], LPARAM),
            'CallWindowProcW': ([C.c_void_p, HWND, UINT, WPARAM, LPARAM], LPARAM),
        }
        for name, (arguments, result) in prototypes.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result
        self.gdi.CreateFontW.argtypes = [C.c_int] * 5 + [UINT] * 8 + [C.c_wchar_p]
        self.gdi.CreateFontW.restype = HWND
        self.gdi.DeleteObject.argtypes, self.gdi.DeleteObject.restype = [HWND], C.c_int
        self.procedure = self.proc_type(self._message)
        self.child_procedure = self.proc_type(self._child_message)

    def _available(self, window):
        info = MONITORINFO(size=C.sizeof(MONITORINFO))
        if not self.user.GetMonitorInfoW(self.user.MonitorFromWindow(window, 2), C.byref(info)):
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        return info.work

    def _position(self, window):
        work = self._available(window)
        dpi = self.user.GetDpiForWindow(window)
        if not dpi:
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        inset = max(8, round(8 * dpi / 96))
        width = min(round(760 * dpi / 96), work.right - work.left - 2 * inset)
        height = min(round(640 * dpi / 96), work.bottom - work.top - 2 * inset)
        if width <= 0 or height <= 0 or not self.user.SetWindowPos(
                window, None, work.left + (work.right - work.left - width) // 2,
                work.top + (work.bottom - work.top - height) // 2, width, height, 0x14):
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')

    def _layout(self, window):
        area = RECT()
        if not self.user.GetClientRect(window, C.byref(area)):
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        dpi = self.user.GetDpiForWindow(window)
        positions = layout(area.right, area.bottom, dpi)
        font = self.gdi.CreateFontW(-round(14 * dpi / 96), 0, 0, 0, 400,
                                   0, 0, 0, 1, 0, 0, 5, 0, 'Segoe UI')
        if not font:
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        for identifier, child in self.children.items():
            if not self.user.SetWindowPos(child, None, *positions[identifier], 0x14):
                self.gdi.DeleteObject(font)
                raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
            self.user.SendMessageW(child, 0x30, font, 1)  # WM_SETFONT
        old_font, self.font = self.font, font
        if old_font:
            self.gdi.DeleteObject(old_font)

    def _create(self, window):
        notice = '完整范围可滚动 / Scroll full scope.\r\nEnter / Esc = 取消 / Cancel.'
        for identifier, kind, text, style in (
                (HEADER, 'STATIC', notice, 0x50000000),
                (DETAILS, 'EDIT', self.summary.replace('\r\n', '\n').replace('\n', '\r\n'), EDIT_STYLE),
                (REVIEW, 'BUTTON', '已读完整范围 / I reviewed the complete scope', 0x50012003),
                (APPROVE, 'BUTTON', '批准 / Approve', 0x50010000),
                (CANCEL, 'BUTTON', '取消 / Cancel', 0x50010001)):
            child = self.user.CreateWindowExW(0, kind, text, style, 0, 0, 1, 1,
                                             window, identifier, None, None)
            if not child:
                raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
            self.children[identifier] = child
            old = self.user.SetWindowLongPtrW(child, -4, C.cast(self.child_procedure, C.c_void_p).value)
            if not old:
                raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
            self.old_procedures[child] = old
        self.user.EnableWindow(self.children[REVIEW], False)
        self.user.EnableWindow(self.children[APPROVE], False)
        self._position(window)
        self._layout(window)
        self.user.SendMessageW(window, 0x401, CANCEL, 0)  # DM_SETDEFID: cancel
        self.user.SetFocus(self.children[CANCEL])
        if not self.user.SetTimer(window, 1, 50, None):
            raise RuntimeError('BATCH_CONFIRMATION_TIMER_UNAVAILABLE')

    def _review_state(self):
        scroll = SCROLLINFO(size=C.sizeof(SCROLLINFO), mask=7)
        if not self.user.GetScrollInfo(self.children[DETAILS], 1, C.byref(scroll)):
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        self.seen_end |= scroll.position >= scroll.maximum - max(0, scroll.page - 1)
        self.user.EnableWindow(self.children[REVIEW], self.seen_end)
        checked = self.user.SendMessageW(self.children[REVIEW], 0xF0, 0, 0) == 1
        self.user.EnableWindow(self.children[APPROVE], self.seen_end and checked)
        return self.seen_end and checked

    def _expired(self):
        return (self.cancelled is not None and self.cancelled.is_set()
                or self.deadline is not None and time.monotonic() >= self.deadline)

    def _child_message(self, window, message, wparam, lparam):
        try:
            return self._child_key(window, message, wparam, lparam)
        except BaseException:
            # Exceptions must not escape a ctypes callback into Win32.
            self.failed = True
            self.user.EndDialog(self.window, NO)
            return 0

    def _child_key(self, window, message, wparam, lparam):
        # Keep Enter/Esc out of the dialog manager's dynamic default-button
        # handling, including when Approve has keyboard focus. Space/click on
        # the enabled Approve control is the deliberate affirmative action.
        if message == 0x87 and lparam:  # WM_GETDLGCODE
            key = C.cast(lparam, C.POINTER(MSG)).contents
            if key.message in (0x100, 0x104) and key.wparam in (13, 27):
                return 4  # DLGC_WANTALLKEYS for this message only
        if message in (0x100, 0x104) and wparam in (13, 27):
            parent = self.window
            self.user.EndDialog(parent, NO)
            return 0
        return self.user.CallWindowProcW(self.old_procedures[window], window, message, wparam, lparam)

    def _message(self, window, message, wparam, lparam):
        try:
            if message == 0x110:  # WM_INITDIALOG
                self.window = window
                self._create(window)
                return 0  # focus was explicitly set to Cancel
            if message == 0x113:  # WM_TIMER
                if self._expired():
                    self.user.EndDialog(window, NO)
                else:
                    self._review_state()
                return 1
            if message == 0x111:  # WM_COMMAND
                identifier, event = wparam & 0xFFFF, wparam >> 16
                if identifier in (1, CANCEL):
                    self.user.EndDialog(window, NO)
                    return 1
                if identifier == APPROVE and event == 0:
                    if not self._expired() and self._review_state():
                        self.user.EndDialog(window, YES)
                    return 1
            if message == 0x10:  # WM_CLOSE
                self.user.EndDialog(window, NO)
                return 1
            if message == 0x2E0:  # WM_DPICHANGED
                self.seen_end = False
                self.user.SendMessageW(self.children[REVIEW], 0xF1, 0, 0)
                self._position(window)
                self._layout(window)
                return 1
            if message == 2:  # WM_DESTROY
                self.user.KillTimer(window, 1)
        except BaseException:
            self.failed = True
            self.user.EndDialog(window, NO)
            return 1
        return 0

    def show(self):
        previous = self.user.SetThreadDpiAwarenessContext(HWND(-4))
        if not previous:
            raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
        title = title_for(self.summary)
        template = C.create_string_buffer(struct.pack('<IIHhhhhHH', DIALOG_STYLE, 0, 0,
                                                       0, 0, 200, 150, 0, 0)
                                          + title.encode('utf-16-le') + b'\0\0')
        try:
            answer = self.user.DialogBoxIndirectParamW(None, template, self.owner, self.procedure, 0)
            if self.failed or answer in (0, -1):
                raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
            return answer
        finally:
            if self.font:
                self.gdi.DeleteObject(self.font)
            if not self.user.SetThreadDpiAwarenessContext(previous):
                raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')


def confirm_public_scope(owner, summary, *, timeout_seconds, cancelled):
    if os.name != 'nt':
        raise RuntimeError('BATCH_CONFIRMATION_DISPLAY_UNAVAILABLE')
    return _Dialog(owner, summary, timeout_seconds, cancelled).show()
