"""Actual Task transitions accept only fresh, task-bound read-only UIA evidence."""
from dataclasses import replace
import importlib
from types import SimpleNamespace

import pytest

from cs2pov.adapters.nvidia_status import (NvidiaStatusEvidence, OverlayNode,
    OverlayWindow, OverlaySnapshot, classify_snapshot)
from cs2pov.adapters.owned_process import ProcessIdentity
from tests.recording_support import recording_setup, fresh


OVERLAY = ProcessIdentity(20616, 134355244961389762,
    'C:/Program Files/NVIDIA Corporation/NVIDIA App/CEF/NVIDIA Overlay.exe')


@pytest.fixture
def setup(tmp_path):
    # Import in the fixture so the first red run retains per-test evidence.
    module = importlib.import_module('cs2pov.services.automatic_recording')
    task, preview, workflow, env = recording_setup(tmp_path)
    env.overlay = OVERLAY
    env.overlay_hwnd = 459430
    env.overlay_visible = True
    env.component_ok = True
    env.journals = []
    env.journal_hook = None
    def checkpoint(value):
        env.journals.append(value)
        if env.journal_hook is not None:
            env.journal_hook(value)
    guard = module.AutomaticRecording(task, overlay_identity=OVERLAY, checkpoint=checkpoint,
        component_guard=lambda: env.component_ok,
        identity_query=lambda pid: env.overlay,
        window_query=lambda hwnd: module.OverlayWindowIdentity(hwnd, env.overlay.pid, env.overlay_visible),
        clock=lambda: env.now)
    return guard, task, preview, workflow, env, module


def evidence(guard, env, state, *, step=None):
    step = guard.capture() if step is None else step
    env.now += .05
    nodes = (OverlayNode(None, 'NVIDIA', 50032, False, True, OVERLAY.pid),
             OverlayNode(0, '录制', 50026, False, True, OVERLAY.pid),
             OverlayNode(1, '开始' if state == 'idle' else '停止', 50000, False, True, OVERLAY.pid))
    snapshot = OverlaySnapshot(step.request.request_id, step.request.requested_at,
        env.now, True, (OverlayWindow(env.overlay_hwnd, OVERLAY, True, nodes),), '')
    proof = classify_snapshot(snapshot, step.request, now=env.now,
                              identity_query=lambda pid: OVERLAY)
    assert proof.state == state
    return step, proof


def auto_start(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    guard.accept(step, proof)
    assert task.state == 'awaiting_start_foreground' and env.inputs == []
    fresh(env)
    env.foreground = env.identity.pid
    task.poll()
    assert task.state == 'awaiting_started' and env.inputs == ['Alt+F9']
    step, proof = evidence(guard, env, 'recording')
    guard.accept(step, proof)
    assert task.state == 'awaiting_play_foreground' and workflow.session.state == 'recording'
    return setup


def auto_stop_wait(setup):
    guard, task, preview, workflow, env, module = auto_start(setup)
    fresh(env)
    task.poll()
    fresh(env, moving=True)
    task.poll()
    fresh(env, end=True)
    task.poll()
    assert task.state == 'awaiting_stopped' and env.inputs == ['Alt+F9', 'Alt+F9']
    return setup


def test_automatic_actual_task_chain_persists_fresh_proof_before_each_confirmation(setup):
    guard, task, preview, workflow, env, module = auto_stop_wait(setup)
    step, proof = evidence(guard, env, 'idle')
    guard.accept(step, proof)
    assert workflow.session.state == 'stopped' and task.state == 'awaiting_end_console'
    assert len(env.inputs) == 2 and env.keys == 1
    assert [row['action'] for row in env.journals if row['status'] == 'consumed'] == [
        'confirm_not_recording', 'confirm_started', 'confirm_stopped']
    assert guard.snapshot()['recording_request_id'] and guard.snapshot()['stopped_request_id']
    assert len({row['request_id'] for row in env.journals if row['status'] == 'consumed'}) == 3
    assert env.closes == 0 and env.launches == 0


@pytest.mark.parametrize('mutate', [
    lambda proof: replace(proof, state='unknown', reason='conflicting_ui_states'),
    lambda proof: replace(proof, reason='other_source'),
    lambda proof: replace(proof, source='file_or_registry_guess'),
    lambda proof: replace(proof, request_id='f'*32),
    lambda proof: replace(proof, identity=replace(OVERLAY, created=OVERLAY.created+1)),
    lambda proof: replace(proof, hwnd=123),
    lambda proof: replace(proof, observed_at=proof.observed_at-3),
    lambda proof: replace(proof, observed_at=proof.observed_at+1),
    lambda proof: replace(proof, snapshot=None),
])
def test_missing_stale_forged_unknown_wrong_source_or_identity_never_confirms(setup, mutate):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    before = task.snapshot()
    with pytest.raises(module.AutomaticRecordingError):
        guard.accept(step, mutate(proof))
    assert task.snapshot() == before and env.inputs == [] and env.journals == []


@pytest.mark.parametrize('state', ['idle', 'recording'])
def test_idle_and_recording_are_not_interchangeable_confirmations(setup, state):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    if state == 'idle':
        step, proof = evidence(guard, env, 'idle'); guard.accept(step, proof)
        fresh(env); env.foreground = env.identity.pid; task.poll()
    step, proof = evidence(guard, env, state)
    before = task.snapshot()
    with pytest.raises(module.AutomaticRecordingError):
        guard.accept(step, proof)
    assert task.snapshot() == before


def test_expired_replaced_or_cancelled_step_never_authorizes_new_task(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    old, proof = evidence(guard, env, 'idle')
    guard.capture()
    with pytest.raises(module.AutomaticRecordingError): guard.accept(old, proof)
    current, proof = evidence(guard, env, 'idle')
    task.cancel()
    with pytest.raises(module.AutomaticRecordingError): guard.accept(current, proof)
    assert env.inputs == []


def test_request_nonce_and_confirmations_are_consumed_once_without_retry(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    guard.accept(step, proof)
    before = task.snapshot()
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert task.snapshot() == before and env.inputs == []


@pytest.mark.parametrize('change', ['scope', 'game', 'overlay', 'window', 'component', 'token'])
def test_identity_scope_component_window_or_step_change_during_persistence_blocks_confirmation(setup, change):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    def mutate(row):
        if row['status'] != 'consumed': return
        if change == 'scope': preview.draft['selection']['player_id'] = '202'
        elif change == 'game': env.identity = replace(env.identity, created=124)
        elif change == 'overlay': env.overlay = replace(OVERLAY, created=OVERLAY.created+1)
        elif change == 'window': env.overlay_visible = False
        elif change == 'component': env.component_ok = False
        elif change == 'token': workflow.session.confirmation_token = 'foreign'
    env.journal_hook = mutate
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert task.state == 'awaiting_not_recording' and env.inputs == []


def test_journal_failure_is_sticky_and_cannot_be_retried_as_confirmation(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    def fail(row): raise OSError('journal unavailable after possible write')
    env.journal_hook = fail
    with pytest.raises(module.AutomaticRecordingError, match='保存|持久'):
        guard.accept(step, proof)
    env.journal_hook = None
    with pytest.raises(module.AutomaticRecordingError): guard.capture()
    assert task.state == 'awaiting_not_recording' and env.inputs == []


def test_slow_journal_cannot_extend_evidence_freshness_or_task_deadline(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    step, proof = evidence(guard, env, 'idle')
    env.journal_hook = lambda row: setattr(env, 'now', env.now + 2.01)
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert task.state == 'awaiting_not_recording' and env.inputs == []


def test_arbitrary_idle_does_not_fill_stop_without_this_instances_recording_transition(setup):
    guard, task, preview, workflow, env, module = setup
    from tests.recording_support import stopped_wait
    stopped_wait(task, env)  # Existing manually confirmed task does not create auto provenance.
    step, proof = evidence(guard, env, 'idle')
    with pytest.raises(module.AutomaticRecordingError, match='转换|本轮|录制中'):
        guard.accept(step, proof)
    assert workflow.session.state == 'awaiting_stopped'


def test_idle_sample_from_before_stop_attempt_cannot_confirm_stopped(setup):
    guard, task, preview, workflow, env, module = auto_stop_wait(setup)
    step, proof = evidence(guard, env, 'idle')
    older = replace(proof.snapshot, started_at=workflow.session.stop_attempted_at-.01,
                    observed_at=workflow.session.stop_attempted_at)
    with pytest.raises(module.AutomaticRecordingError):
        guard.accept(step, replace(proof, snapshot=older, observed_at=older.observed_at))
    assert workflow.session.state == 'awaiting_stopped'


def test_snapshot_and_external_journal_mutation_never_mutate_guard_contract(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle'); guard.accept(step, proof)
    saved = guard.snapshot(); saved['task_id'] = 'foreign'
    env.journals[0]['scope']['player_id'] = '202'
    assert guard.snapshot()['task_id'] == task.task_id and guard.snapshot()['scope']['player_id'] == '101'


def test_same_snapshot_with_conflicting_start_and_stop_cannot_authorize_idle(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    window = proof.snapshot.windows[0]
    conflict = replace(window, nodes=(*window.nodes, OverlayNode(1, '停止', 50000, False, True, OVERLAY.pid)))
    snapshot = replace(proof.snapshot, windows=(conflict,))
    with pytest.raises(module.AutomaticRecordingError):
        guard.accept(step, replace(proof, snapshot=snapshot))
    assert env.journals == [] and env.inputs == []


def test_structurally_valid_wrong_task_step_object_and_none_are_rejected(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    with pytest.raises(module.AutomaticRecordingError): guard.accept(None, proof)
    with pytest.raises(module.AutomaticRecordingError): guard.accept(replace(step, task_id='foreign'), proof)
    assert task.state == 'awaiting_not_recording' and env.journals == []
    guard.accept(step, proof)  # Foreign objects did not consume the real pending step.


def test_current_window_foreign_pid_or_hidden_state_is_not_a_confirmation(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    guard.window_query = lambda hwnd: module.OverlayWindowIdentity(hwnd, OVERLAY.pid+1, True)
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert env.journals == [] and env.inputs == []


def test_observation_cannot_reuse_request_after_clock_deadline_or_extension(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); deadline = workflow.session.deadline
    step, proof = evidence(guard, env, 'idle')
    env.now = deadline
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert workflow.session.deadline == deadline and env.inputs == [] and env.journals == []


def test_identity_change_during_confirm_method_blocks_future_requests_without_retry(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    def mutate(value):
        if value['state'] == 'awaiting_start_foreground':
            env.overlay = replace(OVERLAY, created=OVERLAY.created+1)
    env.checkpoint_failure = mutate
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert task.state == 'awaiting_start_foreground' and guard.blocked
    env.overlay = OVERLAY
    with pytest.raises(module.AutomaticRecordingError): guard.capture()
    assert env.inputs == []


def test_postconfirmation_journal_failure_blocks_even_after_task_advanced(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    def fail(row):
        if row['status'] == 'confirmed': raise OSError('result save uncertain')
    env.journal_hook = fail
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
    assert task.state == 'awaiting_start_foreground' and guard.blocked
    assert env.inputs == []
    with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)


def test_persist_callback_cannot_reenter_capture_or_accept(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step, proof = evidence(guard, env, 'idle')
    rejected = []
    def hook(row):
        with pytest.raises(module.AutomaticRecordingError): guard.capture()
        with pytest.raises(module.AutomaticRecordingError): guard.accept(step, proof)
        rejected.append(row['status'])
    env.journal_hook = hook
    guard.accept(step, proof)
    assert rejected == ['consumed', 'confirmed'] and task.state == 'awaiting_start_foreground'
    assert env.inputs == []


def test_request_count_has_bounded_memory_and_cancellation_revokes_pending_evidence(setup):
    guard, task, preview, workflow, env, module = setup
    task.arm()
    for _ in range(guard.MAX_REQUESTS): guard.capture()
    with pytest.raises(module.AutomaticRecordingError, match='限额'): guard.capture()
    assert guard.blocked and guard.snapshot()['request_count'] == guard.MAX_REQUESTS
    assert env.inputs == []


def test_duplicate_generated_nonce_cannot_issue_another_read_request(setup, monkeypatch):
    guard, task, preview, workflow, env, module = setup
    task.arm(); step = guard.capture()
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=step.request.request_id))
    with pytest.raises(module.AutomaticRecordingError, match='请求|nonce'):
        guard.capture()


def test_component_origin_and_task_reference_are_readonly_and_corrupt_draft_fails_clearly(setup):
    guard, task, preview, workflow, env, module = setup
    with pytest.raises(AttributeError): guard.overlay_identity = replace(OVERLAY, created=123)
    with pytest.raises(AttributeError): guard.task = None
    task.arm()
    preview.draft.clear()
    with pytest.raises(module.AutomaticRecordingError): guard.capture()
    assert env.inputs == []
