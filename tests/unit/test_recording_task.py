from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.disk import MIN_VIDEO_BYTES
from cs2pov.services.recording_task import RecordingTask, RecordingTaskError
from tests.recording_support import recording_setup, fresh, authorize_start, playing, stopped_wait, finish


@pytest.fixture
def setup(tmp_path):
    return recording_setup(tmp_path)


def test_complete_chain_uses_owned_preview_once_and_never_opens_console_before_stopped(setup):
    task, preview, workflow, env = setup
    before = Path(preview.draft['demo']).read_bytes()
    stopped_wait(task, env)
    assert env.keys == 1 and env.queries == [] and env.binding_checks == 0
    finish(task, preview, env)
    assert task.terminal and task.durable and task.state == 'complete' and task.error == ''
    assert workflow.session.state == 'stopped' and workflow.state == 'awaiting_video'
    assert env.inputs == ['Alt+F9', 'Alt+F9'] and env.keys == 1 and len(env.queries) == 1
    assert env.launches == 0 and env.closes == env.restores == 1
    assert workflow.session.game is preview.game
    assert Path(preview.draft['demo']).read_bytes() == before
    assert env.contexts[-1].started_wall is not None
    snapshot = task.snapshot()
    assert snapshot['schema'] == 1 and snapshot['task_id'] == workflow.session.task_id
    assert snapshot['session_id'] == preview.session.name
    assert snapshot['recording_state'] == 'stopped' and snapshot['restore_status'] == 'complete'
    events = list(env.events)
    for _ in range(3):
        assert task.poll() == snapshot
    assert env.events == events


@pytest.mark.parametrize('invalid', ['preview_state', 'binding_state', 'binding_missing',
    'binding_used', 'binding_value', 'scope', 'identity', 'hotkey', 'game', 'directory'])
def test_arm_mismatch_blocks_all_input_and_closes_existing_preview(setup, invalid):
    task, preview, workflow, env = setup
    if invalid == 'preview_state': preview.state = 'ready'
    elif invalid == 'binding_state': preview.playback.state = 'ended'
    elif invalid == 'binding_missing': preview.playback.binding_may_exist = False
    elif invalid == 'binding_used': preview.playback.triggered = True
    elif invalid == 'binding_value': preview.playback.binding = 'demo_resume'
    elif invalid == 'scope': preview.draft['selection']['player_id'] = '202'
    elif invalid == 'identity': env.identity = replace(env.identity, created=124)
    elif invalid == 'hotkey': preview.settings = replace(preview.settings, hotkey='Alt+F10')
    elif invalid == 'game': workflow.session.game = SimpleNamespace(verify=lambda: env.identity)
    elif invalid == 'directory': preview.settings = replace(preview.settings, video_directory=str(preview.session))
    with pytest.raises(RecordingTaskError): task.arm()
    assert env.inputs == [] and env.keys == 0 and env.queries == []
    assert env.closes == 1 and task.terminal


def test_f8_hotkey_conflict_is_rejected_even_with_matching_frozen_settings(setup):
    task, preview, workflow, env = setup
    preview.settings = replace(preview.settings, hotkey='Alt+F8')
    workflow.session._hotkey = 'Alt+F8'
    task = RecordingTask(preview, workflow, checkpoint=task.checkpoint, clock=task.clock)
    with pytest.raises(RecordingTaskError, match='F8'): task.arm()
    assert env.inputs == [] and env.closes == 1


def test_start_wait_continues_reading_current_endpoint_and_has_30_second_limit(setup):
    task, _preview, workflow, env = setup
    task.arm()
    task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    initial_deadline = task.snapshot()['deadline']
    for _ in range(20):
        fresh(env)
        task.poll()
        assert env.inputs == [] and env.keys == 0
        assert task.snapshot()['deadline'] == initial_deadline
    env.now = initial_deadline
    env.proof = replace(env.proof, observed_at=env.now)
    env.movement = replace(env.movement, observed_at=env.now)
    env.foreground = env.identity.pid
    task.poll()
    assert task.terminal and workflow.session.state == 'cancelled'
    assert env.inputs == [] and env.closes == 1


def test_changed_preview_or_low_disk_during_wait_never_sends_start(setup):
    task, _preview, workflow, env = setup
    task.arm()
    task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    fresh(env)
    env.proof = replace(env.proof, player_id='202')
    task.poll()
    assert task.terminal and env.inputs == [] and env.closes == 1


def test_space_drop_after_confirmation_blocks_start_before_input(setup):
    task, _preview, workflow, env = setup
    task.arm()
    task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    fresh(env)
    env.disk_free = MIN_VIDEO_BYTES-1
    env.foreground = env.identity.pid
    task.poll()
    assert task.terminal and workflow.session.state == 'cancelled'
    assert env.inputs == [] and env.keys == 0 and env.closes == 1


@pytest.mark.parametrize('phase', ['not_recording', 'started', 'stopped'])
def test_wrong_foreign_old_and_duplicate_confirmation_never_cancel_valid_task(setup, phase):
    task, preview, workflow, env = setup
    if phase == 'not_recording': task.arm()
    elif phase == 'started': authorize_start(task, env)
    else: stopped_wait(task, env)
    method = getattr(task, 'confirm_'+phase)
    before = task.snapshot()
    for task_id, token in [('foreign', task.confirmation_token), (task.task_id, 'old-step'),
                            (task.task_id, None), (task.task_id, '')]:
        with pytest.raises(RecordingTaskError): method(task_id=task_id, step_token=token)
        assert task.snapshot() == before and env.closes == 0
    method(task_id=task.task_id, step_token=task.confirmation_token)
    count = len(env.inputs)
    with pytest.raises(RecordingTaskError): method(task_id=task.task_id, step_token=before['confirmation_token'])
    assert len(env.inputs) == count and env.closes == 0


@pytest.mark.parametrize('phase', ['not_recording', 'started', 'stopped'])
def test_expired_confirmation_without_timer_poll_closes_and_never_retries(setup, phase):
    task, _preview, workflow, env = setup
    if phase == 'not_recording': task.arm()
    elif phase == 'started': authorize_start(task, env)
    else: stopped_wait(task, env)
    token = task.confirmation_token
    count = len(env.inputs)
    env.now = workflow.session.deadline
    with pytest.raises(RecordingTaskError):
        getattr(task, 'confirm_'+phase)(task_id=task.task_id, step_token=token)
    assert task.terminal and env.closes == 1 and len(env.inputs) == count
    assert workflow.session.state == ('cancelled' if count == 0 else 'unknown')
    task.poll()
    assert len(env.inputs) == count


def test_started_confirmation_is_required_before_any_f8_and_play_is_once(setup):
    task, _preview, workflow, env = setup
    authorize_start(task, env)
    for _ in range(4):
        fresh(env)
        task.poll()
    assert env.keys == 0 and workflow.session.state == 'awaiting_started'
    task.confirm_started(task_id=task.task_id, step_token=task.confirmation_token)
    task.poll()
    assert env.keys == 1
    task.poll()
    assert env.keys == 1 and len(env.inputs) == 1


def test_stop_focus_loss_is_bounded_by_two_seconds_from_actual_end(setup):
    task, _preview, workflow, env = setup
    playing(task, env)
    fresh(env, end=True)
    env.foreground = 999
    task.poll()
    assert task.state == 'awaiting_stop_foreground' and len(env.inputs) == 1
    deadline = task.snapshot()['deadline']
    assert deadline <= env.proof.observed_at+2
    env.now = deadline
    env.proof = replace(env.proof, observed_at=env.now)
    env.foreground = env.identity.pid
    task.poll()
    assert task.terminal and workflow.session.state == 'unknown'
    assert len(env.inputs) == 1 and env.keys == 1 and env.queries == []


@pytest.mark.parametrize('phase', ['start_pending', 'stop_pending'])
def test_slow_pending_checkpoint_cannot_outlive_foreground_input_deadline(setup, phase):
    task, _preview, workflow, env = setup
    if phase == 'start_pending':
        task.arm()
        task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    else:
        playing(task, env)
        fresh(env, end=True)
    count = len(env.inputs)
    def slow(value):
        if value.state == phase:
            env.now += 31 if phase == 'start_pending' else 2
            env.proof = replace(env.proof, observed_at=env.now)
    env.recording_failure = slow
    env.foreground = env.identity.pid
    task.poll()
    assert task.terminal and workflow.session.state == 'unknown'
    assert len(env.inputs) == count and env.closes == 1


def test_stop_rejects_different_player_or_inexact_endpoint(setup):
    task, _preview, workflow, env = setup
    playing(task, env)
    fresh(env, end=True)
    env.proof = replace(env.proof, server_tick=111)
    task.poll()
    assert task.terminal and workflow.session.state == 'unknown'
    assert len(env.inputs) == 1 and env.queries == [] and env.closes == 1


def test_input_uncertainty_becomes_unknown_and_always_restores_without_retry(setup):
    task, _preview, workflow, env = setup
    task.arm()
    task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    env.input_failure = True
    env.foreground = env.identity.pid
    task.poll()
    assert task.terminal and workflow.session.state == 'unknown' and len(env.inputs) == 1
    for _ in range(3): task.poll()
    assert len(env.inputs) == 1 and env.closes == env.restores == 1 and env.keys == 0


def test_task_checkpoint_failure_does_not_prevent_cleanup_or_claim_durable(setup):
    task, _preview, workflow, env = setup
    env.checkpoint_failure = lambda _value: (_ for _ in ()).throw(OSError('task disk full'))
    with pytest.raises(RecordingTaskError, match='task disk full'): task.arm()
    assert task.terminal and not task.durable and env.closes == env.restores == 1
    assert env.inputs == [] and env.keys == 0 and env.queries == []


def test_recorder_checkpoint_failure_after_start_confirmation_prevents_f8_but_closes(setup):
    task, _preview, workflow, env = setup
    authorize_start(task, env)
    def fail(value):
        if value.state in ('recording', 'unknown'):
            raise OSError('recording disk full')
    env.recording_failure = fail
    with pytest.raises(RecordingTaskError):
        task.confirm_started(task_id=task.task_id, step_token=task.confirmation_token)
    assert task.terminal and workflow.session.state == 'unknown'
    assert len(env.inputs) == 1 and env.keys == 0 and env.closes == 1


def test_no_final_console_authority_before_stopped_and_foreign_console_tokens_are_harmless(setup):
    task, preview, workflow, env = setup
    stopped_wait(task, env)
    with pytest.raises(RecordingTaskError):
        task.confirm_console(task_id=task.task_id, session_id=preview.session.name, step_token='fake')
    assert env.queries == [] and env.binding_checks == 0 and env.closes == 0
    task.confirm_stopped(task_id=task.task_id, step_token=task.confirmation_token)
    before = task.snapshot()
    for task_id, session_id, token in [('foreign', preview.session.name, preview.console_step_token),
        (task.task_id, 'other-session', preview.console_step_token),
        (task.task_id, preview.session.name, 'old-console-step')]:
        with pytest.raises(RecordingTaskError):
            task.confirm_console(task_id=task_id, session_id=session_id, step_token=token)
        assert task.snapshot() == before and env.queries == [] and env.closes == 0


@pytest.mark.parametrize('phase', ['armed', 'waiting_start', 'started_wait', 'playing', 'stopped_wait', 'final_console'])
def test_cancel_at_each_phase_consumes_authorization_and_does_not_toggle_again(setup, phase):
    task, preview, workflow, env = setup
    if phase == 'armed': task.arm()
    elif phase == 'waiting_start':
        task.arm()
        task.confirm_not_recording(task_id=task.task_id, step_token=task.confirmation_token)
    elif phase == 'started_wait': authorize_start(task, env)
    elif phase == 'playing': playing(task, env)
    else:
        stopped_wait(task, env)
        if phase == 'final_console':
            task.confirm_stopped(task_id=task.task_id, step_token=task.confirmation_token)
    count, keys = len(env.inputs), env.keys
    task.cancel()
    assert task.terminal and env.closes == env.restores == 1
    assert len(env.inputs) == count and env.keys == keys
    assert workflow.session.state == ('stopped' if phase == 'final_console' else 'unknown' if count else 'cancelled')
    task.cancel()
    task.poll()
    assert len(env.inputs) == count and env.closes == 1


def test_terminal_waits_for_preview_close_and_restore_to_settle(setup):
    task, preview, _workflow, env = setup
    task.arm()
    preview.close_polls = 2
    task.cancel()
    assert task.state == 'closing' and not task.terminal and env.restores == 0
    task.poll()
    assert not task.terminal and env.restores == 0
    task.poll()
    assert task.terminal and env.restores == 1 and task.state == 'complete'
    assert env.closes == 1


def test_snapshot_exposes_serializable_independent_status_and_is_not_mutable_authority(setup):
    import json
    task, _preview, workflow, env = setup
    authorize_start(task, env)
    value = task.snapshot()
    assert json.loads(json.dumps(value, allow_nan=False)) == value
    value['state'] = 'stopped'
    value['confirmation_token'] = 'fake'
    assert task.state == 'awaiting_started' and task.confirmation_token != 'fake'
    task.cancel()
    value = task.snapshot()
    assert value['state'] == 'complete' and value['recording_state'] == 'unknown'
    assert value['restore_status'] == 'complete' and value['cleanup_requested'] is True


def test_unknown_cancel_explicitly_preserves_manual_nvidia_stop_instruction(setup):
    task, _preview, workflow, env = setup
    playing(task, env)
    task.cancel()
    assert workflow.session.state == 'unknown'
    assert 'NVIDIA' in task.error and '停止' in task.error
    assert task.snapshot()['restore_status'] == 'complete'


def test_game_identity_changes_during_recording_never_send_stop_to_new_process(setup):
    task, _preview, workflow, env = setup
    playing(task, env)
    env.identity = replace(env.identity, created=124)
    task.poll()
    assert task.terminal and workflow.session.state == 'unknown'
    assert len(env.inputs) == 1 and env.keys == 1 and env.closes == 1


def test_final_query_wrong_nonce_keeps_confirmed_stopped_and_closes_without_retry(setup):
    task, preview, workflow, env = setup
    stopped_wait(task, env)
    def invalid(_session, identity, _nonce, *, expected, clock):
        proof = SimpleNamespace(process_identity=identity, key='F8', request_nonce='foreign',
                                value='', observed_at=clock())
        return SimpleNamespace(readback=lambda: proof)
    preview.playback.reader_factory = invalid
    finish(task, preview, env)
    assert task.terminal and task.error and workflow.session.state == 'stopped'
    assert len(env.queries) == 1 and len(env.inputs) == 2 and env.keys == 1
    assert env.closes == env.restores == 1


def test_checkpoint_failure_and_close_failure_are_recorded_independently(setup):
    task, preview, workflow, env = setup
    env.checkpoint_failure = lambda _value: (_ for _ in ()).throw(OSError('task disk full'))
    def failed_close():
        env.closes += 1
        preview.state = 'recovery_blocked'
        preview.error = 'owned game close unconfirmed'
        raise OSError(preview.error)
    preview.stop = failed_close
    with pytest.raises(RecordingTaskError): task.arm()
    assert task.terminal and task.state == 'recovery_blocked' and not task.durable
    assert 'task disk full' in task.error and 'owned game close unconfirmed' in task.error
    assert env.closes == 1 and env.restores == 0 and env.inputs == []


def test_final_checkpoint_failure_after_restoration_is_not_durable_success(setup):
    task, preview, workflow, env = setup
    stopped_wait(task, env)
    def failed_final(value):
        if value['terminal']:
            raise OSError('final task checkpoint failed')
    env.checkpoint_failure = failed_final
    finish(task, preview, env)
    assert task.terminal and not task.durable and task.error
    assert workflow.session.state == 'stopped' and preview.state == 'complete'
    assert env.closes == env.restores == 1 and len(env.inputs) == 2 and env.keys == 1


def test_terminal_checkpoints_are_compatible_with_independent_recovery_scanner(setup):
    import json
    from cs2pov.services.recording_recovery import scan_recording_recovery
    task, preview, _workflow, env = setup
    stopped_wait(task, env)
    finish(task, preview, env)
    for name, value in [('recording-session.json', asdict(env.recording_checkpoints[-1])),
                        ('recording-workflow.json', asdict(env.contexts[-1])),
                        ('recording-task.json', env.task_checkpoints[-1])]:
        (preview.session/name).write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
    # The scanner's root is the configured data directory, not sessions itself.
    assert scan_recording_recovery(preview.session.parent.parent) == []
