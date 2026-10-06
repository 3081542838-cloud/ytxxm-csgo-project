from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.owned_process import OwnedGame,ProcessIdentity
from cs2pov.services.preview_session import PreviewSession
from cs2pov.storage.settings import Settings,HudPreset,DataError
from cs2pov.storage.transaction import pending_sessions
from tests.integration.test_hud_trial import check_fixture,candidate


def setup(tmp_path,monkeypatch):
    from cs2pov.services import hud_trial
    monkeypatch.setattr(hud_trial,'cs2_closed',lambda:True)
    check,original=check_fixture(tmp_path)
    check.demo.write_bytes(b'PBDEMS2\0'+b'unchanged original demo')
    fp=fingerprint(check.demo)
    analysis=dict(tick_rate=64,timeline=[[12500,15832],[12538,15870],[12602,15934],[13400,16732]],
                  players=[dict(id='76561199198478034',name=' 一条小虾米OVO')])
    draft=dict(demo=str(check.demo),fingerprint=fp,selection=dict(start_tick=12602,end_tick=13400,
        server_start_tick=15934,server_end_tick=16732,player_id='76561199198478034',
        content_sha256=fp['sha256'],hud=asdict(HudPreset())))
    settings=Settings(installation=str(check.installation),cfg=str(check.protected_configs[0].parent),
                      video_directory=str(check.output))
    env=SimpleNamespace(running=True,launched=0)
    exe=check.installation/'game/bin/win64/cs2.exe'
    identity=ProcessIdentity(123,456,str(exe))
    def popen(*_,**__):
        env.launched+=1
        return SimpleNamespace(pid=123,poll=lambda:None if env.running else 0)
    game=OwnedGame(query=lambda _:identity,closed=lambda:True,popen=popen,
                   terminate=lambda _:setattr(env,'running',False))
    preview=PreviewSession(tmp_path/'data/sessions',settings,analysis,draft,candidate(),game=game,
        console_factory=lambda _:SimpleNamespace())
    return preview,env,check,original


def test_actual_session_resource_and_transaction_can_start_and_restore(tmp_path,monkeypatch):
    preview,env,check,original=setup(tmp_path,monkeypatch)
    preview.start()
    assert preview.state=='awaiting_game' and env.launched==1
    assert preview.resource.path.parent==preview.resource_directory/'resources'
    assert preview.session.parent not in preview.resource.path.parents
    assert json.loads((preview.session/'trial-context.json').read_text())['gameinfo_sha256']==check.gameinfo_sha256
    assert pending_sessions(preview.session.parent)==[preview.session]
    assert (check.installation/'game/csgo/pov.vpk').read_bytes()==preview.resource.path.read_bytes()
    preview.stop()
    assert preview.state=='complete' and pending_sessions(preview.session.parent)==[]
    assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
    assert not (check.installation/'game/csgo/pov.vpk').exists()
    assert check.protected_configs[0].read_bytes()==b'user settings'
    assert fingerprint(check.demo)==preview.draft['fingerprint']


def test_actual_checker_failure_does_not_block_a_retry(tmp_path,monkeypatch):
    preview,env,check,_=setup(tmp_path,monkeypatch)
    (check.installation/'game/csgo/steam.inf').write_text('appID=730\nPatchVersion=future\n')
    with pytest.raises(RuntimeError,match='版本'):preview.start()
    assert not preview.session.exists() and pending_sessions(preview.session.parent)==[] and env.launched==0
    (check.installation/'game/csgo/steam.inf').write_text('appID=730\nPatchVersion=1.41.8.8\n')
    retry=PreviewSession(preview.session.parent,preview.settings,preview.analysis,preview.draft,candidate(),
                         game=preview.game,console_factory=lambda _:SimpleNamespace())
    retry.start();assert retry.state=='awaiting_game';retry.stop()
    assert pending_sessions(preview.session.parent)==[]


@pytest.mark.parametrize('failure',['journal.json','trial-context.json'])
def test_initial_persistence_failure_stays_outside_active_sessions_and_never_touches_game(tmp_path,monkeypatch,failure):
    from cs2pov.services import hud_trial
    from cs2pov.storage import transaction
    preview,env,check,original=setup(tmp_path,monkeypatch)
    module=transaction if failure=='journal.json' else hud_trial
    write=module.atomic_write
    def fail(path,raw):
        if path.name==failure:raise OSError('injected initial persistence failure')
        write(path,raw)
    monkeypatch.setattr(module,'atomic_write',fail)
    with pytest.raises(OSError,match='initial persistence'):preview.start()
    assert env.launched==0 and not preview.session.exists()
    assert pending_sessions(preview.session.parent)==[]
    assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
    assert not (check.installation/'game/csgo/pov.vpk').exists()
    assert check.protected_configs[0].read_bytes()==b'user settings'
    monkeypatch.setattr(module,'atomic_write',write)
    retry=PreviewSession(preview.session.parent,preview.settings,preview.analysis,preview.draft,candidate(),
                         game=preview.game,console_factory=lambda _:SimpleNamespace())
    retry.start();retry.stop()
    assert pending_sessions(preview.session.parent)==[]
