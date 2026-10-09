from dataclasses import asdict,replace
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplaySnapshot
from cs2pov.services.hud_trial import TrialCheck
from cs2pov.services.preview_session import PreviewSession
from cs2pov.services.replay import ReplayEvidence,ReplayError
from cs2pov.storage.settings import DataError,HudPreset,Settings
from cs2pov.storage.transaction import pending_sessions


@pytest.mark.parametrize('patch,automatic,deferred', [
    ('1.41.8.9', True, True), ('1.41.8.8', True, False),
    ('1.41.8.9', False, False)])
def test_patch_specific_seek_strategy_is_selected_only_for_automatic_new_patch(tmp_path, monkeypatch,
        patch, automatic, deferred):
    from cs2pov.services import preview_session
    calls = []
    sentinel = object()
    def factory(*args, **kwargs):
        calls.append(kwargs)
        return sentinel
    monkeypatch.setattr(preview_session, 'ReplayPreparation', factory)
    preview = PreviewSession(tmp_path, Settings(), {}, {}, tmp_path/'base.vpk', automatic=automatic)
    preview.check = SimpleNamespace(patch_version=patch)
    assert preview._new_preparation() is sentinel
    assert calls == ([{'deferred_seek_pause': True}] if deferred else [{}])


def setup(tmp_path):
    path=tmp_path/'original.dem'; path.write_bytes(b'demo remains unchanged')
    fp=fingerprint(path)
    analysis=dict(tick_rate=64,timeline=[[t,t+3332] for t in range(12500,13401)],
                  players=[dict(id='76561199198478034',name=' 一条小虾米OVO')])
    draft=dict(demo=str(path),fingerprint=fp,selection=dict(start_tick=12602,end_tick=13400,
        server_start_tick=15934,server_end_tick=16732,player_id='76561199198478034',
        content_sha256=fp['sha256'],hud=asdict(HudPreset())))
    settings=Settings(installation=str(tmp_path/'game'),cfg=str(tmp_path/'cfg'),video_directory=str(tmp_path/'videos'))
    identity=ProcessIdentity(42,123,'C:/game/cs2.exe')
    env=SimpleNamespace(time=100.,pid=99,running=False,launched=0,closed=0,restored=0,
                        deployed=0,sent=[],proof=None,snapshot=None,restore_failure=False)
    game=SimpleNamespace(process=None,argv=None,verify=lambda:identity)
    def launch(installation,session,*,telemetry):
        assert telemetry is True
        env.launched+=1; env.running=True
        game.argv=['cs2.exe','-insecure']
        game.process=SimpleNamespace(poll=lambda:None if env.running else 0)
        (session/'engine.log').write_text('startup\n')
        return identity
    def close(): env.closed+=1; env.running=False
    game.launch=launch; game.force_close=close
    transaction=SimpleNamespace(data={'complete':False,'prepared':True})
    def deploy(): env.deployed+=1
    def restore():
        assert not env.running, 'Cannot restore while game is still running'
        env.restored+=1
        if env.restore_failure: raise DataError('conflicting external file')
        transaction.data['complete']=True
        return dict(gameinfo='restored',pov='restored')
    transaction.deploy=deploy; transaction.restore=restore
    def builder(base,path):
        path.write_bytes(b'a small verified local resource')
        return SimpleNamespace(path=path,nonce='SESSION_0000000001')
    def checker(installation,demo,output,resource,cfg,*,telemetry):
        return TrialCheck(installation,'0'*64,demo,output,20_000_000_000,(),patch_version='1.41.8.8')
    def console_factory(game):
        from cs2pov.adapters.native_console import NativeConsole
        keyboard=SimpleNamespace(foreground_pid=lambda:env.pid,send_command=env.sent.append)
        return NativeConsole(game,keyboard)
    def reader_factory(session,id,nonce,*,clock):
        assert id==identity and nonce=='SESSION_0000000001'
        return SimpleNamespace(readback=lambda:env.proof,snapshot=lambda:env.snapshot)
    def preparer(check,session,resource):
        assert not session.exists(), 'Transaction owns creation of this directory'
        session.mkdir(parents=True)
        (session/'journal.json').write_text('fake journal for unit stub')
        (session/'trial-context.json').write_text('fake context for unit stub')
        return transaction
    preview=PreviewSession(tmp_path/'sessions',settings,analysis,draft,tmp_path/'base.vpk',
        clock=lambda:env.time,game=game,console_factory=console_factory,reader_factory=reader_factory,
        checker=checker,preparer=preparer,builder=builder)
    proof=ReplayEvidence(identity,True,path,12538,15871,True,'76561199797409805',True,100.)
    return preview,env,proof


def test_unicode_session_path_is_rejected_before_resources_or_game_changes(tmp_path):
    preview, env, _ = setup(tmp_path)
    preview.session = tmp_path / '中文便携目录' / 'sessions' / preview.session.name
    with pytest.raises(DataError, match='英文路径'):
        preview.start()
    assert env.launched == env.deployed == env.restored == 0
    assert not preview.resource_directory.exists()
    assert not preview.session.exists()


@pytest.mark.parametrize('show', [False, True])
def test_preview_builds_native_radar_from_its_frozen_snapshot(tmp_path, show):
    preview, env, _ = setup(tmp_path)
    preview.draft['selection']['hud']['show_radar'] = show
    original = preview.builder
    calls = []
    def builder(base, path, **kwargs):
        calls.append(kwargs)
        return original(base, path)
    preview.builder = builder
    preview.start()
    assert calls == ([{'native_radar': True}] if show else [{}])
    preview.stop()
    preview.poll()
    assert env.restored == 1 and not env.running


def automatic_setup(tmp_path):
    from cs2pov.adapters.binding_log import BindingEvidence
    from cs2pov.adapters.command_pipe import CommandPipes
    from cs2pov.adapters.game_startup import GameStartupEvidence
    from cs2pov.adapters.pipe_console import PipeConsole
    from cs2pov.services.hidden_playback import HiddenPlayback
    preview, env, proof = setup(tmp_path)
    # Automatic preparation uses the measured three-second lead-in. Keep the
    # exact start/end proofs below independent of this setup-only pause point.
    preview.analysis['timeline'] = [[t,t+3332] for t in range(12400,13401)]
    proof = replace(proof, tick=12410, server_tick=15743)
    identity = proof.process_identity
    env.startup = None; env.bootstrap = False; env.pipe_closed = []
    preview.automatic = True
    preview.draft['selection']['duration'] = 12.46875
    original_launch = preview.game.launch
    def launch(installation, session, *, telemetry, command_pipes):
        assert command_pipes.state == 'created' and len(command_pipes.handles) == 2
        return original_launch(installation, session, telemetry=telemetry)
    preview.game.launch = launch
    def pipe_factory(**kwargs):
        handles = iter((11, 12))
        def write(handle, payload, **_):
            env.sent.append(payload.decode('utf-8').rstrip('\n'))
            return len(payload)
        backend = SimpleNamespace(create=lambda _: next(handles), close=env.pipe_closed.append,
            connect=lambda _: True, client_pid=lambda _: 42, drain=lambda *_, **__: 0, write=write)
        return CommandPipes(backend=backend, **kwargs)
    preview.pipes_factory = pipe_factory
    preview.startup_factory = lambda *_, **__: SimpleNamespace(poll=lambda: env.startup)
    def bindings(session, actual_identity, nonce, *, expected=None, clock):
        return SimpleNamespace(decoder=SimpleNamespace(state='waiting'),
            readback=lambda: BindingEvidence(identity,'F8',expected or '',env.time,nonce) if env.bootstrap else None)
    def console(game, pipes, session, **kwargs):
        keyboard=SimpleNamespace(foreground_pid=lambda: env.pid)
        return PipeConsole(game,pipes,session,keyboard=keyboard,binding_reader_factory=bindings,**kwargs)
    preview.pipe_console_factory = console
    preview.playback_factory = lambda *args, **kwargs: HiddenPlayback(*args,reader_factory=bindings,**kwargs)
    env.startup_valid = lambda: GameStartupEvidence(identity, env.time)
    return preview, env, proof


def automatic_ready(preview, env, proof):
    preview.start()
    assert preview.state == 'connecting_game' and env.sent == []
    preview.poll(); assert env.sent == []
    env.startup = env.startup_valid(); preview.poll()
    assert preview.state == 'checking_command_pipe' and len(env.sent) == 1
    preview.poll(); assert len(env.sent) == 1
    env.bootstrap = True; preview.poll()
    assert preview.state == 'loading' and env.sent[-1].startswith('playdemo ')
    env.time += 1
    env.snapshot = ReplaySnapshot(proof.process_identity, proof.path, True, proof.tick,
                                  True, proof.player_id, True, env.time)
    preview.poll(); assert preview.state == 'preparing'
    env.proof = replace(proof,observed_at=env.time); preview.poll()
    assert preview.preparation.state == 'resuming' and env.sent[-1] == 'demo_resume'
    deadline = preview.preparation.deadline
    env.time += .5
    env.snapshot = replace(env.snapshot, tick=12420, paused=False, observed_at=env.time)
    preview.poll()
    assert preview.preparation.state == 'starting'
    assert preview.preparation.deadline == deadline and env.sent[-1] == 'demo_pauseatservertick 15934'
    env.time += .5
    env.proof = replace(proof,tick=12602,server_tick=15934,observed_at=env.time); preview.poll()
    env.time += 1
    env.proof = replace(env.proof,player_id='76561199198478034',observed_at=env.time); preview.poll()
    assert preview.state == 'ready' and env.sent[-1] == 'hideconsole'
    assert env.pid == 99 and not preview.console.console_open_confirmed


def test_automatic_preview_load_seek_player_and_play_without_console_confirmations(tmp_path):
    preview, env, proof = automatic_setup(tmp_path)
    automatic_ready(preview, env, proof)
    with pytest.raises(DataError,match='无需'): preview.confirm_console()
    preview.prepare_playback(); preview.poll(); preview.poll()
    assert preview.state == 'play_ready' and preview.playback.binding_may_exist
    preview.play_clip(); preview.poll()
    assert preview.state == 'awaiting_play_foreground' and not preview.playback.triggered
    env.pid = 42; preview.poll()
    assert preview.state == 'playing' and preview.playback.triggered
    env.time += .5
    env.snapshot = ReplaySnapshot(proof.process_identity,proof.path,True,12630,False,
                                  '76561199198478034',True,env.time)
    preview.poll()
    assert preview.playback.pipe_end_attempted and env.sent[-1] == 'demo_pauseatservertick 16732'
    env.time += 12
    env.snapshot = replace(env.snapshot,tick=13400,paused=True,observed_at=env.time)
    env.proof = replace(env.proof,tick=13400,server_tick=16732,observed_at=env.time)
    preview.poll(); assert preview.state == 'play_ended'
    preview.check_playback_binding(); preview.poll()
    assert preview.state == 'play_verified' and not preview.playback.binding_may_exist
    preview.stop(); preview.stop()
    assert preview.state == 'complete' and env.closed == env.restored == 1
    assert sorted(env.pipe_closed) == [11,12]
    assert fingerprint(proof.path) == preview.draft['fingerprint']


@pytest.mark.parametrize('phase', ['connecting_game','checking_command_pipe','loading'])
def test_automatic_cancel_revokes_pipe_and_never_uses_late_observation(tmp_path, phase):
    preview, env, proof = automatic_setup(tmp_path); preview.start()
    if phase != 'connecting_game':
        env.startup = env.startup_valid(); preview.poll()
    if phase == 'loading':
        env.bootstrap = True; preview.poll()
    assert preview.state == phase
    count = len(env.sent)
    preview.stop()
    env.startup = env.startup_valid(); env.bootstrap = True
    env.snapshot = ReplaySnapshot(proof.process_identity,proof.path,True,12538,True,proof.player_id,True,env.time)
    preview.poll()
    assert preview.state == 'complete' and len(env.sent) == count
    assert env.closed == env.restored == 1 and sorted(env.pipe_closed) == [11,12]


def test_automatic_no_startup_or_no_execution_receipt_cannot_load_demo(tmp_path):
    preview, env, proof = automatic_setup(tmp_path); preview.start(); preview.poll()
    assert env.sent == []
    env.startup = env.startup_valid(); preview.poll()
    env.time += 5
    with pytest.raises(DataError): preview.poll()
    assert not any(command.startswith('playdemo ') for command in env.sent)
    assert preview.state == 'complete' and env.closed == env.restored == 1


def test_automatic_slow_startup_does_not_extend_original_deadline(tmp_path):
    preview, env, proof = automatic_setup(tmp_path); preview.start()
    def slow():
        env.time = preview.deadline
        return env.startup_valid()
    preview.startup_factory = lambda *_,**__: SimpleNamespace(poll=slow)
    with pytest.raises(DataError,match='期限'): preview.poll()
    assert env.sent == [] and preview.state == 'complete'


def test_automatic_pipe_creation_failure_restores_before_game_launch(tmp_path):
    preview, env, proof = automatic_setup(tmp_path)
    def unavailable(**kwargs): raise OSError('named pipe ACL creation failed')
    preview.pipes_factory = unavailable
    with pytest.raises(OSError): preview.start()
    assert env.sent == [] and env.launched == env.deployed == 0
    assert env.restored == 1 and preview.state == 'complete'


def test_automatic_pipe_cleanup_failure_restores_files_but_keeps_session_blocked(tmp_path):
    preview, env, proof = automatic_setup(tmp_path); preview.start(); preview.poll()
    remaining = {11}
    def close(handle):
        if handle in remaining: raise OSError('transient CloseHandle failure')
        env.pipe_closed.append(handle)
    preview.pipes.backend.close = close
    preview.stop()
    assert env.restored == 1 and env.closed == 1
    assert preview.state == 'recovery_blocked' and preview.pipes.handles == [11]
    assert env.sent == []
    remaining.clear()
    preview.stop()
    assert preview.state == 'complete' and preview.pipes.handles == []
    assert sorted(env.pipe_closed) == [11,12] and env.closed == 1 and env.sent == []


@pytest.mark.parametrize('action', ['prepare','final_query'])
def test_automatic_action_input_failure_closes_and_restores_instead_of_leaving_ready(tmp_path, action):
    preview, env, proof = automatic_setup(tmp_path); automatic_ready(preview,env,proof)
    def fail(*_,**__): raise OSError('uncertain pipe write')
    preview.pipes.backend.write = fail
    if action == 'prepare':
        with pytest.raises(DataError): preview.prepare_playback()
    else:
        preview.prepare_playback = lambda: None
        from cs2pov.services.hidden_playback import HiddenPlayback
        preview.playback = HiddenPlayback(preview.controller,preview.session,preview.draft,
                                           clock=preview.clock)
        preview.state = 'play_ended'; preview.playback.state = 'ended'
        preview.playback.started_at = env.time-1
        preview.playback.binding_may_exist = True
        env.proof = replace(env.proof,tick=13400,server_tick=16732)
        with pytest.raises(DataError): preview.check_playback_binding()
    assert preview.state == 'complete' and env.closed == env.restored == 1
    assert preview.console.state == 'closed' and sorted(env.pipe_closed) == [11,12]


def test_automatic_demo_replaced_during_bootstrap_is_not_loaded(tmp_path):
    preview, env, proof = automatic_setup(tmp_path); preview.start()
    env.startup = env.startup_valid(); preview.poll()
    proof.path.write_bytes(b'changed original while waiting for command receipt')
    env.bootstrap = True
    with pytest.raises(DataError,match='Demo'): preview.poll()
    assert not any(command.startswith('playdemo ') for command in env.sent)
    assert preview.state == 'complete' and env.closed == env.restored == 1


def load(preview,env,proof):
    preview.start()
    assert preview.state=='awaiting_console' and env.sent==[]
    preview.confirm_console()
    assert preview.poll() is None and env.sent==[]  # App still foreground.
    env.pid=42; preview.poll()
    assert preview.state=='loading' and env.sent==[f'playdemo "{proof.path.as_posix()}"']
    env.time+=1
    env.snapshot=ReplaySnapshot(proof.process_identity,proof.path,True,0,False,proof.player_id,True,env.time)
    preview.poll()
    assert preview.state=='awaiting_demo_console' and len(env.sent)==1
    assert not preview.console_confirmed and not preview.console.console_open_confirmed
    preview.poll()
    assert len(env.sent)==1  # A map load cannot carry the original console confirmation.
    preview.confirm_console()
    env.pid=99; preview.poll(); assert len(env.sent)==1
    env.pid=42; preview.poll()
    assert preview.state=='preparing' and env.sent[-1]=='demo_gototick 12538; demo_pause'


def test_session_requires_console_focus_and_readback_before_preview_then_restores(tmp_path):
    preview,env,proof=setup(tmp_path); load(preview,env,proof)
    for changes in ({},{'tick':12602,'server_tick':15934},
                    {'tick':12602,'server_tick':15934,'player_id':'76561199198478034'}):
        env.time+=1; env.proof=replace(proof,observed_at=env.time,**changes); preview.poll()
    assert preview.state=='ready' and preview.controller.state=='preview_verified'
    assert not preview.console.console_open_confirmed  # Preview may close the console; input authority expires.
    assert env.launched==env.deployed==1 and env.closed==env.restored==0
    assert not any('F9' in c for c in env.sent)
    count=len(env.sent)
    preview.stop(); preview.stop()
    assert preview.state=='complete' and env.closed==env.restored==1 and len(env.sent)==count
    state=json.loads((preview.session/'preview-state.json').read_text(encoding='utf-8'))
    assert state['state']=='complete' and state['restoration']['gameinfo']=='restored'
    assert fingerprint(proof.path)==preview.draft['fingerprint']


@pytest.mark.parametrize('hotkey', ['Alt+F8', 'Ctrl+F8', 'Shift+F8',
    'Ctrl+Alt+F8', 'Ctrl+Shift+F8', 'Alt+Shift+F8', 'Ctrl+Alt+Shift+F8'])
def test_direct_service_blocks_conflicting_recording_key_before_resource(tmp_path, hotkey):
    preview, env, proof = setup(tmp_path)
    preview.settings = replace(preview.settings, hotkey=hotkey)
    with pytest.raises(DataError, match='F8'):
        preview.start()
    assert env.deployed == env.launched == env.restored == 0
    assert env.sent == [] and not preview.resource_directory.exists()
    assert not preview.session.exists()
    assert fingerprint(proof.path) == preview.draft['fingerprint']


def test_loading_waits_for_correct_fresh_local_demo_not_any_game_output(tmp_path):
    preview,env,proof=setup(tmp_path); preview.start(); preview.confirm_console(); env.pid=42; preview.poll()
    original=ReplaySnapshot(proof.process_identity,proof.path,True,0,False,proof.player_id,True,100.)
    for changes in ({'local_demo':False},{'path':Path('C:/other.dem')},{'observed_at':99.},
                    {'process_identity':ProcessIdentity(42,124,'C:/game/cs2.exe')}):
        env.snapshot=replace(original,**changes)
        if 'process_identity' in changes:
            with pytest.raises(DataError): preview.poll()
            assert preview.state=='complete' and env.closed==env.restored==1
        else:
            assert preview.poll() is None and preview.state=='loading' and len(env.sent)==1


def test_focus_loss_during_preparation_closes_owned_game_then_restores(tmp_path):
    preview,env,proof=setup(tmp_path); load(preview,env,proof)
    env.pid=99
    with pytest.raises(ReplayError,match='前台'): preview.poll()
    assert preview.state=='complete' and env.closed==env.restored==1
    assert len(env.sent)==3 and '前台' in preview.error


def test_missing_confirmation_timeout_does_not_send_input(tmp_path):
    preview,env,_=setup(tmp_path); preview.start(); env.time+=120
    with pytest.raises(ReplayError,match='超时'): preview.poll()
    assert env.sent==[] and env.closed==env.restored==1


def test_engine_startup_and_console_confirmation_have_separate_bounded_steps(tmp_path):
    preview,env,_=setup(tmp_path)
    launch=preview.game.launch
    def delayed(*args,**kwargs):
        result=launch(*args,**kwargs)
        (args[1]/'engine.log').unlink()
        return result
    preview.game.launch=delayed;preview.start()
    assert preview.state=='awaiting_game' and env.sent==[]
    with pytest.raises(DataError):preview.confirm_console()
    env.time+=119;preview.poll();assert preview.state=='awaiting_game'
    (preview.session/'engine.log').write_text('engine initialized\n')
    preview.poll();assert preview.state=='awaiting_console'
    env.time+=119;preview.poll();assert preview.state=='awaiting_console' and env.sent==[]
    env.time+=1
    with pytest.raises(ReplayError,match='超时'):preview.poll()
    assert preview.state=='complete' and env.closed==env.restored==1


def test_startup_timeout_still_closes_and_restores_without_input(tmp_path):
    preview,env,_=setup(tmp_path)
    launch=preview.game.launch
    def delayed(*args,**kwargs):
        result=launch(*args,**kwargs);(args[1]/'engine.log').unlink();return result
    preview.game.launch=delayed;preview.start();env.time+=120
    with pytest.raises(ReplayError,match='超时'):preview.poll()
    assert preview.state=='complete' and env.sent==[] and env.restored==1


def test_cancel_before_confirmation_never_toggles_recording_or_sends_pause(tmp_path):
    preview,env,_=setup(tmp_path); preview.start(); preview.stop()
    assert preview.state=='complete' and env.sent==[] and env.restored==1
    with pytest.raises(DataError): preview.confirm_console()


def test_loaded_demo_timeout_never_sends_commands_to_a_closed_console(tmp_path):
    preview,env,proof=setup(tmp_path)
    preview.start();preview.confirm_console();env.pid=42;preview.poll()
    env.time+=1
    env.snapshot=ReplaySnapshot(proof.process_identity,proof.path,True,0,False,proof.player_id,True,env.time)
    preview.poll()
    assert preview.state=='awaiting_demo_console' and len(env.sent)==1
    env.time+=120
    with pytest.raises(ReplayError,match='超时'): preview.poll()
    assert preview.state=='complete' and env.closed==env.restored==1 and len(env.sent)==1


def test_second_console_confirmation_still_needs_current_demo_readback(tmp_path):
    preview,env,proof=setup(tmp_path)
    preview.start();preview.confirm_console();env.pid=42;preview.poll()
    env.time+=1
    env.snapshot=ReplaySnapshot(proof.process_identity,proof.path,True,0,False,proof.player_id,True,env.time)
    preview.poll();preview.confirm_console()
    env.snapshot=replace(env.snapshot,path=Path('C:/different.dem'))
    assert preview.poll() is None and len(env.sent)==1 and preview.state=='awaiting_demo_console'
    env.snapshot=replace(env.snapshot,path=proof.path,observed_at=env.time-6)
    assert preview.poll() is None and len(env.sent)==1
    preview.stop();assert env.restored==1


def test_restore_conflict_is_not_marked_complete(tmp_path):
    preview,env,_=setup(tmp_path); preview.start(); env.restore_failure=True; preview.stop()
    assert preview.state=='recovery_blocked' and 'conflicting' in preview.error
    assert preview.restoration is None


def test_unidentified_game_is_never_killed_and_restore_is_blocked(tmp_path):
    preview,env,_=setup(tmp_path); preview.start()
    def unknown(): raise DataError('identity changed')
    preview.game.force_close=unknown
    preview.stop()
    assert preview.state=='recovery_blocked' and env.closed==env.restored==0
    assert env.running and 'identity changed' in preview.error


def test_changed_demo_blocks_before_resource_backup_and_launch(tmp_path):
    preview,env,proof=setup(tmp_path); proof.path.write_bytes(b'externally changed')
    with pytest.raises(ReplayError,match='已变化'): preview.start()
    assert env.launched==env.deployed==env.restored==0
    assert not preview.session.exists()


def test_start_failure_after_deploy_restores_originals_and_propagates_error(tmp_path):
    preview,env,_=setup(tmp_path)
    def fail(*_,**__): raise OSError('Steam unavailable')
    preview.game.launch=fail
    with pytest.raises(OSError,match='Steam'): preview.start()
    assert preview.state=='complete' and env.restored==1 and env.closed==0


def test_double_start_and_double_confirmation_do_not_repeat_side_effects(tmp_path):
    preview,env,_=setup(tmp_path); preview.start()
    with pytest.raises(DataError): preview.start()
    preview.confirm_console()
    with pytest.raises(DataError): preview.confirm_console()
    assert env.launched==env.deployed==1 and env.sent==[]


@pytest.mark.parametrize('phase',['builder','checker'])
def test_early_preflight_failure_never_creates_an_orphan_transaction(tmp_path,phase):
    preview,env,_=setup(tmp_path)
    def fail(*_,**__): raise DataError('preflight failed')
    setattr(preview,phase,fail)
    with pytest.raises(DataError,match='preflight'): preview.start()
    assert not preview.session.exists() and pending_sessions(tmp_path/'sessions')==[]
    assert env.launched==env.deployed==env.closed==env.restored==0
    retry,_,_=setup(tmp_path)
    retry.start()
    assert retry.state=='awaiting_console'


def ready(preview,env,proof):
    load(preview,env,proof)
    for changes in ({},{'tick':12602,'server_tick':15934},
                    {'tick':12602,'server_tick':15934,'player_id':'76561199198478034'}):
        env.time+=1;env.proof=replace(proof,observed_at=env.time,**changes);preview.poll()
    assert preview.state=='ready'


def test_ready_game_exit_automatically_restores_without_sending_more_input(tmp_path):
    preview,env,proof=setup(tmp_path);ready(preview,env,proof);count=len(env.sent)
    env.running=False
    def exited(): raise DataError('owned game exited')
    preview.game.verify=exited
    with pytest.raises(DataError):preview.poll()
    assert preview.state=='complete' and env.restored==1 and env.closed==0 and len(env.sent)==count


def test_ready_identity_change_blocks_close_and_recovery(tmp_path):
    preview,env,proof=setup(tmp_path);ready(preview,env,proof)
    def changed():raise DataError('identity changed')
    preview.game.verify=changed;preview.game.force_close=changed
    with pytest.raises(DataError):preview.poll()
    assert preview.state=='recovery_blocked' and env.restored==env.closed==0


def test_ready_label_invalidates_on_changed_player_but_normal_app_focus_is_allowed(tmp_path):
    preview,env,proof=setup(tmp_path);ready(preview,env,proof);env.pid=99
    assert preview.poll()==env.proof and preview.state=='ready'  # Read-only monitoring.
    env.proof=replace(env.proof,player_id='76561199797409805')
    count=len(env.sent);assert preview.poll() is None
    assert preview.state=='preview_changed' and preview.controller.state=='unverified'
    env.proof=replace(env.proof,player_id='76561199198478034')
    assert preview.poll() is None and preview.state=='preview_changed' and len(env.sent)==count


def test_owned_game_exits_during_close_check_and_restoration_still_runs(tmp_path):
    preview,env,_=setup(tmp_path);preview.start()
    def already_exited():
        env.running=False
        raise DataError('owned process has just exited')
    preview.game.force_close=already_exited
    preview.stop()
    assert preview.state=='complete' and env.restored==1 and env.closed==0


class PlaybackStub:
    """Stateful transport stub: assertions exercise the session boundary."""
    instances = []

    def __init__(self, controller, session, draft, *, clock, checkpoint):
        self.controller = controller
        self.session = session
        self.draft = draft
        self.clock = clock
        self.checkpoint = checkpoint
        self.state = 'idle'
        self.triggered = False
        self.started_at = None
        self.binding_may_exist = False
        self.calls = []
        PlaybackStub.instances.append(self)

    def begin(self):
        assert self.controller.console.console_open_confirmed
        self.calls.append('begin')
        self.state = 'querying_empty'

    def poll(self):
        self.calls.append('poll')
        if self.state == 'querying_empty':
            self.binding_may_exist = True
            self.state = 'querying_installed'
        elif self.state == 'querying_installed':
            self.controller.console.console_open_confirmed = False
            self.state = 'ready'
        elif self.state == 'awaiting_foreground':
            self.started_at = self.clock()
            self.triggered = True
            self.state = 'playing'
        elif self.state == 'playing':
            self.state = 'ended'
        elif self.state == 'querying_unbound':
            self.binding_may_exist = False
            self.state = 'verified'
        return None

    def request_play(self):
        assert self.state == 'ready'
        self.calls.append('request_play')
        self.state = 'awaiting_foreground'

    def confirm_end_console(self):
        assert self.state == 'ended'
        self.calls.append('confirm_end_console')
        self.state = 'querying_unbound'


def test_hidden_playback_transport_is_serial_and_restorable(tmp_path):
    PlaybackStub.instances.clear()
    preview, env, proof = setup(tmp_path)
    ready(preview, env, proof)
    preview.playback_factory = PlaybackStub

    preview.prepare_playback()
    assert preview.state == 'awaiting_play_console' and preview.playback is None
    preview.confirm_console()
    preview.poll()
    playback = PlaybackStub.instances[-1]
    assert preview.state == 'arming_playback' and not playback.triggered

    preview.poll()
    assert preview.state == 'arming_playback' and not playback.triggered
    preview.poll()
    assert preview.state == 'play_ready' and not playback.triggered
    preview.play_clip()
    assert preview.state == 'awaiting_play_foreground' and not playback.triggered
    preview.poll()
    assert preview.state == 'playing' and playback.triggered
    preview.poll()
    assert preview.state == 'play_ended'

    env.proof=replace(env.proof,tick=13400,server_tick=16732,observed_at=env.time)

    preview.check_playback_binding()
    assert preview.state == 'awaiting_end_console'
    preview.confirm_console()
    preview.poll()
    assert preview.state == 'checking_binding'
    preview.poll()
    assert preview.state == 'play_verified' and not playback.binding_may_exist
    assert playback.calls == ['begin', 'poll', 'poll', 'request_play', 'poll',
                              'poll', 'confirm_end_console', 'poll']

    preview.stop()
    assert preview.state == 'complete' and env.restored == 1
    saved = json.loads((preview.session/'preview-state.json').read_text(encoding='utf-8'))
    assert saved['playback']['triggered'] is True
    assert saved['playback']['binding_may_exist'] is False


def test_hidden_playback_never_sends_key_before_second_user_action(tmp_path):
    PlaybackStub.instances.clear()
    preview, env, proof = setup(tmp_path)
    ready(preview, env, proof)
    preview.playback_factory = PlaybackStub
    preview.prepare_playback(); preview.confirm_console(); preview.poll(); preview.poll(); preview.poll()
    playback = PlaybackStub.instances[-1]
    assert preview.state == 'play_ready' and not playback.triggered
    with pytest.raises(ReplayError):
        preview.check_playback_binding()
    assert not playback.triggered and preview.state == 'play_ready'


def test_hidden_playback_transport_failure_closes_and_restores(tmp_path):
    class FailingPlayback(PlaybackStub):
        def poll(self):
            self.binding_may_exist = True
            raise ReplayError('binding readback failed')

    preview, env, proof = setup(tmp_path)
    ready(preview, env, proof)
    preview.playback_factory = FailingPlayback
    preview.prepare_playback(); preview.confirm_console(); preview.poll()
    with pytest.raises(ReplayError, match='binding readback failed'):
        preview.poll()
    assert preview.state == 'complete' and env.closed == env.restored == 1
    assert preview.error == 'binding readback failed'


def test_changed_ready_preview_cannot_be_reused_for_hidden_playback(tmp_path):
    preview, env, proof = setup(tmp_path)
    ready(preview, env, proof)
    env.proof = replace(env.proof, tick=12601)
    with pytest.raises(ReplayError):
        preview.prepare_playback()
    assert preview.state == 'preview_changed' and preview.controller.state == 'unverified'
    preview.stop()
    assert preview.state == 'complete' and env.restored == 1


def test_play_authorization_failure_with_possible_binding_restores_immediately(tmp_path):
    class FailingRequest(PlaybackStub):
        def request_play(self):
            self.binding_may_exist = True
            raise ReplayError('authorization failed')

    preview, env, proof = setup(tmp_path)
    ready(preview, env, proof)
    preview.playback = FailingRequest(preview.controller, preview.session, preview.draft,
                                      clock=preview.clock, checkpoint=preview._persist)
    preview.playback.state = 'ready'
    preview.state = 'play_ready'
    with pytest.raises(ReplayError, match='authorization failed'):
        preview.play_clip()
    assert preview.state == 'complete' and env.closed == env.restored == 1


CONSOLE_STEPS = ('awaiting_console', 'awaiting_demo_console',
                 'awaiting_play_console', 'awaiting_end_console')


def waiting_console_step(tmp_path, step):
    """Reach each real waiting transition; retain tokens already consumed."""
    preview, env, proof = setup(tmp_path)
    old_tokens = []
    preview.start()
    if step == 'awaiting_console':
        return preview, env, old_tokens
    old_tokens.append(preview.console_step_token)
    preview.confirm_console(step_token=preview.console_step_token)
    env.pid = 42
    preview.poll()
    env.time += 1
    env.snapshot = ReplaySnapshot(proof.process_identity, proof.path, True, 0,
                                  False, proof.player_id, True, env.time)
    preview.poll()
    if step == 'awaiting_demo_console':
        return preview, env, old_tokens
    old_tokens.append(preview.console_step_token)
    preview.confirm_console(step_token=preview.console_step_token)
    preview.poll()
    for changes in ({}, {'tick': 12602, 'server_tick': 15934},
                    {'tick': 12602, 'server_tick': 15934, 'player_id': '76561199198478034'}):
        env.time += 1
        env.proof = replace(proof, observed_at=env.time, **changes)
        preview.poll()
    assert preview.state == 'ready'
    preview.playback_factory = PlaybackStub
    preview.prepare_playback()
    if step == 'awaiting_play_console':
        return preview, env, old_tokens
    old_tokens.append(preview.console_step_token)
    preview.confirm_console(step_token=preview.console_step_token)
    preview.poll(); preview.poll(); preview.poll()
    preview.play_clip(); preview.poll(); preview.poll()
    assert preview.state == 'play_ended'
    env.proof=replace(env.proof,tick=13400,server_tick=16732,observed_at=env.time)
    preview.check_playback_binding()
    assert step == preview.state == 'awaiting_end_console'
    return preview, env, old_tokens


@pytest.mark.parametrize('step', CONSOLE_STEPS)
@pytest.mark.parametrize('after_deadline', [0, 1])
def test_late_console_confirmation_without_poll_cannot_refresh_authority(tmp_path, step, after_deadline):
    preview, env, _ = waiting_console_step(tmp_path, step)
    original_token = preview.console_step_token
    sent = list(env.sent)
    env.time = preview.deadline + after_deadline
    with pytest.raises(ReplayError, match='确认超时'):
        preview.confirm_console(step_token=original_token)
    assert env.sent == sent and env.closed == env.restored == 1
    assert preview.state == 'complete'
    assert not preview.console_confirmed and preview.console_step_token is None
    saved = json.loads((preview.session/'preview-state.json').read_text(encoding='utf-8'))
    assert saved['state'] == 'complete' and saved['console_step_token'] is None
    with pytest.raises(DataError):
        preview.confirm_console(step_token=original_token)
    assert env.sent == sent and env.closed == env.restored == 1


@pytest.mark.parametrize('step', CONSOLE_STEPS)
def test_current_console_step_accepts_fresh_confirmation_once_without_input(tmp_path, step):
    preview, env, _ = waiting_console_step(tmp_path, step)
    token = preview.console_step_token
    sent = list(env.sent)
    env.time = preview.deadline - .001
    preview.confirm_console(step_token=token)
    deadline = preview.deadline
    assert preview.console_confirmed and deadline == env.time + 30
    assert env.sent == sent and env.closed == env.restored == 0
    with pytest.raises(DataError, match='重复确认'):
        preview.confirm_console(step_token=token)
    assert preview.deadline == deadline and env.sent == sent
    preview.stop()


@pytest.mark.parametrize('step', CONSOLE_STEPS)
def test_old_step_and_foreign_session_tokens_cannot_authorize_current_console(tmp_path, step):
    preview, env, old_tokens = waiting_console_step(tmp_path, step)
    other_path = tmp_path/'other-session'
    other_path.mkdir()
    other, other_env, _ = setup(other_path)
    other.start()
    old_tokens.append(other.console_step_token)
    token = preview.console_step_token
    deadline = preview.deadline
    sent = list(env.sent)
    for stale in old_tokens:
        assert stale is not None and stale != token
        with pytest.raises(DataError, match='旧步骤或其他会话'):
            preview.confirm_console(step_token=stale)
        assert preview.deadline == deadline and not preview.console_confirmed
        assert env.sent == sent and env.closed == env.restored == 0
    preview.confirm_console(step_token=token)
    assert preview.console_confirmed and env.sent == sent
    preview.stop(); other.stop()
    assert env.restored == other_env.restored == 1


@pytest.mark.parametrize('phase', ['awaiting_play_console', 'awaiting_end_console'])
def test_console_wait_drains_real_periodic_log_without_input_or_stale_anchor(tmp_path, phase):
    from datetime import datetime
    from cs2pov.adapters.replay_log import ReplayLogReader
    preview, env, _ = waiting_console_step(tmp_path, phase)
    # End evidence occurs after playback started; engine source timestamps
    # have whole-second precision and must not predate that authorization.
    env.time+=1
    wall = datetime(2026, 10, 3, 17, 0, 0).timestamp()+.25
    origin = env.time
    reader = ReplayLogReader(preview.session,preview.game.verify(),preview.resource.nonce,
                            clock=lambda:env.time,wall=lambda:wall+env.time-origin)
    preview.console.reader=reader
    clip=preview.draft['selection']
    at_end=phase=='awaiting_end_console'
    tick=clip['end_tick'] if at_end else clip['start_tick']
    server=clip['server_end_tick'] if at_end else clip['server_start_tick']
    logfile=preview.session/'engine.log'
    def append(body):
        stamp=datetime.fromtimestamp(wall+env.time-origin).strftime('%m/%d %H:%M:%S')
        with logfile.open('a',encoding='utf-8') as stream:stream.write(stamp+' '+body+'\n')
    append(f'CGameRules - paused on tick {server}')
    sent=list(env.sent);env.pid=99
    for sequence in range(1,25):
        payload=dict(nonce=preview.resource.nonce,sequence=sequence,context='HudDemoController',
                     xuid=clip['player_id'],state=dict(sFileName=preview.draft['demo'],nTick=tick,
                     bIsPaused=True,nObserverMode=2,nSpectatingPlayerId=1,
                     bIsPlayingDemoFile=True,bIsPlayingBroadcast=False))
        append('[PanoramaScript] POV_READBACK '+json.dumps(payload,ensure_ascii=False))
        preview.poll()
        assert preview.state==phase and env.sent==sent and not preview.console_confirmed
        assert reader.readback().server_tick==server
        env.time+=.5
    # Twelve seconds waiting in the app exceeds the five-second freshness
    # limit. Each accepted proof still comes from a fresh report, not a cache.
    preview.confirm_console(step_token=preview.console_step_token)
    preview.poll();assert env.sent==sent  # Foreground is still the app.
    env.pid=42;preview.poll()
    assert preview.state==('arming_playback' if not at_end else 'checking_binding')
    assert env.closed==env.restored==0
    preview.stop();assert env.restored==1


@pytest.mark.parametrize('phase', ['awaiting_play_console', 'awaiting_end_console'])
@pytest.mark.parametrize('change', [{'player_id':'76561199797409805'}, {'tick':12500},
                                  {'observed_at':0.}, {'first_person':False}])
def test_console_wait_changed_or_stale_proof_restores_without_input(tmp_path,phase,change):
    preview,env,_=waiting_console_step(tmp_path,phase)
    sent=list(env.sent);env.pid=99
    env.proof=replace(env.proof,**change)
    with pytest.raises(ReplayError,match='回读'):preview.poll()
    assert preview.state=='complete' and env.sent==sent and env.closed==env.restored==1
