"""Held switching keys delay unconsumed input without renewing authority."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cs2pov.adapters.replay_log import ReplaySnapshot
from cs2pov.services.replay import ReplayError
from tests.unit.test_preview_session import setup as preview_setup, load
from tests.unit.test_hidden_playback import fixture as playback_setup, arm_ready
from tests.recording_support import recording_setup, fresh, playing


def ready_preview(tmp_path):
    preview, env, proof = preview_setup(tmp_path)
    preview.draft['selection']['duration'] = (13400-12602)/64
    load(preview, env, proof)
    for changes in ({}, {'tick':12602,'server_tick':15934},
                    {'tick':12602,'server_tick':15934,'player_id':'76561199198478034'}):
        env.time += 1
        env.proof = replace(proof, observed_at=env.time, **changes)
        preview.poll()
    assert preview.state == 'ready'
    return preview, env, proof


@pytest.mark.parametrize('expired', [False, True])
def test_initial_confirmation_waits_for_release_without_extending_deadline(tmp_path, expired):
    preview, env, proof = preview_setup(tmp_path)
    preview.start()
    preview.confirm_console(step_token=preview.console_step_token)
    env.pid = proof.process_identity.pid
    preview.console.input_ready = lambda: False
    deadline = preview.deadline
    env.time += 2
    assert preview.poll() is None
    assert preview.state == 'awaiting_console' and preview.deadline == deadline
    assert preview.console_confirmed and not preview.console.console_open_confirmed
    assert env.sent == [] and env.closed == 0
    if expired:
        env.time = deadline
        with pytest.raises(ReplayError, match='超时'):
            preview.poll()
        assert env.sent == [] and env.closed == env.restored == 1
    else:
        preview.console.input_ready = lambda: True
        preview.poll()
        assert preview.state == 'loading'
        assert env.sent == [f'playdemo "{proof.path.as_posix()}"']


def test_loaded_demo_confirmation_waits_without_beginning_seek(tmp_path):
    preview, env, proof = preview_setup(tmp_path)
    preview.start(); preview.confirm_console(); env.pid = 42; preview.poll()
    env.time += 1
    env.snapshot = ReplaySnapshot(proof.process_identity, proof.path, True, 0,
        False, proof.player_id, True, env.time)
    preview.poll(); preview.confirm_console()
    preview.console.input_ready = lambda: False
    deadline = preview.deadline
    preview.poll()
    assert preview.state == 'awaiting_demo_console' and preview.preparation is None
    assert preview.deadline == deadline and len(env.sent) == 1
    preview.console.input_ready = lambda: True
    preview.poll()
    assert preview.state == 'preparing'
    assert env.sent[-2:] == ['demo_pause', 'demo_gototick 12538; demo_pause']


def test_initial_readiness_check_cannot_authorize_input_past_original_deadline(tmp_path):
    preview, env, _ = preview_setup(tmp_path)
    preview.start(); preview.confirm_console(); env.pid = 42
    deadline = preview.deadline
    def slow_check():
        env.time = deadline
        return True
    preview.console.input_ready = slow_check
    with pytest.raises(ReplayError, match='超时'):
        preview.poll()
    assert env.sent == [] and env.closed == env.restored == 1


def test_loaded_demo_readiness_cannot_use_snapshot_that_aged_during_check(tmp_path):
    preview, env, proof = preview_setup(tmp_path)
    preview.start(); preview.confirm_console(); env.pid=42; preview.poll()
    env.time += 1
    env.snapshot=ReplaySnapshot(proof.process_identity,proof.path,True,0,
        False,proof.player_id,True,env.time)
    preview.poll(); preview.confirm_console()
    deadline=preview.deadline
    def slow_check():
        env.time += 6
        return True
    preview.console.input_ready=slow_check
    preview.poll()
    assert preview.state == 'awaiting_demo_console' and preview.deadline == deadline
    assert preview.console_confirmed and len(env.sent) == 1
    preview.console.input_ready=lambda:True
    env.snapshot=replace(env.snapshot,observed_at=env.time)
    preview.poll()
    assert preview.state == 'preparing' and len(env.sent) == 3


def test_f8_readiness_cannot_consume_play_with_aged_start_evidence(tmp_path):
    playback, _, console, clock, *_=playback_setup(tmp_path)
    arm_ready(playback,clock)
    def slow_check(*, playback=False):
        clock.advance(6)
        return True
    console.input_ready=slow_check
    with pytest.raises(ReplayError,match='过期'):
        playback.poll()
    assert console.keys == 0 and not playback.triggered and playback.state == 'failed'


@pytest.mark.parametrize('phase', ['querying_empty', 'querying_installed', 'awaiting_foreground'])
def test_hidden_readiness_check_keeps_original_query_or_play_deadline(tmp_path, phase):
    playback, _, console, clock, *_ = playback_setup(tmp_path)
    if phase == 'awaiting_foreground':
        arm_ready(playback, clock)
    else:
        playback.begin()
        if phase == 'querying_installed': playback.poll()
    baseline = (len(console.commands), console.hidden, console.keys, playback.triggered)
    deadline = playback.deadline
    def slow_check(*, playback=False):
        clock.value = deadline
        return True
    console.input_ready = slow_check
    with pytest.raises(ReplayError, match='超时'):
        playback.poll()
    assert playback.state == 'failed'
    assert (len(console.commands),console.hidden,console.keys,playback.triggered) == baseline


@pytest.mark.parametrize('phase', ['awaiting_play_console', 'awaiting_end_console'])
def test_later_console_confirmation_cannot_query_while_keys_held(tmp_path, phase):
    preview, env, proof = ready_preview(tmp_path)
    if phase == 'awaiting_play_console':
        preview.prepare_playback()
    else:
        preview.state = 'play_ended'
        env.proof = replace(env.proof, tick=13400, server_tick=16732)
        preview.playback = SimpleNamespace(state='ended', triggered=True,
            binding_may_exist=True, started_at=100.,
            confirm_end_console=lambda: env.sent.append('final-query'))
        preview.check_playback_binding()
    preview.confirm_console(step_token=preview.console_step_token)
    preview.console.input_ready = lambda: False
    deadline, count = preview.deadline, len(env.sent)
    preview.poll()
    assert preview.state == phase and preview.deadline == deadline
    assert preview.console_confirmed and not preview.console.console_open_confirmed
    assert len(env.sent) == count
    preview.console.input_ready = lambda: True
    preview.poll()
    assert preview.state == ('arming_playback' if phase == 'awaiting_play_console' else 'checking_binding')
    assert len(env.sent) == count+1


@pytest.mark.parametrize('phase', ['querying_empty', 'querying_installed', 'awaiting_foreground'])
def test_hidden_playback_wait_does_not_consume_query_hide_or_f8(tmp_path, phase):
    playback, controller, console, clock, *_ = playback_setup(tmp_path)
    if phase == 'awaiting_foreground':
        arm_ready(playback, clock)
    else:
        playback.begin()
        if phase == 'querying_installed':
            playback.poll()
    calls = []
    console.input_ready = lambda *, playback=False: calls.append(playback) or False
    baseline = (len(console.commands), console.hidden, console.keys,
                playback.triggered, playback.binding_may_exist)
    deadline = playback.deadline
    playback.poll()
    assert playback.state == phase and playback.deadline == deadline
    assert (len(console.commands),console.hidden,console.keys,
            playback.triggered,playback.binding_may_exist) == baseline
    assert calls == [phase == 'awaiting_foreground']
    console.input_ready = lambda *, playback=False: True
    playback.poll()
    if phase == 'querying_empty':
        assert playback.state == 'querying_installed' and len(console.commands) == baseline[0]+1
    elif phase == 'querying_installed':
        assert playback.state == 'ready' and console.hidden == baseline[1]+1
    else:
        assert playback.state == 'playing' and console.keys == 1 and playback.triggered


def test_hidden_f8_wait_expires_with_no_consumed_input(tmp_path):
    playback, _, console, clock, *_ = playback_setup(tmp_path)
    arm_ready(playback, clock)
    console.input_ready = lambda *, playback=False: False
    playback.poll()
    assert console.keys == 0 and not playback.triggered
    clock.value = playback.deadline
    with pytest.raises(ReplayError, match='超时'):
        playback.poll()
    assert console.keys == 0 and not playback.triggered and playback.state == 'failed'


@pytest.mark.parametrize('expired', [False, True])
def test_nvidia_start_wait_preserves_authority_deadline_and_one_input(tmp_path, expired):
    task, preview, workflow, env = recording_setup(tmp_path)
    task.arm(); task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    env.foreground = env.identity.pid
    preview.console.input_ready = lambda: False
    deadline = task.snapshot()['deadline']
    fresh(env); task.poll()
    assert task.state == 'awaiting_start_foreground' and env.inputs == []
    assert workflow.session.start_attempted_at is None
    assert task.snapshot()['deadline'] == deadline
    if expired:
        env.now = deadline; task.poll()
        assert task.terminal and env.inputs == [] and env.closes == env.restores == 1
        assert workflow.session.state == 'cancelled'
    else:
        preview.console.input_ready = lambda: True
        fresh(env); task.poll(); task.poll()
        assert task.state == 'awaiting_started' and len(env.inputs) == 1


@pytest.mark.parametrize('expired', [False, True])
def test_nvidia_stop_wait_keeps_original_two_second_endpoint_limit(tmp_path, expired):
    task, preview, workflow, env = recording_setup(tmp_path)
    playing(task, env)
    preview.console.input_ready = lambda *, playback=False: False
    fresh(env, end=True); observed = env.proof.observed_at; task.poll()
    assert task.state == 'awaiting_stop_foreground' and len(env.inputs) == 1
    assert workflow.session.stop_attempted_at is None
    assert task.snapshot()['deadline'] == observed+2
    env.now += 1; fresh(env, end=True); task.poll()
    assert task.state == 'awaiting_stop_foreground'
    assert task.snapshot()['deadline'] == observed+2 and len(env.inputs) == 1
    if expired:
        env.now = observed+2; task.poll()
        assert task.terminal and len(env.inputs) == 1
        assert workflow.session.state == 'unknown' and '手动' in task.error
        assert env.closes == env.restores == 1
    else:
        preview.console.input_ready = lambda *, playback=False: True
        task.poll(); task.poll()
        assert task.state == 'awaiting_stopped' and len(env.inputs) == 2
