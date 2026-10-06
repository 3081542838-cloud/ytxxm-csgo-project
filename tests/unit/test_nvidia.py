from types import SimpleNamespace
import ctypes

import pytest

from cs2pov.adapters.nvidia import (NvidiaInputError, Win32NvidiaHotkey,
                                     hotkey_events, parse_hotkey)
from cs2pov.adapters.owned_process import ProcessIdentity


class FakeFn:
    def __init__(self, fn):
        self.fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.fn(*args)


class FakeUser32:
    def __init__(self):
        self.pid = 42
        self.held = set()
        self.sent = []
        self.events = []
        self.send_result = None
        self.send_results = []
        self.GetForegroundWindow = FakeFn(lambda: 1)
        self.GetWindowThreadProcessId = FakeFn(self._pid)
        self.GetAsyncKeyState = FakeFn(lambda key: 0x8000 if key in self.held else 0)
        self.SendInput = FakeFn(self._send)

    def _pid(self, _hwnd, pointer):
        pointer._obj.value = self.pid
        return 1

    def _send(self, count, batch, size):
        self.sent.append((count, [batch[i].data.ki.wVk for i in range(count)]))
        self.events.append([(batch[i].data.ki.wVk, batch[i].data.ki.dwFlags) for i in range(count)])
        if self.send_results:
            value = self.send_results.pop(0)
            if isinstance(value, Exception): raise value
        else:
            value = count if self.send_result is None else self.send_result
        for key, flags in self.events[-1][:value]:
            if flags & 2: self.held.discard(key)
            else: self.held.add(key)
        return value


def game(identity=None):
    identity = identity or ProcessIdentity(42, 123, 'C:/game/cs2.exe')
    return SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=lambda: identity)


def test_hotkey_parser_and_event_order_are_bounded():
    assert parse_hotkey('Alt+F9') == ((0x12,), 0x78)
    events = hotkey_events('Ctrl+Alt+F12')
    assert len(events) == 6
    assert [event.data.ki.wVk for event in events] == [0x11, 0x12, 0x7B, 0x7B, 0x12, 0x11]
    assert events[0].data.ki.dwFlags == 0 and events[-1].data.ki.dwFlags == 0x2


@pytest.mark.parametrize('value', ['', 'F9', 'Alt+Alt+F9', 'Win+F9', 'Alt+F13', 'Alt+F0'])
def test_invalid_hotkey_never_produces_input(value):
    with pytest.raises(NvidiaInputError):
        hotkey_events(value)


def test_native_hotkey_sends_one_batch_and_checks_game_identity():
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    adapter.send_once('Alt+F9', game())
    assert user32.sent == [(4, [0x12, 0x78, 0x78, 0x12])]


def test_preflight_uses_only_unheld_key_release_and_never_recording_toggle():
    from cs2pov.adapters.nvidia import check_input_available
    api = FakeUser32()
    check_input_available(api)
    assert api.events == [[(0x87, 2)]]
    assert api.held == set()


def test_preflight_rejection_is_not_retried_and_reports_before_game_changes():
    from cs2pov.adapters.nvidia import check_input_available
    api = FakeUser32()
    api.send_result = 0
    with pytest.raises(NvidiaInputError, match='游戏文件尚未修改'):
        check_input_available(api)
    assert len(api.sent) == 1
    assert api.held == set()


def test_preflight_never_releases_user_held_key():
    from cs2pov.adapters.nvidia import check_input_available
    api = FakeUser32()
    api.held.add(0x87)
    with pytest.raises(NvidiaInputError, match='按住'):
        check_input_available(api)
    assert not api.sent
    assert api.held == {0x87}


def test_zero_inserted_events_can_recover_without_duplicate_toggle():
    user32 = FakeUser32()
    user32.send_results = [0, 4]
    waits = []
    adapter = Win32NvidiaHotkey(user32, zero_attempts=3, wait=waits.append)
    adapter.send_once('Alt+F9', game())
    assert len(user32.sent) == 2 and waits == [0.15]
    assert adapter.delivery_attempts == [0, 4]


def test_zero_recovery_rechecks_foreground_and_never_retries_partial_input():
    user32 = FakeUser32()
    user32.send_results = [0, 4]
    def move_foreground(_seconds):
        user32.pid = 99
    adapter = Win32NvidiaHotkey(user32, zero_attempts=3, wait=move_foreground)
    with pytest.raises(NvidiaInputError, match='未发送'):
        adapter.send_once('Alt+F9', game())
    assert len(user32.sent) == 1
    user32 = FakeUser32()
    user32.send_results = [2, 2]
    adapter = Win32NvidiaHotkey(user32, zero_attempts=3, wait=lambda _: pytest.fail('partial retry'))
    with pytest.raises(NvidiaInputError, match='未知'):
        adapter.send_once('Alt+F9', game())
    assert len(user32.sent) == 2  # Original input plus keyup-only cleanup.


def test_zero_exhaustion_is_distinct_and_contains_delivery_counts():
    from cs2pov.adapters.nvidia import NvidiaNotSentError
    user32 = FakeUser32()
    user32.send_result = 0
    adapter = Win32NvidiaHotkey(user32, zero_attempts=3, wait=lambda _: None)
    with pytest.raises(NvidiaNotSentError, match='0/4'):
        adapter.send_once('Alt+F9', game())
    assert adapter.delivery_attempts == [0, 0, 0]
    assert len(user32.sent) == 3 and user32.held == set()


def test_held_key_partial_delivery_and_identity_change_never_retry():
    user32 = FakeUser32(); adapter = Win32NvidiaHotkey(user32); target = game()
    user32.held.add(0x12)
    with pytest.raises(NvidiaInputError): adapter.send_once('Alt+F9', target)
    assert user32.sent == []

    user32.held.clear(); user32.send_results = [3]
    with pytest.raises(NvidiaInputError, match='未知'):
        adapter.send_once('Alt+F9', target)
    assert user32.events == [[(0x12, 0), (0x78, 0), (0x78, 2), (0x12, 2)], [(0x12, 2)]]
    assert sum(any(flags == 0 for _, flags in batch) for batch in user32.events) == 1

    user32.send_result = None
    user32.pid = 99
    with pytest.raises(NvidiaInputError): adapter.send_once('Alt+F9', target)
    assert len(user32.sent) == 2  # One original toggle and one keyup-only cleanup.


def test_missing_insecure_or_foreground_blocks_before_send():
    user32 = FakeUser32(); adapter = Win32NvidiaHotkey(user32)
    target = game(); target.argv = ['cs2.exe']
    with pytest.raises(NvidiaInputError): adapter.send_once('Alt+F9', target)
    assert user32.sent == []
    target = game(); user32.pid = 999
    with pytest.raises(NvidiaInputError): adapter.send_once('Alt+F9', target)
    assert user32.sent == []


@pytest.mark.parametrize('inserted', range(6))
def test_every_partial_prefix_has_at_most_one_toggle_and_only_keyup_cleanup(inserted):
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    target = game()
    user32.send_results = [inserted]
    with pytest.raises(NvidiaInputError, match='未发送' if inserted == 0 else '未知'):
        adapter.send_once('Ctrl+Alt+F12', target, expected_identity=target.verify())
    assert user32.held == set()
    assert len(user32.events) == (1 if inserted == 0 else 2)
    assert sum(any(flags == 0 for _, flags in batch) for batch in user32.events) == 1
    if inserted:
        assert all(flags == 2 for _, flags in user32.events[1])
        assert all(key in (0x11, 0x12, 0x7B) for key, _ in user32.events[1])
    # The only function-key down belongs to the original batch.
    assert sum(key == 0x7B and flags == 0 for batch in user32.events for key, flags in batch) == 1


@pytest.mark.parametrize('cleanup_result', [0, OSError('cleanup unavailable')])
def test_cleanup_failure_stays_unknown_and_is_not_retried(cleanup_result):
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    user32.send_results = [2, cleanup_result]
    with pytest.raises(NvidiaInputError, match='释放'):
        adapter.send_once('Alt+F9', game())
    assert len(user32.events) == 2
    assert user32.events[1] == [(0x78, 2), (0x12, 2)]
    assert all(flags == 2 for _, flags in user32.events[1])
    assert user32.held == {0x78, 0x12}


@pytest.mark.parametrize('changed', [ProcessIdentity(42, 124, 'C:/game/cs2.exe'),
                                    ProcessIdentity(42, 123, 'C:/other/cs2.exe')])
def test_frozen_identity_rejects_pid_reuse_or_executable_change_before_input(changed):
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    expected = game().verify()
    with pytest.raises(NvidiaInputError, match='冻结'):
        adapter.send_once('Alt+F9', game(changed), expected_identity=expected)
    assert user32.sent == [] and user32.held == set()


def test_identity_change_after_full_input_is_unknown_and_cannot_cause_retry_or_cleanup():
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    target = game()
    expected = target.verify()
    identities = iter([expected, ProcessIdentity(42, 124, 'C:/game/cs2.exe')])
    target.verify = lambda: next(identities)
    with pytest.raises(NvidiaInputError, match='未知'):
        adapter.send_once('Alt+F9', target, expected_identity=expected)
    assert len(user32.events) == 1 and user32.held == set()


def test_process_disappearing_after_full_input_is_reported_as_unknown_without_retry():
    user32 = FakeUser32()
    adapter = Win32NvidiaHotkey(user32)
    target = game()
    expected = target.verify()
    calls = []
    def verify():
        calls.append(True)
        if len(calls) == 2: raise OSError('process exited')
        return expected
    target.verify = verify
    with pytest.raises(NvidiaInputError, match='未知'):
        adapter.send_once('Alt+F9', target, expected_identity=expected)
    assert len(user32.events) == 1 and user32.held == set()


@pytest.mark.parametrize('held', [0x10, 0x11, 0x12, 0x5B, 0x5C, 0x78])
def test_user_held_keys_are_never_released_or_toggled(held):
    user32 = FakeUser32(); user32.held.add(held)
    adapter = Win32NvidiaHotkey(user32)
    with pytest.raises(NvidiaInputError, match='按住'): adapter.send_once('Alt+F9', game())
    assert user32.sent == [] and user32.held == {held}
