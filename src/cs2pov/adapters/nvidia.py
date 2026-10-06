"""Fail-closed NVIDIA App hotkey delivery.

The overlay owns recording state. This adapter only sends one configured,
validated chord to the already foreground, -insecure game and never retries a
partial or uncertain SendInput result.
"""
import ctypes
import re
import sys
import time
from ctypes import wintypes

from cs2pov.adapters.native_console import INPUT, INPUTUNION, KEYBDINPUT, KEYUP, MODIFIER_KEYS
from cs2pov.storage.settings import DataError


class NvidiaInputError(DataError):
    pass


class NvidiaNotSentError(NvidiaInputError):
    """Every attempted batch inserted zero events; no toggle was delivered."""


MODIFIER_VK = {"Ctrl": 0x11, "Alt": 0x12, "Shift": 0x10}
HOTKEY = re.compile(r"(?:(?:Ctrl|Alt|Shift)\+){1,3}F(?:[1-9]|1[0-2])")


def parse_hotkey(value):
    if not isinstance(value, str) or HOTKEY.fullmatch(value) is None:
        raise NvidiaInputError("NVIDIA 热键必须是 Ctrl / Alt / Shift 加 F1–F12。")
    parts = value.split("+")
    modifiers, function = parts[:-1], parts[-1]
    if len(set(modifiers)) != len(modifiers):
        raise NvidiaInputError("NVIDIA 热键修饰键不能重复。")
    return tuple(MODIFIER_VK[item] for item in modifiers), 0x70 + int(function[1:]) - 1


def ensure_playback_hotkey_compatible(value):
    """F8 is reserved for the verified, self-unbinding local playback action."""
    parsed = parse_hotkey(value)
    if parsed[1] == 0x77:
        raise NvidiaInputError('F8 用于片段播放，录制热键不能含 F8；请设置其他功能键。')
    return parsed


def hotkey_events(value):
    modifiers, function = parse_hotkey(value)
    events = []
    for key in modifiers:
        events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(key, 0, 0, 0, 0))))
    events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(function, 0, 0, 0, 0))))
    events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(function, 0, KEYUP, 0, 0))))
    for key in reversed(modifiers):
        events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(key, 0, KEYUP, 0, 0))))
    return events


def check_input_available(user32=None):
    """Check a general input denial with one unheld F24 key-up, never a toggle.

    Passing this check does not prove a later game hotkey will be accepted.
    It only detects a current desktop-wide SendInput failure before deployment.
    """
    if user32 is None:
        if sys.platform != 'win32':
            raise NvidiaInputError('自动录制仅支持 Windows。')
        user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    if user32.GetAsyncKeyState(0x87) & 0x8000:
        raise NvidiaInputError('检测到 F24 按住，请松开后再开始；游戏文件尚未修改。')
    event = INPUT(1, INPUTUNION(ki=KEYBDINPUT(0x87, 0, KEYUP, 0, 0)))
    batch = (INPUT * 1)(event)
    if sys.platform == 'win32':
        ctypes.set_last_error(0)
    sent = user32.SendInput(1, batch, ctypes.sizeof(INPUT))
    error = ctypes.get_last_error() if sys.platform == 'win32' else 0
    if sent != 1:
        raise NvidiaInputError(
            f'Windows 当前拒绝自动按键（错误码{error}）。本次录制尚未开始，游戏文件尚未修改。\n'
            '请确认桌面已解锁；关闭可能影响输入的程序后再试。若仍失败，可重启后仅打开 Steam、NVIDIA 和本应用。\n'
            '这项错误不能单独证明是权限或某个平台导致；请勿反复点击开始录制。')


class Win32NvidiaHotkey:
    """One-shot input guarded by the held foreground game identity."""
    def __init__(self, user32=None, *, game=None, zero_attempts=None, wait=time.sleep):
        native = user32 is None
        if user32 is None:
            if sys.platform != "win32":
                raise NvidiaInputError("NVIDIA 原生热键仅支持 Windows。")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32 = user32
        self.game = game
        self.zero_attempts = (3 if native else 1) if zero_attempts is None else zero_attempts
        if type(self.zero_attempts) is not int or not 1 <= self.zero_attempts <= 3:
            raise NvidiaInputError('零输入恢复次数必须为1至3。')
        self.wait = wait
        self.delivery_attempts = []
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

    def _release_partial(self, events, inserted):
        # SendInput inserts this batch serially. Release only keys whose down
        # event belongs to the inserted prefix and has no matching up yet.
        # A cleanup batch contains no down events and never retries a toggle.
        held = []
        for event in events[:inserted]:
            key = event.data.ki.wVk
            if event.data.ki.dwFlags & KEYUP:
                if key in held:
                    held.remove(key)
            elif key not in held:
                held.append(key)
        if not held:
            return ''
        cleanup = [INPUT(1, INPUTUNION(ki=KEYBDINPUT(key, 0, KEYUP, 0, 0)))
                   for key in reversed(held)]
        batch = (INPUT * len(cleanup))(*cleanup)
        try:
            result = self.user32.SendInput(len(cleanup), batch, ctypes.sizeof(INPUT))
        except Exception:
            return '；按键释放也无法确认，请手动松开键盘'
        if result != len(cleanup):
            return '；按键释放未完整送达，请手动松开键盘'
        return '；已仅释放本次残留按键'

    def send_once(self, hotkey, game=None, *, expected_identity=None):
        game = game if game is not None else self.game
        if game is None:
            raise NvidiaInputError("NVIDIA 热键缺少受管理游戏身份。")
        modifiers, function = parse_hotkey(hotkey)
        identity = game.verify()
        if expected_identity is not None and identity != expected_identity:
            raise NvidiaInputError('游戏身份与冻结的录制任务不一致，未发送 NVIDIA 热键。')
        if "-insecure" not in (game.argv or []) or self.foreground_pid() != identity.pid:
            raise NvidiaInputError("受管理 CS2 未在前台或缺少 -insecure，未发送 NVIDIA 热键。")
        if any(self.user32.GetAsyncKeyState(key) & 0x8000
               for key in (*MODIFIER_KEYS, function)):
            raise NvidiaInputError("检测到修饰键或录制热键按住；未发送 NVIDIA 热键。")
        events = hotkey_events(hotkey)
        batch = (INPUT * len(events))(*events)
        self.delivery_attempts = []
        for attempt in range(self.zero_attempts):
            if attempt:
                self.wait(0.15)
                try:
                    if (game.verify() != identity or '-insecure' not in (game.argv or [])
                            or self.foreground_pid() != identity.pid
                            or any(self.user32.GetAsyncKeyState(key) & 0x8000
                                   for key in (*MODIFIER_KEYS, function))):
                        raise NvidiaNotSentError('前台、游戏身份或按键状态改变；未发送录制热键。')
                except Exception as error:
                    raise NvidiaNotSentError('零输入恢复已终止；未发送录制热键：' + str(error)) from error
            if sys.platform == 'win32':
                ctypes.set_last_error(0)
            sent = self.user32.SendInput(len(events), batch, ctypes.sizeof(INPUT))
            winerror = ctypes.get_last_error() if sys.platform == 'win32' else 0
            self.delivery_attempts.append(sent)
            if sent != 0:
                break  # Partial/full insertion consumes the toggle; never resend.
        if sent == 0:
            raise NvidiaNotSentError(
                f'Windows 未发送录制热键（0/{len(events)}，尝试{len(self.delivery_attempts)}次，'
                f'错误码{winerror}）。请确保游戏与应用使用相同权限，且桌面未锁定。')
        if sent != len(events):
            # Zero means no inserted event, so no user's key is released.
            # Cleanup failure does not permit an additional cleanup retry.
            cleanup = self._release_partial(events, sent) if 0 < sent < len(events) else ''
            raise NvidiaInputError(f"NVIDIA 热键未完整送达（{sent}/{len(events)}，错误码{winerror}）；录制状态未知，禁止自动重试。" + cleanup)
        try:
            after = game.verify()
        except Exception as error:
            raise NvidiaInputError('热键发送后游戏身份无法核验；录制状态未知，禁止重试。') from error
        if (after != identity or "-insecure" not in (game.argv or [])
                or self.foreground_pid() != identity.pid):
            raise NvidiaInputError("热键发送后游戏身份或前台变化；录制状态未知，禁止重试。")
