"""Guarded Windows keyboard input for an explicitly opened CS2 console.

SendInput only reports insertion into the input stream. It does not prove that
CS2 accepted a command, which Demo is loaded, or who is being spectated.
"""
import ctypes
from ctypes import wintypes
import sys
from cs2pov.storage.settings import DataError


class ConsoleInputError(DataError):
    pass


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("data", INPUTUNION)]


KEYUP = 0x0002
UNICODE = 0x0004
VK_RETURN = 0x0D
VK_F8 = 0x77
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)


def command_events(command: str):
    if (not isinstance(command, str) or not 0 < len(command) <= 512
            or any(c in command for c in "\r\n\x00")
            or any(0xD800 <= ord(c) <= 0xDFFF for c in command)):
        raise ConsoleInputError("控制台命令为空、过长或包含换行/控制字符。")
    units = command.encode("utf-16-le")
    events = []
    for offset in range(0, len(units), 2):
        unit = int.from_bytes(units[offset:offset + 2], "little")
        events.extend((INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, unit, UNICODE, 0, 0))),
                       INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, unit, UNICODE | KEYUP, 0, 0)))))
    events.extend((INPUT(1, INPUTUNION(ki=KEYBDINPUT(VK_RETURN, 0, 0, 0, 0))),
                   INPUT(1, INPUTUNION(ki=KEYBDINPUT(VK_RETURN, 0, KEYUP, 0, 0)))))
    return events


class Win32Keyboard:
    """No focus activation and no clipboard; target must already be foreground."""
    def __init__(self):
        if sys.platform != "win32":
            raise ConsoleInputError("原生键盘仅支持 Windows。")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def foreground_pid(self):
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return None
        return pid.value or None

    def input_ready(self, *, playback=False):
        """Read held keys so callers can wait within their existing deadline."""
        if type(playback) is not bool:
            return False
        keys = (*MODIFIER_KEYS, VK_F8) if playback else MODIFIER_KEYS
        try:
            for key in keys:
                state = self.user32.GetAsyncKeyState(key)
                if type(state) is not int or state & 0x8000:
                    return False
            return True
        except Exception:
            return False

    def send_command(self, command):
        # Held modifiers can change console behavior; never release the
        # user's keys or synthesize an unverified correction.
        if any(self.user32.GetAsyncKeyState(key) & 0x8000 for key in
               MODIFIER_KEYS):
            raise ConsoleInputError("检测到修饰键按住；请松开键盘后重试。")
        events = command_events(command)
        batch = (INPUT * len(events))(*events)
        sent = self.user32.SendInput(len(events), batch, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise ConsoleInputError("键盘输入未完整送达；当前命令结果未知，禁止自动重试。")

    def send_play_key(self):
        """One physical F8 pair; no key correction or automatic retry."""
        if any(self.user32.GetAsyncKeyState(key) & 0x8000
               for key in (*MODIFIER_KEYS, VK_F8)):
            raise ConsoleInputError("检测到修饰键或 F8 按住；未发送播放按键。")
        events = (INPUT(1, INPUTUNION(ki=KEYBDINPUT(VK_F8, 0, 0, 0, 0))),
                  INPUT(1, INPUTUNION(ki=KEYBDINPUT(VK_F8, 0, KEYUP, 0, 0))))
        batch = (INPUT * 2)(*events)
        if self.user32.SendInput(2, batch, ctypes.sizeof(INPUT)) != 2:
            raise ConsoleInputError("播放按键未完整送达；结果未知，禁止自动重试。")


class NativeConsole:
    """Optional session-local readback; keyboard delivery alone proves nothing."""
    def __init__(self, game, keyboard=None, *, reader=None):
        self.game = game
        self.keyboard = keyboard if keyboard is not None else Win32Keyboard()
        self.console_open_confirmed = False
        self.reader = reader

    def confirm_open_and_empty(self):
        identity = self.game.verify()
        if "-insecure" not in (self.game.argv or []):
            raise ConsoleInputError("受管理游戏缺少 -insecure 启动证据。")
        self.console_open_confirmed = True
        return identity

    def foreground_pid(self):
        return self.keyboard.foreground_pid()

    def input_ready(self, *, playback=False):
        """Advisory read only: existing input authorization is preserved."""
        if type(playback) is not bool:
            return False
        try:
            identity = self.game.verify()
            if ("-insecure" not in (self.game.argv or [])
                    or self.foreground_pid() != identity.pid):
                return False
            missing = object()
            predicate = getattr(self.keyboard, "input_ready", missing)
            if predicate is not missing:
                if not callable(predicate) or predicate(playback=playback) is not True:
                    return False
            # Injected legacy keyboards keep their independent send guards.
            # Recheck ownership after reading keys without changing authority.
            return (self.game.verify() == identity
                    and "-insecure" in (self.game.argv or [])
                    and self.foreground_pid() == identity.pid)
        except Exception:
            return False

    def command(self, value):
        try:
            identity = self.game.verify()
        except Exception:
            self.console_open_confirmed = False
            raise
        if (not self.console_open_confirmed or "-insecure" not in (self.game.argv or [])
                or self.foreground_pid() != identity.pid):
            self.console_open_confirmed = False
            raise ConsoleInputError("控制台未确认、游戏身份变化或前台失焦，停止输入。")
        try:
            self.keyboard.send_command(value)
            after = self.game.verify()
        except Exception:
            self.console_open_confirmed = False
            raise
        if (after != identity or "-insecure" not in (self.game.argv or [])
                or self.foreground_pid() != identity.pid):
            self.console_open_confirmed = False
            raise ConsoleInputError("输入后游戏身份或前台变化；命令是否执行未知，禁止自动重试。")

    def hide_console(self):
        """Fixed command; closing the console always consumes confirmation."""
        try:
            self.command("hideconsole")
        finally:
            self.console_open_confirmed = False

    def press_play_key(self):
        """Send a single F8 batch after the upper layer's one-use authorization.

        A caller must separately prove the restricted temporary binding. Neither
        successful insertion nor this guard proves that playback began.
        """
        try:
            identity = self.game.verify()
            if (self.console_open_confirmed or "-insecure" not in (self.game.argv or [])
                    or self.foreground_pid() != identity.pid):
                raise ConsoleInputError("控制台尚未收起、游戏身份变化或前台失焦，未发送播放按键。")
            self.keyboard.send_play_key()
            after = self.game.verify()
            if (after != identity or "-insecure" not in (self.game.argv or [])
                    or self.foreground_pid() != identity.pid):
                raise ConsoleInputError("播放按键发送后游戏身份或前台变化；结果未知，禁止自动重试。")
        finally:
            self.console_open_confirmed = False

    def readback(self):
        return self._read("readback")

    def snapshot(self):
        return self._read("snapshot")

    def _read(self, method):
        if self.reader is None:
            return None
        try:
            identity = self.game.verify()
            if "-insecure" not in (self.game.argv or []):
                raise ConsoleInputError("回读游戏缺少 -insecure 启动证据。")
            proof = getattr(self.reader, method)()
            if proof is not None and proof.process_identity != identity:
                raise ConsoleInputError("回读会话与受管理游戏身份不一致。")
            return proof
        except Exception:
            self.console_open_confirmed = False
            raise
