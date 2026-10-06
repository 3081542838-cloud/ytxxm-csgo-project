"""Real pipe ledgers and controllers around the two engine-frame handoff.

Only OS pipe writes and incoming replay telemetry are injected. Exact cuts,
identity, scope, original deadlines and one-use consumption remain checked.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.services.hidden_playback import HiddenPlayback
from cs2pov.services.replay import ReplayController, ReplayEvidence, ReplayError
from cs2pov.storage.settings import DataError
from tests.unit.test_pipe_console import setup as pipe_setup, ready as pipe_ready
from tests.unit.test_replay_preparation import pipe_preparation


def resuming_preparation(tmp_path):
    prep, env, console = pipe_preparation(tmp_path)
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    env.time += 1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    prep.poll()
    assert prep.state == 'resuming' and env.writes[-1] == b'demo_resume\n'
    return prep, env, console


def moving_preroll(env):
    env.time += .1
    env.replay_proof = replace(env.replay_proof, paused=False, tick=12420,
                               observed_at=env.time)


def test_prepare_waits_for_actual_unpaused_report_then_submits_only_pauseat(tmp_path):
    prep, env, console = resuming_preparation(tmp_path)
    before, since, deadline = tuple(env.writes), prep.since, prep.deadline
    for _ in range(3):
        env.time += .1
        env.replay_proof = replace(env.replay_proof, observed_at=env.time)
        assert prep.poll() is None and prep.state == 'resuming'
        assert tuple(env.writes) == before
    moving_preroll(env)
    prep.poll()
    assert prep.state == 'starting'
    assert (prep.since, prep.deadline) == (since, deadline)
    assert env.writes[-1] == b'demo_pauseatservertick 15934\n'
    assert sum(value == b'demo_resume\n' for value in env.writes) == 1
    assert console.sequence == len(env.writes)
    before = tuple(env.writes)
    prep.poll()  # Unpaused telemetry cannot grant exact start/selection.
    assert tuple(env.writes) == before and prep.state == 'starting'
    assert not any(b'spec_player' in value for value in env.writes)


@pytest.mark.parametrize('change', ['missing', 'paused', 'wrong_demo', 'not_local',
    'before_lead', 'at_start', 'after_start', 'wrong_identity', 'stale', 'future'])
def test_prepare_missing_or_invalid_post_resume_state_never_schedules_pause(tmp_path, change):
    prep, env, _ = resuming_preparation(tmp_path)
    moving_preroll(env)
    proof = env.replay_proof
    updates = {'paused': dict(paused=True), 'wrong_demo': dict(path=Path('C:/wrong.dem')),
        'not_local': dict(local_demo=False), 'before_lead': dict(tick=12409),
        'at_start': dict(tick=12602), 'after_start': dict(tick=12603),
        'wrong_identity': dict(process_identity=replace(proof.process_identity, created=124)),
        'stale': dict(observed_at=95.), 'future': dict(observed_at=env.time+.01)}
    env.replay_proof = None if change == 'missing' else replace(proof, **updates[change])
    before, deadline = tuple(env.writes), prep.deadline
    assert prep.poll() is None and prep.state == 'resuming'
    assert tuple(env.writes) == before and prep.deadline == deadline
    env.time = deadline
    with pytest.raises(ReplayError, match='超时'):
        prep.poll()
    assert prep.state == 'failed' and tuple(env.writes) == before


@pytest.mark.parametrize('change', ['scope', 'state', 'since', 'deadline', 'paused',
    'position', 'demo', 'identity', 'expired'])
def test_prepare_pauseat_rechecks_phase_and_running_state_after_its_ledger(tmp_path, change):
    prep, env, console = resuming_preparation(tmp_path)
    moving_preroll(env)
    before = tuple(env.writes)
    original = console.persist
    def mutate(path, payload):
        original(path, payload)
        if change == 'scope': prep.draft['selection']['server_start_tick'] += 1
        elif change == 'state': prep.state = 'resuming'
        elif change == 'since': prep.since += .01
        elif change == 'deadline': prep.deadline += 1
        elif change == 'expired': env.time = prep.deadline
        else:
            values = {'paused': dict(paused=True), 'position': dict(tick=12602),
                'demo': dict(path=Path('C:/wrong.dem')),
                'identity': dict(process_identity=replace(env.replay_proof.process_identity, created=124))}
            env.replay_proof = replace(env.replay_proof, **values[change])
    console.persist = mutate
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and console.state == 'failed'
    assert tuple(env.writes) == before
    with pytest.raises(ReplayError): prep.poll()
    assert tuple(env.writes) == before


def test_prepare_unknown_pauseat_delivery_is_consumed_without_resending_resume(tmp_path):
    prep, env, console = resuming_preparation(tmp_path)
    moving_preroll(env)
    before = len(env.writes)
    env.failure = True
    with pytest.raises(ReplayError): prep.poll()
    assert prep.state == 'failed' and len(env.writes) == before+1
    env.failure = False
    with pytest.raises(ReplayError): prep.poll()
    assert len(env.writes) == before+1
    assert sum(value == b'demo_resume\n' for value in env.writes) == 1


def playing_pipe(tmp_path, *, before_play=None):
    console, env, identity = pipe_setup(tmp_path)
    pipe_ready(console, env, identity)
    env.foreground = identity.pid
    console.keyboard.foreground_pid = lambda: env.foreground
    draft = dict(demo=str(tmp_path/'original.dem'), selection=dict(start_tick=10,
        end_tick=20, server_start_tick=100, server_end_tick=110, player_id='101', duration=1.))
    env.replay_proof = ReplayEvidence(identity, True, Path(draft['demo']), 10, 100,
                                     True, '101', True, env.time)
    console.reader = SimpleNamespace(readback=lambda: env.replay_proof,
                                     snapshot=lambda: env.replay_proof)
    playback = HiddenPlayback(ReplayController(console.game, console, clock=lambda: env.time),
        tmp_path, draft, clock=lambda: env.time)
    playback.state = 'awaiting_foreground'
    playback.binding_may_exist = True
    playback.deadline = env.time+30
    if before_play is not None: before_play(playback, env, console)
    playback.poll()
    assert playback.state == 'playing' and playback.triggered
    assert env.writes[-1] == b'unbind F8; demo_resume\n'
    return playback, env, console


def moving_clip(env):
    env.time += .1
    env.replay_proof = replace(env.replay_proof, paused=False, tick=12,
                               observed_at=env.time)


def test_pipe_playback_endpoint_is_delayed_single_command_with_exact_end_required(tmp_path):
    playback, env, console = playing_pipe(tmp_path)
    before, deadline = tuple(env.writes), playback.deadline
    playback.poll()  # Start is still paused, so no endpoint command yet.
    assert tuple(env.writes) == before and not playback.pipe_end_attempted
    moving_clip(env)
    playback.poll()
    assert playback.moving_seen and playback.pipe_end_attempted
    assert env.writes[-1] == b'demo_pauseatservertick 110\n'
    assert playback.deadline == deadline and console.sequence == len(env.writes)
    before = tuple(env.writes)
    playback.poll()
    assert tuple(env.writes) == before
    env.time += .2
    env.replay_proof = replace(env.replay_proof, tick=20, paused=True,
                               server_tick=111, observed_at=env.time)
    with pytest.raises(ReplayError): playback.poll()
    assert playback.state == 'failed' and tuple(env.writes) == before


@pytest.mark.parametrize('boundary', ['checkpoint', 'ledger'])
@pytest.mark.parametrize('change', ['foreground', 'scope', 'state', 'deadline', 'since',
    'paused', 'at_end', 'demo', 'player', 'first_person', 'identity', 'expired'])
def test_pipe_endpoint_after_persistence_rechecks_running_scope_and_original_authority(tmp_path, boundary, change):
    playback, env, console = playing_pipe(tmp_path)
    moving_clip(env)
    before = tuple(env.writes)
    def mutation():
        if change == 'foreground': env.foreground = 99
        elif change == 'scope': playback.draft['selection']['server_end_tick'] += 1
        elif change == 'state': playback.state = 'ended'
        elif change == 'deadline': playback.deadline += 1
        elif change == 'since': playback.started_at -= 1
        elif change == 'expired': env.time = playback.deadline
        else:
            values = {'paused': dict(paused=True), 'at_end': dict(tick=20),
                'demo': dict(path=Path('C:/wrong.dem')), 'player': dict(player_id='202'),
                'first_person': dict(first_person=False),
                'identity': dict(process_identity=replace(env.replay_proof.process_identity, created=124))}
            env.replay_proof = replace(env.replay_proof, **values[change])
    if boundary == 'checkpoint': playback.checkpoint = mutation
    else:
        original = console.persist
        def mutate(path, payload):
            original(path, payload); mutation()
        console.persist = mutate
    with pytest.raises(DataError): playback.poll()
    assert playback.pipe_end_attempted and playback.triggered and playback.state == 'failed'
    assert tuple(env.writes) == before
    playback.poll()
    assert tuple(env.writes) == before


def test_pipe_endpoint_unknown_delivery_never_resends_or_claims_stopped(tmp_path):
    playback, env, console = playing_pipe(tmp_path)
    moving_clip(env)
    before = len(env.writes)
    env.failure = True
    with pytest.raises(DataError): playback.poll()
    assert playback.state == 'failed' and playback.pipe_end_attempted
    assert len(env.writes) == before+1
    env.failure = False
    env.replay_proof = replace(env.replay_proof, paused=True, tick=20, server_tick=110)
    assert playback.poll() is None and playback.end_proof is None
    assert len(env.writes) == before+1 and playback.binding_may_exist


def test_unpaused_at_end_never_authorizes_late_pauseat(tmp_path):
    playback, env, _ = playing_pipe(tmp_path)
    moving_clip(env)
    env.replay_proof = replace(env.replay_proof, tick=20)
    before = tuple(env.writes)
    with pytest.raises(ReplayError): playback.poll()
    assert playback.state == 'failed' and tuple(env.writes) == before


@pytest.mark.parametrize('change', ['since', 'deadline'])
def test_pipe_playback_original_time_anchor_cannot_change_between_polls(tmp_path, change):
    playback, env, _ = playing_pipe(tmp_path)
    moving_clip(env)
    if change == 'since': playback.started_at -= 1
    else: playback.deadline += 20
    before = tuple(env.writes)
    with pytest.raises(ReplayError): playback.poll()
    assert playback.state == 'failed' and tuple(env.writes) == before


@pytest.mark.parametrize('boundary', ['checkpoint', 'ledger'])
@pytest.mark.parametrize('change', ['since', 'deadline', 'scope'])
def test_pipe_initial_resume_rechecks_frozen_playback_phase_after_persistence(tmp_path, boundary, change):
    holder = {}
    def configure(playback, env, console):
        holder.update(playback=playback, env=env, before=tuple(env.writes))
        def mutation():
            if change == 'since': playback.started_at -= 1
            elif change == 'deadline': playback.deadline += 20
            else: playback.draft['selection']['server_end_tick'] += 1
        if boundary == 'checkpoint': playback.checkpoint = mutation
        else:
            original = console.persist
            def mutate(path, payload):
                original(path, payload); mutation()
            console.persist = mutate
    with pytest.raises(ReplayError): playing_pipe(tmp_path, before_play=configure)
    assert holder['playback'].triggered and holder['playback'].state == 'failed'
    assert tuple(holder['env'].writes) == holder['before']
