from dataclasses import replace
import pytest
from cs2pov.services.replay import position_matches, ReplayError
from tests.unit.test_replay_log import decoder, emit, report
from tests.recording_support import recording_setup


def test_second_precision_rejects_more_than_one_second_and_invalid_bounds():
    draft = {'position_tolerance_ticks': 64}
    assert position_matches(draft, 12603, 12602)
    assert position_matches(draft, 12666, 12602)
    assert not position_matches(draft, 12667, 12602)
    assert not position_matches(draft, True, 1)
    assert not position_matches({}, 12603, 12602)
    for bad in [True, -1, 1025, 1.5]:
        with pytest.raises(ReplayError):
            position_matches({'position_tolerance_ticks': bad}, 1, 1)


def test_real_tick_boundary_retains_engine_position_and_requires_two_sources():
    value, _ = decoder()
    value.allow_tick_boundary = True
    value.consume('10/03 02:39:49 CGameRules - paused on tick 15935')
    value.consume('10/03 02:39:49 [Demo] Demo paused at engine time 15934, demo tick 12603')
    emit(value, report(nTick=12602))
    assert value.readback().tick == 12603
    assert value.readback().server_tick == 15934
    emit(value, report(sequence=2, nTick=12601))
    assert value.readback() is None


def test_second_precision_does_not_relax_player_identity(tmp_path):
    task, preview, _, env = recording_setup(tmp_path)
    preview.draft['position_tolerance_ticks'] = 64
    env.foreground = env.identity.pid
    env.proof = replace(env.proof, tick=11, server_tick=101)
    assert preview.controller.verify_result(preview.draft, since=99).tick == 11
    env.proof = replace(env.proof, player_id='202')
    with pytest.raises(ReplayError):
        preview.controller.verify_result(preview.draft, since=99)


def test_second_mode_resume_settles_without_refreshing_deadline(tmp_path):
    from tests.unit.test_pipe_resume_pause_phases import resuming_preparation, moving_preroll
    prep, env, _ = resuming_preparation(tmp_path)
    prep.draft['position_tolerance_ticks'] = 64
    original_deadline, original_since = prep.deadline, prep.since
    moving_preroll(env)
    before = list(env.writes)
    prep.poll()
    assert prep.state == 'resuming' and env.writes == before
    env.time = original_since + 1.3
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    prep.poll()
    assert env.writes[-1] == b'demo_pauseatservertick 15934\n'
    assert (prep.since, prep.deadline) == (original_since, original_deadline)
