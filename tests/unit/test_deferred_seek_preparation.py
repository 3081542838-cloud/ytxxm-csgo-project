"""Current engine seek completion, using real pipe ledgers and replay decoding."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.replay_log import ReplayLogDecoder
from cs2pov.services.replay import ReplayController, ReplayError
from cs2pov.services.replay_preparation import ReplayPreparation
from tests.unit.test_pipe_console import setup as pipe_setup, ready as pipe_ready
from tests.unit.test_replay_preparation import setup as original_setup


def setup(tmp_path):
    _, _, _, analysis, draft = original_setup(tmp_path)
    analysis['timeline'] = [[tick, tick+3332] for tick in range(12200, 13401)]
    console, env, identity = pipe_setup(tmp_path)
    pipe_ready(console, env, identity)
    env.wall = datetime(2026, 10, 6, 15, 0, 0).timestamp()
    decoder = ReplayLogDecoder(identity, 'DEFERRED_SEEK_0001',
        clock=lambda: env.time, wall=lambda: env.wall)
    console.reader = SimpleNamespace(decoder=decoder, readback=decoder.readback,
                                    snapshot=decoder.snapshot)
    controller = ReplayController(console.game, console, clock=lambda: env.time)
    prep = ReplayPreparation(controller, analysis, draft, deferred_seek_pause=True)
    env.sequence = 0

    def emit(body):
        stamp = datetime.fromtimestamp(env.wall).strftime('%m/%d %H:%M:%S')
        decoder.consume(f'{stamp} {body}')

    def report(tick, *, paused=False, server=None, nonce=None, path=None,
               local=True, player=None, first_person=True):
        env.sequence += 1
        if server is not None:
            emit(f'CGameRules - paused on tick {server}')
        emit('[PanoramaScript] POV_READBACK '+json.dumps(dict(
            nonce=nonce or decoder.nonce, sequence=env.sequence, context='HudDemoController',
            state=dict(sFileName=path or draft['demo'], nTick=tick, bIsPaused=paused,
                nObserverMode=2 if first_person else 3, nSpectatingPlayerId=1288,
                bIsPlayingDemoFile=local, bIsPlayingBroadcast=False),
            xuid=player or draft['selection']['player_id'])))

    def advance(seconds=1):
        env.time += seconds
        env.wall += seconds

    report(13400, paused=True, server=16732)
    return prep, env, console, decoder, emit, report, advance


def settling(tmp_path):
    prep, env, console, decoder, emit, report, advance = setup(tmp_path)
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    emit(f'[Demo] Demo Skipping to tick {prep.preroll_tick}...')
    advance()
    return prep, env, console, decoder, emit, report, advance


@pytest.mark.parametrize('flag', [1, 0, None, 'true'])
def test_deferred_seek_flag_requires_actual_boolean(tmp_path, flag):
    original, _, _, analysis, draft = original_setup(tmp_path)
    with pytest.raises(ReplayError, match='跳转'):
        ReplayPreparation(original.controller, analysis, draft, deferred_seek_pause=flag)


def test_deferred_seek_requires_owned_pipe_before_any_game_input(tmp_path):
    original, env, _, analysis, draft = original_setup(tmp_path)
    prep = ReplayPreparation(original.controller, analysis, draft, deferred_seek_pause=True)
    with pytest.raises(ReplayError, match='自动'):
        prep.begin()
    assert env.sent == [] and prep.state == 'failed'


@pytest.mark.parametrize('change', [dict(paused=False), dict(tick=13399)])
def test_already_paused_load_must_keep_its_anchor_through_seek_ledger(tmp_path, change):
    prep, env, console, decoder, _, _, _ = setup(tmp_path)
    before = tuple(env.writes)
    persist = console.persist
    def mutate(path, payload):
        persist(path, payload)
        decoder.snapshot_value = replace(decoder.snapshot_value, **change)
    console.persist = mutate
    with pytest.raises(ReplayError, match='暂停位置'):
        prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    assert tuple(env.writes) == before
    assert prep.state == 'failed' and console.state == 'failed'


def test_running_load_still_sends_owned_pause_before_deferred_seek(tmp_path):
    prep, env, console, _, _, report, _ = setup(tmp_path)
    report(13400, paused=False)
    write = console.pipes.backend.write
    def observed_pause(handle, payload, **kwargs):
        result = write(handle, payload, **kwargs)
        if payload == b'demo_pause\n':
            report(13400, paused=True, server=16732)
        return result
    console.pipes.backend.write = observed_pause
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    assert env.writes[-2:] == [b'demo_pause\n', b'demo_gototick 12282\n']
    assert prep.state == 'settling_seek'


def test_deferred_seek_waits_for_movement_then_actual_server_pause_without_widening_cut(tmp_path):
    prep, env, console, decoder, _, report, advance = settling(tmp_path)
    assert prep.preroll_tick == 12282  # Five seconds before the original cut.
    assert env.writes[-1:] == [b'demo_gototick 12282\n']
    assert b'demo_pause\n' not in env.writes  # Already paused: never toggle it into playback.
    assert prep.state == 'settling_seek'
    since, deadline = prep.since, prep.deadline
    report(12282)  # First target frame may still precede engine seek completion.
    assert prep.poll() is None and prep.state == 'settling_seek'
    assert env.writes[-1] == b'demo_gototick 12282\n'
    advance(); report(12300)
    prep.poll()
    assert prep.state == 'pausing_seek' and env.writes[-1] == b'demo_pause\n'
    assert (prep.since, prep.deadline) == (since, deadline)
    writes = tuple(env.writes)
    advance(); report(12305, paused=True)
    assert decoder.snapshot().paused and decoder.readback() is None
    prep.poll()  # A paused Panorama state cannot fabricate the server clock.
    assert prep.state == 'pausing_seek' and tuple(env.writes) == writes
    report(12305, paused=True, server=15637)
    prep.poll()
    assert prep.state == 'seeking' and prep.preroll_tick == 12305
    assert (prep.since, prep.deadline) == (since, deadline)
    prep.poll()
    assert prep.state == 'resuming' and env.writes[-1] == b'demo_resume\n'
    advance(); report(12310)
    prep.poll()
    assert prep.state == 'starting' and env.writes[-1] == b'demo_pauseatservertick 15934\n'
    advance(); report(12602, paused=True, server=15935)
    prep.poll()
    assert prep.state == 'starting' and not any(b'spec_player' in value for value in env.writes)
    report(12602, paused=True, server=15934, player='76561199797409805')
    prep.poll()
    assert prep.state == 'selecting'
    advance(); report(12602, paused=True, server=15934, first_person=False)
    assert prep.poll() is None and prep.state == 'selecting'
    advance(); report(12602, paused=True, server=15934)
    assert prep.poll() == decoder.readback() and prep.state == 'ready'
    assert prep.draft['selection']['start_tick'] == 12602
    assert prep.draft['selection']['server_start_tick'] == 15934
    assert prep.draft.get('position_tolerance_ticks', 0) == 0
    assert sum(value == b'demo_pause\n' for value in env.writes) == 1


@pytest.mark.parametrize('case', ['missing', 'requested', 'before', 'too_far', 'wrong_demo',
    'not_local', 'wrong_nonce', 'wrong_identity', 'stale', 'future'])
def test_invalid_seek_snapshot_never_authorizes_pause_or_extends_deadline(tmp_path, case):
    prep, env, console, decoder, _, report, _ = settling(tmp_path)
    changes = {'requested': dict(tick=12282), 'before': dict(tick=12281),
        'too_far': dict(tick=12347), 'wrong_demo': dict(path='C:/other.dem'),
        'not_local': dict(local=False), 'wrong_nonce': dict(nonce='OTHER_SESSION_0001')}
    if case != 'missing':
        args = dict(tick=12300)
        args.update(changes.get(case, {}))
        report(**args)
    if case == 'wrong_identity':
        decoder.snapshot_value = replace(decoder.snapshot_value,
            process_identity=replace(decoder.identity, created=124))
    if case in ('stale', 'future'):
        decoder.snapshot_value = replace(decoder.snapshot_value,
            observed_at=env.time-5.01 if case == 'stale' else env.time+.01)
    writes, deadline = tuple(env.writes), prep.deadline
    assert prep.poll() is None and prep.state == 'settling_seek'
    assert tuple(env.writes) == writes and prep.deadline == deadline
    env.time = deadline
    with pytest.raises(ReplayError, match='超时'):
        prep.poll()
    assert prep.state == 'failed' and tuple(env.writes) == writes


@pytest.mark.parametrize('case', ['scope', 'requested', 'state', 'since', 'deadline', 'reader', 'nonce',
    'snapshot', 'expired'])
def test_seek_pause_rechecks_original_scope_snapshot_after_consuming_ledger(tmp_path, case):
    prep, env, console, decoder, _, report, _ = settling(tmp_path)
    report(12300)
    writes = tuple(env.writes)
    persist = console.persist
    def mutate(path, payload):
        persist(path, payload)
        if case == 'scope': prep.draft['selection']['server_start_tick'] += 1
        elif case == 'requested': prep.preroll_tick += 1
        elif case == 'state': prep.state = 'settling_seek'
        elif case == 'since': prep.since += .01
        elif case == 'deadline': prep.deadline += 1
        elif case == 'reader': console.reader = SimpleNamespace(decoder=decoder,
            readback=decoder.readback, snapshot=decoder.snapshot)
        elif case == 'nonce': decoder.nonce = 'REPLACED_SESS_0001'
        elif case == 'expired': env.time = prep.deadline
        else: decoder.snapshot_value = replace(decoder.snapshot_value, tick=12347)
    console.persist = mutate
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and console.state == 'failed'
    assert tuple(env.writes) == writes
    assert json.loads(console.ledger.read_bytes())['consumed'] is True
    with pytest.raises(ReplayError): prep.poll()
    assert tuple(env.writes) == writes


@pytest.mark.parametrize('case', ['no_server', 'before', 'too_far', 'at_server_start',
    'wrong_demo', 'not_local', 'wrong_identity', 'stale', 'future'])
def test_wait_for_real_paused_evidence_in_original_seek_range(tmp_path, case):
    prep, env, console, decoder, _, report, advance = settling(tmp_path)
    report(12300); prep.poll()
    advance()
    changes = {'no_server': dict(server=None), 'before': dict(tick=12282),
        'too_far': dict(tick=12347), 'at_server_start': dict(server=15934),
        'wrong_demo': dict(path='C:/other.dem'), 'not_local': dict(local=False)}
    args = dict(tick=12305, paused=True, server=15637)
    args.update(changes.get(case, {})); report(**args)
    if case == 'wrong_identity':
        decoder.proof = replace(decoder.proof,
            process_identity=replace(decoder.identity, created=124))
    if case in ('stale', 'future'):
        decoder.proof = replace(decoder.proof,
            observed_at=env.time-5.01 if case == 'stale' else env.time+.01)
    writes, deadline = tuple(env.writes), prep.deadline
    if case == 'at_server_start':
        with pytest.raises(ReplayError, match='起点之前'): prep.poll()
        assert prep.state == 'failed'
    else:
        assert prep.poll() is None and prep.state == 'pausing_seek'
    assert tuple(env.writes) == writes and prep.deadline == deadline


def test_uncertain_seek_pause_delivery_never_resends_pause_or_seek(tmp_path):
    prep, env, console, _, _, report, _ = settling(tmp_path)
    report(12300)
    writes = tuple(env.writes)
    env.failure = True
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and console.state == 'failed'
    assert tuple(env.writes) == writes+(b'demo_pause\n',)
    env.failure = False
    with pytest.raises(ReplayError): prep.poll()
    assert tuple(env.writes) == writes+(b'demo_pause\n',)


def test_seek_snapshot_input_readiness_cannot_renew_original_deadline(tmp_path):
    prep, env, console, _, _, report, _ = settling(tmp_path)
    report(12300)
    writes, deadline = tuple(env.writes), prep.deadline
    def delayed():
        env.time = deadline
        return True
    console.input_ready = delayed
    with pytest.raises(ReplayError, match='超时'): prep.poll()
    assert prep.state == 'failed' and tuple(env.writes) == writes
    assert prep.deadline == deadline
