from types import SimpleNamespace
import ctypes
import pytest
from cs2pov.adapters.native_console import (ConsoleInputError, NativeConsole, INPUT,
                                             command_events, UNICODE, KEYUP, VK_RETURN,
                                             Win32Keyboard, VK_F8, MODIFIER_KEYS)
from cs2pov.adapters.owned_process import ProcessIdentity


def fixture_console():
    identity = ProcessIdentity(42, 123, "C:/game/cs2.exe")
    game = SimpleNamespace(argv=["cs2.exe", "-insecure"], verify=lambda: identity)
    keyboard = SimpleNamespace(pid=42, sent=[], play_keys=[], foreground_pid=lambda: keyboard.pid,
                               send_command=lambda value: keyboard.sent.append(value))
    keyboard.send_play_key = lambda: keyboard.play_keys.append("F8")
    return NativeConsole(game, keyboard), game, keyboard


def test_unicode_command_is_one_key_batch_with_enter_and_no_clipboard():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    events = command_events('spec_player " 一条小虾米OVO"')
    assert events[-2].data.ki.wVk == VK_RETURN
    assert events[-1].data.ki.dwFlags == KEYUP
    assert all(event.data.ki.dwFlags & UNICODE for event in events[:-2])
    units = [event.data.ki.wScan for event in events[:-2:2]]
    assert bytes(value for unit in units for value in unit.to_bytes(2, "little")).decode("utf-16-le") == 'spec_player " 一条小虾米OVO"'


@pytest.mark.parametrize("value", ["", "a\nquit", "a\rquit", "a\0quit", "x" * 513, "\ud800"])
def test_command_rejects_unsafe_or_incomplete_text(value):
    with pytest.raises(ConsoleInputError):
        command_events(value)


def test_native_console_requires_explicit_open_and_blocks_focus_loss():
    console, game, keyboard = fixture_console()
    with pytest.raises(ConsoleInputError):
        console.command("demo_pause")
    console.confirm_open_and_empty()
    console.command("demo_pause")
    keyboard.pid = 99
    with pytest.raises(ConsoleInputError, match="失焦"):
        console.command("demo_resume")
    assert keyboard.sent == ["demo_pause"] and not console.console_open_confirmed


def test_partial_or_unknown_input_invalidates_console_state():
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    def fail(_):
        raise ConsoleInputError("partial SendInput")
    keyboard.send_command = fail
    with pytest.raises(ConsoleInputError, match="partial"):
        console.command("demo_pause")
    assert not console.console_open_confirmed and console.readback() is None


def test_game_identity_or_insecure_loss_blocks_without_input():
    console, game, keyboard = fixture_console()
    console.confirm_open_and_empty()
    game.argv = ["cs2.exe"]
    with pytest.raises(ConsoleInputError):
        console.command("demo_resume")
    assert keyboard.sent == []
    game.argv = ["cs2.exe", "-insecure"]
    console.confirm_open_and_empty()
    game.verify = lambda: (_ for _ in ()).throw(ConsoleInputError("identity changed"))
    with pytest.raises(ConsoleInputError, match="identity changed"):
        console.command("demo_resume")
    assert not console.console_open_confirmed and keyboard.sent == []


def test_readback_is_bound_to_current_owned_insecure_process():
    from cs2pov.services.replay import ReplayEvidence
    from pathlib import Path
    from dataclasses import replace
    console, game, keyboard = fixture_console()
    identity = game.verify()
    proof = ReplayEvidence(identity,True,Path('C:/demo.dem'),10,100,True,'123',True,1.)
    reader = SimpleNamespace(readback=lambda:proof)
    console.reader = reader
    console.confirm_open_and_empty()
    assert console.readback()==proof and keyboard.sent==[]
    reader.readback = lambda:replace(proof,process_identity=ProcessIdentity(42,124,'C:/game/cs2.exe'))
    with pytest.raises(ConsoleInputError,match='不一致'): console.readback()
    assert not console.console_open_confirmed
    game.argv=['cs2.exe']
    with pytest.raises(ConsoleInputError,match='insecure'): console.readback()


def test_readback_errors_invalidate_input_confirmation_and_are_not_suppressed():
    console,_,_ = fixture_console(); console.confirm_open_and_empty()
    def fail(): raise OSError('log unavailable')
    console.reader=SimpleNamespace(readback=fail)
    with pytest.raises(OSError,match='unavailable'): console.readback()
    assert not console.console_open_confirmed


@pytest.mark.parametrize('fault', ['identity', 'insecure', 'verify_exception'])
def test_console_command_post_input_ownership_change_is_unknown_and_revokes_authority(fault):
    console, game, keyboard = fixture_console()
    console.confirm_open_and_empty()
    def send(value):
        keyboard.sent.append(value)
        if fault == 'identity':
            game.verify = lambda: ProcessIdentity(42, 124, 'C:/game/cs2.exe')
        elif fault == 'insecure':
            game.argv = ['cs2.exe']
        else:
            game.verify = lambda: (_ for _ in ()).throw(ConsoleInputError('ownership unavailable'))
    keyboard.send_command = send
    with pytest.raises(ConsoleInputError):
        console.command('demo_pause')
    assert keyboard.sent == ['demo_pause']
    assert not console.console_open_confirmed
    with pytest.raises(ConsoleInputError):
        console.command('demo_resume')
    assert keyboard.sent == ['demo_pause']


def fake_win32_keyboard(*, held=None, send_count=2):
    keyboard = Win32Keyboard.__new__(Win32Keyboard)
    observed = SimpleNamespace(checked=[], batches=[])
    def key_state(key):
        observed.checked.append(key)
        return 0x8000 if key == held else 0
    def send(count, batch, size):
        observed.batches.append((count, size, [
            (item.type, item.data.ki.wVk, item.data.ki.wScan,
             item.data.ki.dwFlags, item.data.ki.time, item.data.ki.dwExtraInfo)
            for item in batch]))
        return send_count
    keyboard.user32 = SimpleNamespace(GetAsyncKeyState=key_state, SendInput=send)
    return keyboard, observed


def test_play_key_sends_only_one_physical_f8_down_up_batch():
    keyboard, observed = fake_win32_keyboard()
    keyboard.send_play_key()
    assert observed.checked == [*MODIFIER_KEYS, VK_F8]
    assert observed.batches == [(2, ctypes.sizeof(INPUT), [
        (1, VK_F8, 0, 0, 0, 0), (1, VK_F8, 0, KEYUP, 0, 0)])]


@pytest.mark.parametrize("held", [*MODIFIER_KEYS, VK_F8])
def test_play_key_does_not_send_or_release_a_users_held_keys(held):
    keyboard, observed = fake_win32_keyboard(held=held)
    with pytest.raises(ConsoleInputError, match="按住"):
        keyboard.send_play_key()
    assert observed.batches == []


@pytest.mark.parametrize("send_count", [0, 1])
def test_partial_play_key_delivery_is_unknown_without_retry_or_correction(send_count):
    keyboard, observed = fake_win32_keyboard(send_count=send_count)
    with pytest.raises(ConsoleInputError, match="禁止自动重试"):
        keyboard.send_play_key()
    assert len(observed.batches) == 1 and observed.batches[0][0] == 2


def test_native_play_key_requires_console_confirmation_to_be_consumed():
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    with pytest.raises(ConsoleInputError, match="控制台尚未收起"):
        console.press_play_key()
    assert keyboard.play_keys == [] and not console.console_open_confirmed
    console.press_play_key()
    assert keyboard.play_keys == ["F8"] and keyboard.sent == []


@pytest.mark.parametrize("fault", ["foreground", "insecure", "identity"])
def test_native_play_key_blocks_pre_input_guard_failure_without_retry(fault):
    console, game, keyboard = fixture_console()
    if fault == "foreground":
        keyboard.pid = 99
    elif fault == "insecure":
        game.argv = ["cs2.exe"]
    else:
        game.verify = lambda: (_ for _ in ()).throw(ConsoleInputError("identity changed"))
    with pytest.raises(ConsoleInputError):
        console.press_play_key()
    assert keyboard.play_keys == [] and keyboard.sent == []
    assert not console.console_open_confirmed


@pytest.mark.parametrize("fault", ["foreground", "insecure", "identity", "verify_exception"])
def test_native_play_key_post_input_change_is_unknown_and_never_retried(fault):
    console, game, keyboard = fixture_console()
    def send():
        keyboard.play_keys.append("F8")
        if fault == "foreground":
            keyboard.pid = 99
        elif fault == "insecure":
            game.argv = ["cs2.exe"]
        elif fault == "identity":
            # Same PID with a different birth time is a different process.
            game.verify = lambda: ProcessIdentity(42, 124, "C:/game/cs2.exe")
        else:
            game.verify = lambda: (_ for _ in ()).throw(ConsoleInputError("identity changed"))
    keyboard.send_play_key = send
    with pytest.raises(ConsoleInputError):
        console.press_play_key()
    assert keyboard.play_keys == ["F8"] and keyboard.sent == []
    assert not console.console_open_confirmed


def test_native_play_key_partial_failure_does_not_issue_a_second_batch():
    console, _, keyboard = fixture_console()
    def fail():
        keyboard.play_keys.append("partial F8")
        raise ConsoleInputError("partial SendInput; retry forbidden")
    keyboard.send_play_key = fail
    with pytest.raises(ConsoleInputError, match="partial"):
        console.press_play_key()
    assert keyboard.play_keys == ["partial F8"] and keyboard.sent == []
    assert not console.console_open_confirmed


def test_hide_console_is_one_fixed_command_and_always_revokes_confirmation():
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    console.hide_console()
    assert keyboard.sent == ["hideconsole"] and keyboard.play_keys == []
    assert not console.console_open_confirmed


@pytest.mark.parametrize("fault", ["foreground", "partial"])
def test_hide_console_failure_also_revokes_confirmation_without_retry(fault):
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    if fault == "foreground":
        keyboard.pid = 99
    else:
        def fail(value):
            keyboard.sent.append(value)
            raise ConsoleInputError("partial SendInput")
        keyboard.send_command = fail
    with pytest.raises(ConsoleInputError):
        console.hide_console()
    assert keyboard.sent == ([] if fault == "foreground" else ["hideconsole"])
    assert keyboard.play_keys == [] and not console.console_open_confirmed


@pytest.mark.parametrize("playback", [False, True])
@pytest.mark.parametrize("held", MODIFIER_KEYS)
def test_keyboard_readiness_waits_for_each_held_modifier_without_input(playback, held):
    keyboard, observed = fake_win32_keyboard(held=held)
    assert keyboard.input_ready(playback=playback) is False
    assert held in observed.checked and observed.batches == []
    keyboard.user32.GetAsyncKeyState = lambda _key: 0
    assert keyboard.input_ready(playback=playback) is True
    assert observed.batches == []


@pytest.mark.parametrize("playback", [False, True])
def test_keyboard_readiness_only_reads_the_required_keys(playback):
    keyboard, observed = fake_win32_keyboard()
    assert keyboard.input_ready(playback=playback) is True
    assert observed.checked == [*MODIFIER_KEYS, *([VK_F8] if playback else [])]
    assert observed.batches == []


def test_keyboard_readiness_checks_f8_only_for_playback():
    keyboard, observed = fake_win32_keyboard(held=VK_F8)
    assert keyboard.input_ready() is True
    assert keyboard.input_ready(playback=True) is False
    assert observed.batches == []


@pytest.mark.parametrize("state", [None, "up", True])
def test_keyboard_readiness_rejects_invalid_key_state_without_input(state):
    keyboard, observed = fake_win32_keyboard()
    keyboard.user32.GetAsyncKeyState = lambda _key: state
    assert keyboard.input_ready() is False
    assert observed.batches == []


@pytest.mark.parametrize("playback", [False, True])
def test_keyboard_readiness_fail_closed_on_key_state_errors(playback):
    keyboard, observed = fake_win32_keyboard()
    def fail(_key):
        raise OSError("key state unavailable")
    keyboard.user32.GetAsyncKeyState = fail
    assert keyboard.input_ready(playback=playback) is False
    assert observed.batches == []


@pytest.mark.parametrize("playback", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
def test_console_readiness_is_read_only_and_preserves_existing_authority(playback, confirmed):
    console, _, keyboard = fixture_console()
    console.console_open_confirmed = confirmed
    observed = []
    def ready(*, playback=False):
        observed.append(playback)
        return True
    keyboard.input_ready = ready
    assert console.input_ready(playback=playback) is True
    assert observed == [playback]
    assert console.console_open_confirmed is confirmed
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("value", [False, None, 0, 1, "ready"])
def test_console_readiness_requires_a_strict_true_predicate(value):
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    keyboard.input_ready = lambda **_kwargs: value
    assert console.input_ready() is False
    assert console.console_open_confirmed is True
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("fault", ["foreground", "insecure", "identity", "foreground_error", "predicate_error"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_console_readiness_guard_failure_is_false_without_input_or_authority_change(fault, confirmed):
    console, game, keyboard = fixture_console()
    console.console_open_confirmed = confirmed
    calls = []
    def ready(**_kwargs):
        calls.append("ready")
        if fault == "predicate_error":
            raise OSError("key state unavailable")
        return True
    keyboard.input_ready = ready
    if fault == "foreground":
        keyboard.pid = 99
    elif fault == "insecure":
        game.argv = ["cs2.exe"]
    elif fault == "identity":
        game.verify = lambda: (_ for _ in ()).throw(ConsoleInputError("identity changed"))
    elif fault == "foreground_error":
        keyboard.foreground_pid = lambda: (_ for _ in ()).throw(OSError("foreground unavailable"))
    assert console.input_ready() is False
    assert calls == (["ready"] if fault == "predicate_error" else [])
    assert console.console_open_confirmed is confirmed
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("fault", ["identity", "foreground", "insecure"])
def test_console_readiness_rechecks_ownership_after_reading_keys(fault):
    console, game, keyboard = fixture_console()
    console.confirm_open_and_empty()
    def ready(**_kwargs):
        if fault == "identity":
            game.verify = lambda: ProcessIdentity(42, 124, "C:/game/cs2.exe")
        elif fault == "foreground":
            keyboard.pid = 99
        else:
            game.argv = ["cs2.exe"]
        return True
    keyboard.input_ready = ready
    assert console.input_ready() is False
    assert console.console_open_confirmed is True
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("playback", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
def test_console_readiness_keeps_old_injected_keyboards_compatible(playback, confirmed):
    console, _, keyboard = fixture_console()
    console.console_open_confirmed = confirmed
    assert console.input_ready(playback=playback) is True
    assert console.console_open_confirmed is confirmed
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("value", [True, "ready"])
def test_console_readiness_rejects_a_non_callable_predicate(value):
    console, _, keyboard = fixture_console()
    console.confirm_open_and_empty()
    keyboard.input_ready = value
    assert console.input_ready() is False
    assert console.console_open_confirmed is True
    assert keyboard.sent == [] and keyboard.play_keys == []


@pytest.mark.parametrize("playback", [False, True])
def test_readiness_does_not_authorize_input_after_a_key_becomes_held(playback):
    console, _, _ = fixture_console()
    keyboard, observed = fake_win32_keyboard()
    keyboard.foreground_pid = lambda: 42
    console.keyboard = keyboard
    if not playback:
        console.confirm_open_and_empty()
    assert console.input_ready(playback=playback) is True
    keyboard.user32.GetAsyncKeyState = lambda key: 0x8000 if key == MODIFIER_KEYS[0] else 0
    with pytest.raises(ConsoleInputError, match="按住"):
        if playback:
            console.press_play_key()
        else:
            console.command("demo_pause")
    assert observed.batches == [] and not console.console_open_confirmed


@pytest.mark.parametrize("held", MODIFIER_KEYS)
def test_command_sender_still_rejects_each_held_modifier(held):
    keyboard, observed = fake_win32_keyboard(held=held)
    with pytest.raises(ConsoleInputError, match="按住"):
        keyboard.send_command("demo_pause")
    assert observed.batches == []


@pytest.mark.parametrize("send_count", [0, 1])
def test_command_sender_partial_input_stays_unknown_without_retry(send_count):
    keyboard, observed = fake_win32_keyboard(send_count=send_count)
    with pytest.raises(ConsoleInputError, match="禁止自动重试"):
        keyboard.send_command("demo_pause")
    assert len(observed.batches) == 1
