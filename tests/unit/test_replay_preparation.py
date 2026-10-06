from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogDecoder, ReplaySnapshot
from cs2pov.services.replay import ReplayController, ReplayEvidence, ReplayError
from cs2pov.services.replay_preparation import ReplayPreparation
from cs2pov.storage.settings import HudPreset


def setup(tmp_path):
    path = tmp_path/'sample.dem'; path.write_bytes(b'original sample')
    fp = fingerprint(path)
    analysis = dict(tick_rate=64, timeline=[[t,t+3332] for t in range(12500,13401)],
                    players=[dict(id='76561199198478034',name=' 一条小虾米OVO')])
    draft = dict(demo=str(path),fingerprint=fp,selection=dict(
        start_tick=12602,end_tick=13400,server_start_tick=15934,server_end_tick=16732,
        player_id='76561199198478034',content_sha256=fp['sha256'],hud=asdict(HudPreset())))
    identity = ProcessIdentity(612,456,'C:/game/cs2.exe')
    env = SimpleNamespace(time=100.,pid=612,proof=None,sent=[])
    game = SimpleNamespace(argv=['cs2.exe','-insecure'],verify=lambda:identity)
    console = SimpleNamespace(foreground_pid=lambda:env.pid,command=env.sent.append,readback=lambda:env.proof)
    controller = ReplayController(game,console,clock=lambda:env.time)
    prep = ReplayPreparation(controller,analysis,draft)
    proof = ReplayEvidence(identity,True,path,12538,15871,True,'76561199797409805',True,100.)
    return prep,env,proof,analysis,draft


def at(env,proof,**changes):
    env.time += 1
    env.proof = replace(proof,observed_at=env.time,**changes)


def test_seek_lead_in_exact_pause_then_select_target_without_racing(tmp_path):
    prep,env,proof,_,draft = setup(tmp_path)
    prep.begin()
    assert env.sent == ['demo_pause','demo_gototick 12538; demo_pause']
    assert prep.poll() is None and len(env.sent)==2  # No readback, no more input.
    at(env,proof)
    assert prep.poll() is None and prep.state=='starting'
    assert env.sent[-1]=='demo_resume; demo_pauseatservertick 15934'
    assert not any('spec_player' in c for c in env.sent)
    at(env,proof,tick=12602,server_tick=15935)
    assert prep.poll() is None and prep.state=='starting'  # One tick error still rejected.
    at(env,proof,tick=12602,server_tick=15934)
    assert prep.poll() is None and prep.state=='selecting'
    assert 'spec_mode 2' in env.sent and 'spec_mode 1' not in env.sent
    assert 'spec_player " 一条小虾米OVO"' in env.sent
    count = len(env.sent)
    at(env,proof,tick=12602,server_tick=15934)
    assert prep.poll() is None and len(env.sent)==count  # Wait for actual identity change.
    at(env,proof,tick=12602,server_tick=15934,player_id=draft['selection']['player_id'],first_person=False)
    assert prep.poll() is None
    at(env,proof,tick=12602,server_tick=15934,player_id=draft['selection']['player_id'])
    assert prep.poll()==env.proof and prep.state=='ready'
    assert prep.controller.state=='preview_verified'
    env.proof = replace(env.proof,first_person=False)
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state=='failed' and prep.controller.state=='unverified'


@pytest.mark.parametrize('changes',[dict(tick=12539),dict(path=Path('C:/other.dem')),
    dict(local_demo=False),dict(paused=False),dict(observed_at=99.),dict(observed_at=101.),
    dict(process_identity=ProcessIdentity(612,457,'C:/game/cs2.exe'))])
def test_incomplete_or_wrong_seek_proof_never_starts_playback(tmp_path,changes):
    prep,env,proof,_,_ = setup(tmp_path); prep.begin()
    env.proof = replace(proof,**changes)
    assert prep.poll() is None and len(env.sent)==2
    env.time += 20
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert prep.state=='failed' and len(env.sent)==2


def test_focus_loss_never_retries_or_uses_stale_ready_state(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path); prep.begin()
    at(env,proof); env.pid=999
    with pytest.raises(ReplayError,match='前台'): prep.poll()
    assert len(env.sent)==2 and prep.state=='failed'
    env.pid=612
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.sent)==2


def test_partial_input_failure_and_cancel_do_not_retry(tmp_path):
    prep,env,_,_,_ = setup(tmp_path)
    def fail(command):
        env.sent.append(command)
        raise ReplayError('input outcome unknown')
    prep.controller.console.command=fail
    with pytest.raises(ReplayError,match='unknown'): prep.begin()
    assert len(env.sent)==1 and prep.state=='failed'
    prep.cancel()
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.sent)==1


@pytest.mark.parametrize('mutation',[
    lambda p: p.analysis.update(tick_rate=0),
    lambda p: p.analysis.update(timeline=[[12602,15934],[13400,16732]]),
    lambda p: p.analysis['timeline'].reverse(),
    lambda p: p.draft['selection'].update(server_start_tick=15935),
    lambda p: p.draft['selection'].update(server_end_tick=16733),
    lambda p: p.draft['selection'].update(end_tick=12601),
    lambda p: Path(p.draft['demo']).write_bytes(b'externally modified'),
])
def test_bad_source_or_time_mapping_blocks_before_any_input(tmp_path,mutation):
    prep,env,_,_,_ = setup(tmp_path); mutation(prep)
    with pytest.raises(ReplayError): prep.begin()
    assert env.sent==[] and prep.state=='failed'


def test_snapshots_draft_and_prevents_double_start(tmp_path):
    prep,env,_,analysis,draft = setup(tmp_path)
    draft['selection']['start_tick']=13000; analysis['timeline'].clear()
    prep.begin()
    assert env.sent[-1]=='demo_gototick 12538; demo_pause'
    with pytest.raises(ReplayError): prep.begin()
    assert len(env.sent)==2


def test_seek_at_or_after_target_server_tick_is_a_hard_failure(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path); prep.begin()
    at(env,proof,server_tick=15934)
    with pytest.raises(ReplayError,match='起点之前'): prep.poll()
    assert len(env.sent)==2 and prep.state=='failed'


def snapshot(proof, **changes):
    value = ReplaySnapshot(proof.process_identity, proof.path, proof.local_demo,
        proof.tick, proof.paused, proof.player_id, proof.first_person, proof.observed_at)
    return replace(value, **changes)


def test_paused_seek_snapshot_without_new_server_pause_advances_only_preroll(tmp_path):
    prep,env,proof,_,draft = setup(tmp_path)
    nonce = 'PREROLL_SESSION_0001'
    env.wall = datetime(2026,10,4,11,44,45).timestamp()
    decoder = ReplayLogDecoder(proof.process_identity,nonce,
        clock=lambda:env.time,wall=lambda:env.wall)
    prep.controller.console.readback = decoder.readback
    prep.controller.console.snapshot = decoder.snapshot

    def emit(body):
        stamp = datetime.fromtimestamp(env.wall).strftime('%m/%d %H:%M:%S')
        decoder.consume(f'{stamp} {body}')

    def report(sequence, tick):
        emit('[PanoramaScript] POV_READBACK '+json.dumps(dict(
            nonce=nonce,sequence=sequence,context='HudDemoController',
            state=dict(sFileName=draft['demo'],nTick=tick,bIsPaused=True,
                nObserverMode=2,nSpectatingPlayerId=1288,
                bIsPlayingDemoFile=True,bIsPlayingBroadcast=False),
            xuid=draft['selection']['player_id'])))

    emit('CGameRules - paused on tick 17000')
    report(1,13668)
    assert decoder.readback().server_tick == 17000
    prep.begin()
    emit('[Demo] Demo Skipping to tick 12538...')
    env.time += 1; env.wall += 1
    report(2,prep.preroll_tick)
    assert decoder.readback() is None and decoder.anchor is None
    assert decoder.snapshot().paused and decoder.snapshot().tick == prep.preroll_tick
    assert prep.poll() is None and prep.state == 'starting'
    assert env.sent == ['demo_pause','demo_gototick 12538; demo_pause',
                        'demo_timescale 1','demo_resume; demo_pauseatservertick 15934']
    assert decoder.readback() is None and decoder.anchor is None
    assert prep.controller.state == 'unverified'

    # A perfectly matching Panorama start still cannot supply a server clock.
    env.time += 1; env.wall += 1
    report(3,draft['selection']['start_tick'])
    assert decoder.snapshot().player_id == draft['selection']['player_id']
    assert prep.poll() is None and prep.state == 'starting' and len(env.sent) == 4
    assert decoder.readback() is None and decoder.anchor is None
    assert prep.deadline == 121  # Original per-stage timeout is unchanged.
    env.time = prep.deadline
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == 4


@pytest.mark.parametrize('changes',[
    dict(tick=12539),dict(path=Path('C:/other.dem')),dict(local_demo=False),
    dict(paused=False),dict(observed_at=99.),dict(observed_at=101.),
    dict(process_identity=ProcessIdentity(612,457,'C:/game/cs2.exe'))])
def test_invalid_preroll_snapshot_never_starts_playback(tmp_path,changes):
    prep,env,proof,_,_ = setup(tmp_path)
    prep.controller.console.snapshot = lambda:snapshot(proof,**changes)
    prep.begin()
    assert prep.poll() is None and prep.state == 'seeking' and len(env.sent) == 2
    env.time = prep.deadline
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == 2


def test_stale_preroll_snapshot_cannot_extend_deadline_or_send_input(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path)
    prep.controller.console.snapshot = lambda:snapshot(proof)
    prep.begin(); env.time += 6
    assert prep.poll() is None and prep.state == 'seeking' and len(env.sent) == 2
    assert prep.deadline == 120
    env.time = prep.deadline
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert len(env.sent) == 2


def test_wrong_session_nonce_cannot_supply_preroll_snapshot(tmp_path):
    prep,env,proof,_,draft = setup(tmp_path)
    wall = datetime(2026,10,4,11,44,46).timestamp()
    decoder = ReplayLogDecoder(proof.process_identity,'PREROLL_SESSION_0001',
        clock=lambda:env.time,wall=lambda:wall)
    prep.controller.console.readback = decoder.readback
    prep.controller.console.snapshot = decoder.snapshot
    prep.begin()
    decoder.consume('10/04 11:44:46 [PanoramaScript] POV_READBACK '+json.dumps(dict(
        nonce='OTHER_SESSION_00001',sequence=1,context='HudDemoController',
        state=dict(sFileName=draft['demo'],nTick=prep.preroll_tick,bIsPaused=True,
            nObserverMode=2,nSpectatingPlayerId=1288,
            bIsPlayingDemoFile=True,bIsPlayingBroadcast=False),
        xuid=draft['selection']['player_id'])))
    assert decoder.readback() is None and decoder.snapshot() is None
    assert prep.poll() is None and prep.state == 'seeking' and len(env.sent) == 2


def test_available_full_preroll_proof_cannot_be_bypassed_by_snapshot(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path)
    def unexpected_snapshot():
        pytest.fail('Available server proof must not be replaced with a snapshot')
    prep.controller.console.snapshot = unexpected_snapshot
    prep.begin(); at(env,proof,server_tick=15934)
    with pytest.raises(ReplayError,match='起点之前'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == 2


def test_full_server_proof_arriving_during_snapshot_read_keeps_boundary_gate(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path)
    def refreshed_snapshot():
        # ReplayLogReader.snapshot() pulls new log lines before returning.
        env.proof = replace(proof,server_tick=15934)
        return snapshot(proof)
    prep.controller.console.snapshot = refreshed_snapshot
    prep.begin()
    with pytest.raises(ReplayError,match='起点之前'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == 2


@pytest.mark.parametrize('changes',[dict(tick=12539),dict(path=Path('C:/other.dem')),
    dict(observed_at=99.),dict(paused=False),dict(local_demo=False),
    dict(process_identity=ProcessIdentity(612,457,'C:/game/cs2.exe'))])
def test_invalid_proof_arriving_during_snapshot_read_is_not_masked(tmp_path,changes):
    prep,env,proof,_,_ = setup(tmp_path)
    def refreshed_snapshot():
        env.proof = replace(proof,**changes)
        return snapshot(proof)
    prep.controller.console.snapshot = refreshed_snapshot
    prep.begin()
    assert prep.poll() is None and prep.state == 'seeking' and len(env.sent) == 2


def test_valid_proof_arriving_during_snapshot_read_starts_preroll_once(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path)
    def refreshed_snapshot():
        env.proof = proof
        return snapshot(proof)
    prep.controller.console.snapshot = refreshed_snapshot
    prep.begin()
    assert prep.poll() is None and prep.state == 'starting'
    assert env.sent[-2:] == ['demo_timescale 1','demo_resume; demo_pauseatservertick 15934']
    assert prep.poll() is None and prep.state == 'starting' and len(env.sent) == 4


@pytest.mark.parametrize('changes',[dict(tick=12539),dict(observed_at=99.),
    dict(path=Path('C:/other.dem')),dict(local_demo=False),dict(paused=False),
    dict(process_identity=ProcessIdentity(612,457,'C:/game/cs2.exe'))])
def test_invalid_available_proof_is_not_masked_by_valid_preroll_snapshot(tmp_path,changes):
    prep,env,proof,_,_ = setup(tmp_path)
    prep.controller.console.snapshot = lambda:snapshot(proof)
    prep.begin(); env.proof = replace(proof,**changes)
    assert prep.poll() is None and prep.state == 'seeking' and len(env.sent) == 2


def test_snapshot_fallback_never_replaces_exact_start_player_or_ready_proof(tmp_path):
    prep,env,proof,_,draft = setup(tmp_path)
    env.snapshot = snapshot(proof)
    prep.controller.console.snapshot = lambda:env.snapshot
    prep.begin()
    assert prep.poll() is None and prep.state == 'starting'
    count = len(env.sent)
    env.time += 1
    env.snapshot = snapshot(proof,tick=12602,player_id=draft['selection']['player_id'],
                            observed_at=env.time)
    assert prep.poll() is None and prep.state == 'starting' and len(env.sent) == count
    assert prep.controller.state == 'unverified'
    at(env,proof,tick=12602,server_tick=15935)
    assert prep.poll() is None and prep.state == 'starting' and len(env.sent) == count
    at(env,proof,tick=12602,server_tick=15934)
    assert prep.poll() is None and prep.state == 'selecting'
    count = len(env.sent)
    env.proof = None
    env.snapshot = replace(env.snapshot,observed_at=env.time)
    assert prep.poll() is None and prep.state == 'selecting' and len(env.sent) == count
    at(env,proof,tick=12602,server_tick=15934)
    assert prep.poll() is None and prep.state == 'selecting'
    at(env,proof,tick=12602,server_tick=15934,
       player_id=draft['selection']['player_id'],first_person=False)
    assert prep.poll() is None and prep.state == 'selecting'
    at(env,proof,tick=12602,server_tick=15934,player_id=draft['selection']['player_id'])
    assert prep.poll() == env.proof and prep.state == 'ready'
    env.proof = None
    env.snapshot = replace(env.snapshot,observed_at=env.time)
    with pytest.raises(ReplayError,match='回读'): prep.poll()
    assert prep.state == 'failed' and prep.controller.state == 'unverified'


def test_preroll_resume_input_failure_never_retries_snapshot_transition(tmp_path):
    prep,env,proof,_,_ = setup(tmp_path)
    prep.controller.console.snapshot = lambda:snapshot(proof)
    prep.begin()
    def fail_resume(command):
        env.sent.append(command)
        if command.startswith('demo_resume'):
            raise ReplayError('input outcome unknown')
    prep.controller.console.command = fail_resume
    with pytest.raises(ReplayError,match='unknown'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == 4
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.sent) == 4


def waiting_input_stage(tmp_path, stage, *, use_snapshot=False):
    prep,env,proof,_,_ = setup(tmp_path)
    prep.begin()
    if stage == 'starting':
        at(env,proof)
        assert prep.poll() is None and prep.state == 'starting'
        at(env,proof,tick=12602,server_tick=15934)
    else:
        at(env,proof)
    env.snapshot = snapshot(env.proof) if use_snapshot else None
    if use_snapshot:
        env.proof = None
        prep.controller.console.snapshot = lambda:env.snapshot
    env.input_ready = False
    env.input_checks = env.readbacks = 0
    def readback():
        env.readbacks += 1
        return env.proof
    def input_ready():
        env.input_checks += 1
        return env.input_ready
    prep.controller.console.readback = readback
    prep.controller.console.input_ready = input_ready
    return prep,env,proof


@pytest.mark.parametrize('stage,use_snapshot',[
    ('seeking',False),('seeking',True),('starting',False)])
def test_held_keys_wait_with_current_readback_then_send_stage_once(tmp_path,stage,use_snapshot):
    prep,env,_ = waiting_input_stage(tmp_path,stage,use_snapshot=use_snapshot)
    sent = list(env.sent)
    since,deadline = prep.since,prep.deadline
    for _ in range(2):
        env.time += 1
        readbacks = env.readbacks
        assert prep.poll() is None and prep.state == stage
        assert env.sent == sent and (prep.since,prep.deadline) == (since,deadline)
        assert env.readbacks > readbacks  # Waiting still consumes current log evidence.
    assert env.input_checks == 2
    env.input_ready = True
    env.time += 1
    if use_snapshot:
        env.snapshot = replace(env.snapshot,observed_at=env.time)
    else:
        env.proof = replace(env.proof,observed_at=env.time)
    assert prep.poll() is None
    if stage == 'seeking':
        assert prep.state == 'starting'
        assert env.sent == sent + ['demo_timescale 1','demo_resume; demo_pauseatservertick 15934']
    else:
        assert prep.state == 'selecting' and env.sent == sent + prep.commands
    assert env.input_checks == 3
    sent = list(env.sent)
    assert prep.poll() is None and env.sent == sent and env.input_checks == 3


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_held_keys_do_not_extend_stage_deadline(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    since,deadline = prep.since,prep.deadline
    assert prep.poll() is None and prep.state == stage
    assert (prep.since,prep.deadline) == (since,deadline) and env.sent == sent
    env.time = deadline
    def unexpected_check():
        pytest.fail('Expired preparation must fail before input readiness or readback')
    prep.controller.console.input_ready = unexpected_check
    prep.controller.console.readback = unexpected_check
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert prep.state == 'failed' and prep.controller.state == 'unverified'
    assert env.sent == sent and (prep.since,prep.deadline) == (since,deadline)


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_input_readiness_cannot_cross_original_deadline_and_send(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    since,deadline = prep.since,prep.deadline
    def delayed_readiness():
        env.time = deadline
        return True
    prep.controller.console.input_ready = delayed_readiness
    with pytest.raises(ReplayError,match='超时'): prep.poll()
    assert prep.state == 'failed' and prep.controller.state == 'unverified'
    assert env.sent == sent and (prep.since,prep.deadline) == (since,deadline)


@pytest.mark.parametrize('stage,use_snapshot',[
    ('seeking',False),('seeking',True),('starting',False)])
def test_input_readiness_delay_cannot_authorize_proof_that_became_stale(tmp_path,stage,use_snapshot):
    prep,env,_ = waiting_input_stage(tmp_path,stage,use_snapshot=use_snapshot)
    sent = list(env.sent)
    since,deadline = prep.since,prep.deadline
    def delayed_readiness():
        env.time += 6
        return True
    prep.controller.console.input_ready = delayed_readiness
    assert prep.poll() is None and prep.state == stage and env.sent == sent
    assert (prep.since,prep.deadline) == (since,deadline)
    prep.controller.console.input_ready = lambda:True
    if use_snapshot:
        env.snapshot = replace(env.snapshot,observed_at=env.time)
    else:
        env.proof = replace(env.proof,observed_at=env.time)
    assert prep.poll() is None and prep.state != stage and len(env.sent) > len(sent)


@pytest.mark.parametrize('stage',['seeking','starting'])
@pytest.mark.parametrize('changes',[
    dict(tick=12539),dict(path=Path('C:/other.dem')),
    dict(local_demo=False),dict(paused=False),dict(observed_at=99.),
    dict(observed_at=999.),
    dict(process_identity=ProcessIdentity(612,457,'C:/game/cs2.exe'))])
def test_key_release_revalidates_current_proof_before_any_stage_input(tmp_path,stage,changes):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    since,deadline = prep.since,prep.deadline
    assert prep.poll() is None and prep.state == stage and env.sent == sent
    env.input_ready = True
    env.time += 1
    env.proof = replace(env.proof,**changes)
    assert prep.poll() is None and prep.state == stage and env.sent == sent
    assert (prep.since,prep.deadline) == (since,deadline)


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_key_release_rechecks_server_tick_boundary(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    assert prep.poll() is None and prep.state == stage
    env.input_ready = True
    env.time += 1
    env.proof = replace(env.proof,observed_at=env.time,server_tick=15935)
    if stage == 'seeking':
        with pytest.raises(ReplayError,match='起点之前'): prep.poll()
        assert prep.state == 'failed'
    else:
        assert prep.poll() is None and prep.state == stage
    assert env.sent == sent


@pytest.mark.parametrize('changes',[dict(tick=12539),dict(observed_at=99.),dict(paused=False)])
def test_key_release_revalidates_current_preroll_snapshot(tmp_path,changes):
    prep,env,_ = waiting_input_stage(tmp_path,'seeking',use_snapshot=True)
    sent = list(env.sent)
    assert prep.poll() is None and prep.state == 'seeking'
    env.input_ready = True
    env.time += 1
    env.snapshot = replace(env.snapshot,**changes)
    assert prep.poll() is None and prep.state == 'seeking' and env.sent == sent


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_key_release_cannot_use_proof_that_became_stale_while_waiting(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    assert prep.poll() is None and prep.state == stage
    env.input_ready = True
    env.time += 6
    assert prep.poll() is None and prep.state == stage and env.sent == sent
    env.proof = replace(env.proof,observed_at=env.time)
    assert prep.poll() is None and prep.state != stage and len(env.sent) > len(sent)


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_input_readiness_exception_fails_without_sending_or_retrying(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    def fail_readiness():
        raise ReplayError('input readiness unavailable')
    prep.controller.console.input_ready = fail_readiness
    with pytest.raises(ReplayError,match='readiness unavailable'): prep.poll()
    assert prep.state == 'failed' and prep.controller.state == 'unverified'
    assert env.sent == sent
    prep.controller.console.input_ready = lambda:True
    with pytest.raises(ReplayError): prep.poll()
    assert env.sent == sent


@pytest.mark.parametrize('stage',['seeking','starting'])
@pytest.mark.parametrize('guard',['foreground','insecure','identity'])
def test_held_keys_still_enforce_controller_guards(tmp_path,stage,guard):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    sent = list(env.sent)
    if guard == 'foreground':
        env.pid = 999
    elif guard == 'insecure':
        prep.controller.game.argv = ['cs2.exe']
    else:
        def failed_identity():
            raise ReplayError('process identity changed')
        prep.controller.game.verify = failed_identity
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and env.sent == sent and env.input_checks == 0


@pytest.mark.parametrize('stage',['seeking','starting'])
def test_stage_send_partial_failure_after_key_release_is_never_retried(tmp_path,stage):
    prep,env,_ = waiting_input_stage(tmp_path,stage)
    env.input_ready = True
    sent = list(env.sent)
    def fail_second_command(command):
        env.sent.append(command)
        if len(env.sent) == len(sent) + 2:
            raise ReplayError('input outcome unknown')
    prep.controller.console.command = fail_second_command
    with pytest.raises(ReplayError,match='input outcome unknown'): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == len(sent) + 2
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.sent) == len(sent) + 2


def pipe_preparation(tmp_path):
    """Real pipe ledger/dispatch, injected OS writes and actual replay guards."""
    from tests.unit.test_pipe_console import setup as pipe_setup, ready as pipe_ready
    _native, _unused, proof, analysis, draft = setup(tmp_path)
    analysis['timeline'] = [[tick,tick+3332] for tick in range(12400,13401)]
    console, env, identity = pipe_setup(tmp_path)
    pipe_ready(console, env, identity)
    # Pipe preparation now starts three seconds before 12602. The exact cut
    # and all negative proof/ledger assertions below are unchanged.
    env.replay_proof = replace(proof, process_identity=identity,tick=12410,server_tick=15742)
    console.reader = SimpleNamespace(readback=lambda: env.replay_proof,
        snapshot=lambda: None if env.replay_proof is None else snapshot(env.replay_proof))
    controller = ReplayController(console.game, console, clock=lambda: env.time)
    return ReplayPreparation(controller, analysis, draft), env, console


def pipe_starting(tmp_path):
    prep, env, console = pipe_preparation(tmp_path)
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    env.time += 1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    assert prep.poll() is None and prep.state == 'resuming'
    deadline = prep.deadline
    env.time += .5
    env.replay_proof = replace(env.replay_proof, tick=12420, paused=False,
                               observed_at=env.time)
    assert prep.poll() is None and prep.state == 'starting'
    assert prep.deadline == deadline and env.writes[-1] == b'demo_pauseatservertick 15934\n'
    env.time += .5
    env.replay_proof = replace(env.replay_proof, tick=12602, server_tick=15934,
                               paused=True, observed_at=env.time)
    return prep, env, console


@pytest.mark.parametrize('after', [1, 3])
@pytest.mark.parametrize('change', [dict(path=Path('C:/different.dem')), dict(local_demo=False),
    dict(paused=False), dict(tick=12603), dict(server_tick=15935),
    dict(observed_at=99.), dict(observed_at=999.)])
def test_selection_rechecks_exact_start_after_every_pipe_ledger(tmp_path, after, change):
    prep, env, console = pipe_starting(tmp_path)
    original = console.persist
    target = console.sequence + after
    before = len(env.writes)
    def mutate(path, payload):
        original(path, payload)
        if console.sequence == target:
            env.replay_proof = replace(env.replay_proof, **change)
    console.persist = mutate
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and prep.controller.state == 'unverified'
    assert console.state == 'failed' and len(env.writes) == before + after - 1
    assert json.loads(console.ledger.read_bytes())['sequence'] == target
    assert json.loads(console.ledger.read_bytes())['consumed'] is True
    env.replay_proof = replace(env.replay_proof, path=Path(prep.draft['demo']),
                               paused=True, tick=12602, server_tick=15934, observed_at=env.time)
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.writes) == before + after - 1  # No retry after consumed input failed.


def test_selection_short_individual_writes_cannot_spend_stale_batch_proof(tmp_path):
    prep, env, console = pipe_starting(tmp_path)
    persist, write = console.persist, console.pipes.backend.write
    before = len(env.writes)
    def delayed_persist(*args):
        persist(*args)
        env.time += .49
    def delayed_write(*args, **kwargs):
        count = write(*args, **kwargs)
        env.time += .49
        return count
    console.persist, console.pipes.backend.write = delayed_persist, delayed_write
    with pytest.raises(ReplayError, match='回读'): prep.poll()
    assert prep.state == 'failed' and console.state == 'failed'
    assert len(env.writes) == before + 5  # Sixth durable attempt crosses the 5-second proof age.
    assert env.time - env.replay_proof.observed_at > 5
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.writes) == before + 5


def test_selection_input_does_not_reset_original_starting_deadline(tmp_path):
    prep, env, console = pipe_starting(tmp_path)
    deadline = prep.deadline
    env.time = deadline - .1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    persist = console.persist
    before = len(env.writes)
    def slow_persist(*args):
        persist(*args)
        env.time += .49
        env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    console.persist = slow_persist
    with pytest.raises(ReplayError, match='超时'): prep.poll()
    assert env.time >= deadline and len(env.writes) == before
    assert prep.state == 'failed' and console.state == 'failed'


def test_selection_accepts_fresh_start_from_starting_window_before_player_arrives(tmp_path):
    prep, env, console = pipe_starting(tmp_path)
    env.replay_proof = replace(env.replay_proof, observed_at=env.time-.5,
                               player_id='a different player', first_person=False)
    before = len(env.writes)
    assert prep.poll() is None and prep.state == 'selecting'
    assert len(env.writes) == before + len(prep.commands)
    env.time += 1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time,
                               player_id=prep.draft['selection']['player_id'], first_person=True)
    assert prep.poll() == env.replay_proof and prep.state == 'ready'


@pytest.mark.parametrize('change', [dict(path=Path('C:/different.dem')), dict(paused=False),
                                   dict(tick=12603), dict(server_tick=15935)])
def test_native_selection_rechecks_current_scope_between_commands(tmp_path, change):
    prep, env, proof, _analysis, _draft = setup(tmp_path)
    prep.begin(); at(env, proof); prep.poll()
    at(env, proof, tick=12602, server_tick=15934)
    before = len(env.sent)
    def mutate(command):
        env.sent.append(command)
        env.proof = replace(env.proof, **change)
    prep.controller.console.command = mutate
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and len(env.sent) == before + 1
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.sent) == before + 1


@pytest.mark.parametrize('change', [dict(path=Path('C:/different.dem')), dict(paused=False),
                                   dict(tick=12539), dict(server_tick=15934)])
def test_preroll_resume_rechecks_scope_after_its_own_pipe_ledger(tmp_path, change):
    prep, env, console = pipe_preparation(tmp_path)
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    env.time += 1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    persist = console.persist
    target = console.sequence + 2  # demo_timescale first, then resume/pauseatserver.
    before = len(env.writes)
    def mutate(path, payload):
        persist(path, payload)
        if console.sequence == target:
            env.replay_proof = replace(env.replay_proof, **change)
    console.persist = mutate
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and console.state == 'failed'
    assert len(env.writes) == before + 1 and env.writes[-1] == b'demo_timescale 1\n'
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.writes) == before + 1


@pytest.mark.parametrize('after', [1, 2])
@pytest.mark.parametrize('change', [dict(path=Path('C:/different.dem')), dict(local_demo=False),
                                   dict(observed_at=94.99)])
def test_initial_pipe_pause_and_seek_recheck_loaded_demo_after_ledger(tmp_path, after, change):
    prep, env, console = pipe_preparation(tmp_path)
    persist = console.persist
    target = console.sequence + after
    before = len(env.writes)
    def mutate(path, payload):
        persist(path, payload)
        if console.sequence == target:
            env.replay_proof = replace(env.replay_proof, **change)
    console.persist = mutate
    with pytest.raises(ReplayError): prep.begin(loading_since=100., loading_deadline=120.)
    assert prep.state == 'failed' and console.state == 'failed'
    assert len(env.writes) == before + after - 1
    assert json.loads(console.ledger.read_bytes())['sequence'] == target
    with pytest.raises(ReplayError): prep.begin(loading_since=100., loading_deadline=120.)
    assert len(env.writes) == before + after - 1


def test_initial_pipe_input_requires_original_loading_window(tmp_path):
    prep, env, _console = pipe_preparation(tmp_path)
    before = len(env.writes)
    with pytest.raises(ReplayError, match='载入'): prep.begin()
    assert prep.state == 'failed' and len(env.writes) == before


def test_initial_loaded_demo_may_be_unpaused_until_owned_pause_observed(tmp_path):
    prep, env, console = pipe_preparation(tmp_path)
    env.replay_proof = replace(env.replay_proof, paused=False, tick=10)
    write = console.pipes.backend.write
    def observe_owned_pause(handle, payload, **kwargs):
        count = write(handle, payload, **kwargs)
        if payload == b'demo_pause\n':
            env.time += .1
            env.replay_proof = replace(env.replay_proof, paused=True, tick=12, observed_at=env.time)
        return count
    console.pipes.backend.write = observe_owned_pause
    prep.begin(loading_since=100., loading_deadline=120.)
    assert prep.state == 'seeking'
    assert env.writes[-2:] == [b'demo_pause\n', b'demo_gototick 12410; demo_pause\n']


@pytest.mark.parametrize('change', [dict(paused=False), dict(tick=12539)])
def test_initial_already_paused_position_cannot_change_before_seek_ledger(tmp_path, change):
    prep, env, console = pipe_preparation(tmp_path)
    persist = console.persist
    target = console.sequence + 2
    before = len(env.writes)
    def mutate(path, payload):
        persist(path, payload)
        if console.sequence == target:
            env.replay_proof = replace(env.replay_proof, **change)
    console.persist = mutate
    with pytest.raises(ReplayError, match='暂停位置'): prep.begin(loading_since=100., loading_deadline=120.)
    assert prep.state == 'failed' and console.state == 'failed'
    assert len(env.writes) == before + 1 and env.writes[-1] == b'demo_pause\n'


@pytest.mark.parametrize('after', [1, 2])
def test_initial_pipe_ledger_cannot_extend_original_loading_deadline(tmp_path, after):
    prep, env, console = pipe_preparation(tmp_path)
    persist = console.persist
    target = console.sequence + after
    before = len(env.writes)
    def cross_deadline(path, payload):
        persist(path, payload)
        if console.sequence == target:
            env.time += .49
            env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    console.persist = cross_deadline
    with pytest.raises(ReplayError, match='超时'): prep.begin(loading_since=100., loading_deadline=100.1)
    assert prep.state == 'failed' and console.state == 'failed'
    assert len(env.writes) == before + after - 1


def test_selection_preserves_inclusive_five_second_freshness_boundary(tmp_path):
    prep, env, _console = pipe_starting(tmp_path)
    env.time = env.replay_proof.observed_at + 5
    before = len(env.writes)
    assert prep.poll() is None and prep.state == 'selecting'
    assert len(env.writes) == before + len(prep.commands)


def test_selection_final_game_verify_cannot_write_after_original_phase_deadline(tmp_path):
    from cs2pov.storage.settings import DataError
    prep, env, console = pipe_starting(tmp_path)
    original_deadline = prep.deadline
    env.time = original_deadline - .1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    before = len(env.writes)
    phase_input = prep._phase_input
    verify = console.game.verify
    write = console.pipes.backend.write
    env.guard_returned = False
    env.delayed_final_verify = False
    env.write_times = []

    def observe_guard_return(*args, **kwargs):
        # Run every real phase/readback check before marking the final handoff.
        result = phase_input(*args, **kwargs)
        env.guard_returned = True
        return result

    def delayed_verify():
        if env.guard_returned and not env.delayed_final_verify:
            # This is PipeConsole._send's actual _target() after input_guard.
            env.time += .2
            env.delayed_final_verify = True
        # Keep identity and proof fresh: only the original phase limit is lost.
        env.replay_proof = replace(env.replay_proof, observed_at=env.time)
        return verify()

    def timed_write(*args, **kwargs):
        env.write_times.append(env.time)
        return write(*args, **kwargs)

    prep._phase_input = observe_guard_return
    console.game.verify = delayed_verify
    console.pipes.backend.write = timed_write
    with pytest.raises(DataError): prep.poll()
    assert env.delayed_final_verify and env.time >= original_deadline
    assert len(env.writes) == before, f'Late writes after phase {original_deadline}: {env.write_times}'
    assert env.write_times == []
    assert prep.state == 'failed' and prep.controller.state == 'unverified'
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.writes) == before
