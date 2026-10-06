"""Independent audit of authority frozen when the resume input was consumed."""
from dataclasses import replace

import pytest

from cs2pov.services.replay import ReplayError
from tests.unit.test_pipe_resume_pause_phases import resuming_preparation
from tests.unit.test_replay_preparation import pipe_preparation


@pytest.mark.parametrize('field', ['deadline', 'since'])
def test_resuming_must_reject_authority_replaced_before_next_poll(tmp_path, field):
    prep, env, _ = resuming_preparation(tmp_path)
    original_since, original_deadline = prep.since, prep.deadline
    before = tuple(env.writes)
    if field == 'deadline':
        env.time = original_deadline + .1
        prep.deadline = original_deadline + 20
        observed_at = env.time
    else:
        env.time += .1
        prep.since = original_since - 10
        observed_at = original_since - .1
    env.replay_proof = replace(env.replay_proof, paused=False, tick=12420,
                               observed_at=observed_at)
    with pytest.raises(ReplayError):
        prep.poll()
    assert prep.state == 'failed' and tuple(env.writes) == before


@pytest.mark.parametrize('field', ['deadline', 'since', 'scope'])
def test_initial_resume_must_recheck_frozen_phase_after_its_ledger(tmp_path, field):
    prep, env, console = pipe_preparation(tmp_path)
    prep.begin(loading_since=env.time, loading_deadline=env.time+120)
    env.time += 1
    env.replay_proof = replace(env.replay_proof, observed_at=env.time)
    resume_sequence = console.sequence + 2  # timescale then the independent resume.
    original = console.persist
    def mutate(path, payload):
        original(path, payload)
        if console.sequence != resume_sequence:
            return
        if field == 'deadline':
            prep.deadline += 1
        elif field == 'since':
            prep.since -= 1
        else:
            prep.draft['selection']['server_start_tick'] += 1
    console.persist = mutate
    with pytest.raises(ReplayError):
        prep.poll()
    assert prep.state == 'failed'
    assert b'demo_resume\n' not in env.writes
