import pytest
from types import SimpleNamespace
from tests.unit.test_nvidia import FakeUser32, game
from cs2pov.adapters import overlay_hotkey
from cs2pov.adapters.overlay_hotkey import OverlayHotkey
from cs2pov.adapters.nvidia import NvidiaInputError
from cs2pov.adapters.owned_process import ProcessIdentity


@pytest.fixture
def env(monkeypatch):
    api=FakeUser32()
    g=game()
    identity=ProcessIdentity(123,456,'overlay.exe')
    e=SimpleNamespace(api=api,game=g,identity=identity,now=100.,valid=True)
    monkeypatch.setattr(overlay_hotkey,'process_identity',lambda pid:e.identity)
    e.adapter=OverlayHotkey(g,g.verify(),user32=api,clock=lambda:e.now)
    return e


def send(e,direction='open'):
    e.adapter.send_once(direction,e.identity,deadline=102.,input_guard=lambda:e.valid)


def test_visibility_hotkey_is_alt_z_and_never_recording_function_key(env):
    send(env)
    assert env.api.sent==[(4,[0x12,0x5A,0x5A,0x12])]
    assert env.api.held==set()


@pytest.mark.parametrize('count',range(4))
def test_partial_input_only_releases_own_inserted_keys_and_does_not_retry(env,count):
    env.api.send_results=[count]
    with pytest.raises(NvidiaInputError): send(env)
    assert env.api.held==set()
    assert sum(any(flags==0 for _,flags in batch) for batch in env.api.events)==1
    assert all(flags==2 for _,flags in env.api.events[-1]) if count else len(env.api.events)==1


@pytest.mark.parametrize('fault',['foreground','held','deadline','contract','identity','secure'])
def test_invalid_native_context_blocks_before_visibility_input(env,fault):
    if fault=='foreground': env.api.pid=999
    if fault=='held': env.api.held.add(0x5A)
    if fault=='deadline': env.now=102.
    if fault=='contract': env.valid=False
    if fault=='identity': env.game.verify=lambda:ProcessIdentity(42,999,'game.exe')
    if fault=='secure': env.game.argv=['cs2.exe']
    with pytest.raises(NvidiaInputError): send(env)
    assert not env.api.sent


def test_only_close_can_be_delivered_to_exact_provider_foreground(env):
    env.api.pid=env.identity.pid
    with pytest.raises(NvidiaInputError): send(env)
    assert not env.api.sent
    send(env,'close')
    assert len(env.api.sent)==1


def test_post_input_scope_loss_reports_failure_without_second_toggle(env):
    original=env.api.SendInput.fn
    def changed(*args):
        sent=original(*args)
        env.valid=False
        return sent
    env.api.SendInput.fn=changed
    with pytest.raises(NvidiaInputError): send(env)
    assert len(env.api.sent)==1
