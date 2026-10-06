"""Public Alt+Z visibility input, independent of the recording toggle."""
import ctypes
from ctypes import wintypes
import os
import time

from cs2pov.adapters.nvidia import Win32NvidiaHotkey, NvidiaInputError
from cs2pov.adapters.native_console import INPUT, INPUTUNION, KEYBDINPUT, KEYUP, MODIFIER_KEYS
from cs2pov.adapters.owned_process import process_identity


class OverlayHotkey(Win32NvidiaHotkey):
    def __init__(self, game, expected_identity, *, clock=time.monotonic, user32=None):
        super().__init__(user32, game=game)
        self.expected_identity, self.clock = expected_identity, clock

    def _activate_owned_game(self):
        api = self.user32
        api.IsWindowVisible.argtypes = [wintypes.HWND]
        api.IsWindowVisible.restype = wintypes.BOOL
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        api.EnumWindows.restype = wintypes.BOOL
        api.SetForegroundWindow.argtypes = [wintypes.HWND]
        api.SetForegroundWindow.restype = wintypes.BOOL
        windows = []
        def collect(hwnd, _):
            if api.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                if api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) and pid.value == self.expected_identity.pid:
                    windows.append(hwnd)
            return True
        if not api.EnumWindows(callback_type(collect), 0) or len(windows) != 1:
            raise NvidiaInputError('不能确认唯一受管理游戏窗口，未切换前台。')
        if self.game.verify() != self.expected_identity or not api.SetForegroundWindow(windows[0]):
            raise NvidiaInputError('不能切到本次受管理游戏，未发送浮窗热键。')

    def send_once(self, direction, overlay_identity, *, deadline, input_guard):
        if direction not in ('open', 'close'):
            raise NvidiaInputError('浮窗操作无效。')
        def verify():
            if (self.clock() >= deadline or input_guard() is not True
                    or self.game.verify() != self.expected_identity
                    or process_identity(overlay_identity.pid) != overlay_identity
                    or '-insecure' not in (self.game.argv or [])):
                raise NvidiaInputError('浮窗输入的任务、来源或原期限失效。')
        verify()
        foreground = self.foreground_pid()
        # Only the product's own foreground may be moved after its user action.
        if direction == 'open' and foreground == os.getpid():
            self._activate_owned_game()
            verify()
            foreground = self.foreground_pid()
        allowed = (self.expected_identity.pid,) if direction == 'open' else (self.expected_identity.pid, overlay_identity.pid)
        if foreground not in allowed:
            raise NvidiaInputError('前台不是本次游戏或已核验浮窗，未发送热键。')
        if any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in (*MODIFIER_KEYS, 0x5A)):
            raise NvidiaInputError('检测到修饰键或 Z 按住，未发送浮窗热键。')
        events = [INPUT(1, INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))
                  for vk, flags in ((0x12, 0), (0x5A, 0), (0x5A, KEYUP), (0x12, KEYUP))]
        verify()
        if self.foreground_pid() not in allowed:
            raise NvidiaInputError('浮窗输入前前台已变化。')
        batch = (INPUT * len(events))(*events)
        sent = self.user32.SendInput(len(events), batch, ctypes.sizeof(INPUT))
        if sent != len(events):
            cleanup = self._release_partial(events, sent) if 0 < sent < len(events) else ''
            raise NvidiaInputError('浮窗热键未完整送达；禁止自动重试。' + cleanup)
        verify()
