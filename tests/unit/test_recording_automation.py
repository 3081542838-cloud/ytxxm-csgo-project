"""Bridge evidence tests use real tasks and never send native input."""
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from cs2pov.adapters.nvidia_status import (
    NvidiaStatusEvidence, OverlayNode, OverlaySnapshot, OverlayWindow,
    classify_snapshot,
)
from cs2pov.adapters.owned_process import ProcessIdentity
from tests import recording_support


OVERLAY = ProcessIdentity(20616, 134355244961389762,
    'C:/Program Files/NVIDIA Corporation/NVIDIA App/CEF/NVIDIA Overlay.exe')


class Monitor:
    """Controlled queued observation delivery, with optional stale callbacks."""

    def __init__(self):
        self.busy = False
        self.jobs = []
        self.cancelled = 0

    def start(self, request, callback):
        if self.busy:
            return False
        self.busy = True
        self.jobs.append((request, callback))
        return True

    def complete(self, proof):
        request, callback = self.jobs[-1]
        self.busy = False
        callback(proof)

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from cs2pov.services import automatic_recording, recording_automation
    holder = {}
    original_workflow = recording_support.RecordingWorkflow

    def workflow(*args, **kwargs):
        kwargs['input_guard'] = lambda expected: holder['runner'].input_guard(expected)
        return original_workflow(*args, **kwargs)

    monkeypatch.setattr(recording_support, 'RecordingWorkflow', workflow)
    task, preview, workflow, env = recording_support.recording_setup(tmp_path)
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

    def query_window(hwnd):
        return automatic_recording.OverlayWindowIdentity(
            hwnd, env.overlay.pid, env.overlay_visible)

    class AutomaticWithObservedProcess(automatic_recording.AutomaticRecording):
        def __init__(self, *args, **kwargs):
            kwargs['identity_query'] = lambda pid: env.overlay
            kwargs['window_query'] = query_window
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(recording_automation, 'AutomaticRecording', AutomaticWithObservedProcess)
    monkeypatch.setattr(recording_automation, 'process_identity', lambda pid: env.overlay)
    monkeypatch.setattr(recording_automation, 'window_identity', query_window)
    monkeypatch.setattr(recording_automation, 'classify_snapshot',
        lambda snapshot, request, **kwargs: classify_snapshot(snapshot, request,
            identity_query=lambda pid: env.overlay, **kwargs))
    monitor = Monitor()
    runner = recording_automation.RecordingAutomation(task, monitor, checkpoint,
        identity=OVERLAY, guard=lambda: env.component_ok, clock=lambda: env.now)
    holder['runner'] = runner
    return SimpleNamespace(runner=runner, task=task, preview=preview,
        workflow=workflow, env=env, monitor=monitor, module=recording_automation)


def proof_for(setup, request, state):
    env = setup.env
    if state == 'unknown':
        return NvidiaStatusEvidence('unknown', 'no_visible_overlay_window', request.request_id)
    env.now += .05
    nodes = (
        OverlayNode(None, 'NVIDIA', 50032, False, True, OVERLAY.pid),
        OverlayNode(0, '录制', 50026, False, True, OVERLAY.pid),
        OverlayNode(1, '开始' if state == 'idle' else '停止', 50000,
                    False, True, OVERLAY.pid),
    )
    snapshot = OverlaySnapshot(request.request_id, request.requested_at,
        env.now, True, (OverlayWindow(env.overlay_hwnd, OVERLAY, True, nodes),), '')
    result = classify_snapshot(snapshot, request, now=env.now,
                               identity_query=lambda pid: OVERLAY)
    assert result.state == state
    return result


def observe(setup, state):
    setup.runner.poll()
    assert setup.monitor.busy
    request = setup.monitor.jobs[-1][0]
    result = proof_for(setup, request, state)
    setup.monitor.complete(result)
    return result


def started(setup):
    env, task = setup.env, setup.task
    task.arm()
    idle = observe(setup, 'idle')
    assert task.state == 'awaiting_start_foreground'
    recording_support.fresh(env)
    env.foreground = env.identity.pid
    task.poll()
    assert task.state == 'awaiting_started' and env.inputs == ['Alt+F9']
    observe(setup, 'recording')
    assert task.state == 'awaiting_play_foreground'
    recording_support.fresh(env)
    task.poll()
    recording_support.fresh(env, moving=True)
    task.poll()
    assert task.state == 'recording'
    return idle


def test_fresh_idle_start_recording_and_new_idle_complete_existing_task(setup):
    started(setup)
    # A new observed recording state while playing supplies the stop guard.
    observe(setup, 'recording')
    recording_support.fresh(setup.env, end=True)
    setup.task.poll()
    assert setup.task.state == 'awaiting_stopped'
    observe(setup, 'idle')
    assert setup.workflow.session.state == 'stopped'
    assert setup.task.state == 'awaiting_end_console'
    assert setup.env.inputs == ['Alt+F9', 'Alt+F9'] and setup.env.keys == 1
    assert [row['action'] for row in setup.env.journals if row['status'] == 'consumed'] == [
        'confirm_not_recording', 'confirm_started', 'confirm_stopped']
    assert setup.env.launches == 0 and setup.env.closes == 0


def test_recording_observed_before_this_start_cannot_authorize_stop(setup):
    idle = started(setup)
    request = setup.monitor.jobs[-1][0]
    old = replace(setup.runner.latest.snapshot,
        started_at=setup.workflow.session.start_attempted_at-.05,
        observed_at=setup.workflow.session.start_attempted_at)
    # Keep the evidence fresh enough: only the start provenance rejects it.
    request = replace(request, requested_at=old.started_at)
    setup.runner.request = request
    setup.runner.latest = replace(setup.runner.latest, observed_at=old.observed_at,
                                 snapshot=old)
    recording_support.fresh(setup.env, end=True)
    setup.task.poll()
    assert setup.task.cleanup_requested and setup.workflow.session.state == 'unknown'
    assert setup.workflow.session.stop_attempted_at is None
    assert setup.env.inputs == ['Alt+F9']


def test_manual_nvidia_stop_during_playback_cancels_without_inverse_toggle(setup):
    started(setup)
    observe(setup, 'idle')
    assert setup.task.cleanup_requested and setup.task.terminal
    assert '禁止' in setup.runner.error
    assert setup.env.inputs == ['Alt+F9']
    assert setup.workflow.session.stop_attempted_at is None
    setup.task.poll()
    setup.runner.poll()
    assert setup.env.inputs == ['Alt+F9']


def test_unknown_result_revokes_previous_fresh_idle_and_cannot_start(setup):
    setup.task.arm()
    observe(setup, 'idle')
    assert setup.runner.input_guard('idle') is True
    observe(setup, 'unknown')
    assert setup.runner.latest.state == 'unknown'
    assert setup.runner.input_guard('idle') is False
    recording_support.fresh(setup.env)
    setup.env.foreground = setup.env.identity.pid
    setup.task.poll()
    assert setup.task.cleanup_requested and setup.env.inputs == []
    assert setup.workflow.session.start_attempted_at is None


def test_unknown_during_recording_revokes_stop_guard_without_second_toggle(setup):
    started(setup)
    observe(setup, 'unknown')
    recording_support.fresh(setup.env, end=True)
    setup.task.poll()
    assert setup.task.cleanup_requested and setup.env.inputs == ['Alt+F9']
    assert setup.workflow.session.stop_attempted_at is None


def test_fresh_recording_after_stop_deadline_never_extends_original_two_seconds(setup):
    started(setup)
    setup.env.foreground = 999
    recording_support.fresh(setup.env, end=True)
    setup.task.poll()
    assert setup.task.state == 'awaiting_stop_foreground'
    deadline = setup.task._input_deadline
    setup.env.now = deadline + .01
    observe(setup, 'recording')
    assert setup.env.now - setup.runner.latest.observed_at < 2
    assert setup.runner.input_guard('recording') is False
    setup.env.foreground = setup.env.identity.pid
    recording_support.fresh(setup.env, end=True)
    setup.task.poll()
    assert setup.task.cleanup_requested and setup.env.inputs == ['Alt+F9']
    assert setup.task._input_deadline is None


def test_failed_confirmed_journal_blocks_fresh_idle_guard_after_task_advance(setup):
    setup.task.arm()
    def fail(row):
        if row['status'] == 'confirmed':
            raise OSError('confirmed journal write failed')
    setup.env.journal_hook = fail
    observe(setup, 'idle')
    assert setup.runner.coordinator.blocked
    assert setup.task.cleanup_requested and setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


def test_coordinator_revocation_alone_rejects_cached_fresh_idle_without_task_cleanup(setup):
    setup.task.arm()
    observe(setup, 'idle')
    assert setup.runner.input_guard('idle') is True
    setup.runner.coordinator.cancel()
    assert setup.task.state == 'awaiting_start_foreground'
    assert not setup.task.cleanup_requested
    assert setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


def test_component_read_failure_returns_false_without_toggle_or_confirmation(setup):
    setup.task.arm()
    observe(setup, 'idle')
    def fail_guard():
        raise OSError('locked component unavailable')
    setup.runner.guard = fail_guard
    assert setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


def test_cancel_and_late_valid_callback_do_not_advance_task_or_revive_evidence(setup):
    setup.task.arm()
    setup.runner.poll()
    request, callback = setup.monitor.jobs[-1]
    result = proof_for(setup, request, 'idle')
    before = setup.task.snapshot()
    setup.runner.cancel()
    callback(result)  # Simulates a worker that returns after cancellation.
    assert setup.task.snapshot() == before
    assert setup.runner.latest is None and setup.runner.input_guard('idle') is False
    assert setup.monitor.cancelled == 1 and setup.env.journals == []
    assert setup.env.inputs == []


@pytest.mark.parametrize('change', ['task_id', 'session_id', 'scope', 'game'])
def test_late_callback_for_changed_task_generation_is_ignored(setup, change):
    setup.task.arm()
    setup.runner.poll()
    request, callback = setup.monitor.jobs[-1]
    result = proof_for(setup, request, 'idle')
    if change in ('task_id', 'session_id'):
        setattr(setup.task, change, 'foreign-generation')
    elif change == 'scope':
        setup.task.scope = replace(setup.task.scope, player_id='202')
    else:
        setup.task.expected_identity = replace(setup.task.expected_identity, created=456)
    callback(result)
    assert setup.task.state == 'awaiting_not_recording'
    assert setup.runner.latest is None and setup.env.journals == []
    assert setup.env.inputs == []


@pytest.mark.parametrize('change', ['stale', 'future', 'request', 'scope', 'overlay',
    'hidden_window', 'foreign_window', 'component', 'component_truthy', 'game'])
def test_input_guard_rechecks_live_contract_and_never_trusts_unchanged_cache(setup, change, monkeypatch):
    setup.task.arm()
    observe(setup, 'idle')
    assert setup.runner.input_guard('idle') is True
    env = setup.env
    if change == 'stale':
        env.now += 2.01
    elif change == 'future':
        setup.runner.latest = replace(setup.runner.latest, observed_at=env.now+1)
    elif change == 'request':
        setup.runner.request = replace(setup.runner.request, request_id='f'*32)
    elif change == 'scope':
        setup.preview.draft['selection']['player_id'] = '202'
    elif change == 'overlay':
        env.overlay = replace(OVERLAY, created=OVERLAY.created+1)
    elif change == 'hidden_window':
        env.overlay_visible = False
    elif change == 'foreign_window':
        monkeypatch.setattr(setup.module, 'window_identity',
            lambda hwnd: SimpleNamespace(visible=True, pid=OVERLAY.pid+1))
    elif change == 'component':
        env.component_ok = False
    elif change == 'component_truthy':
        env.component_ok = 1
    elif change == 'game':
        setup.task.expected_identity = replace(setup.task.expected_identity, created=456)
    assert setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


def test_poll_does_not_start_parallel_workers_or_send_input_from_observer(setup):
    setup.task.arm()
    setup.runner.poll()
    setup.runner.poll()
    assert len(setup.monitor.jobs) == 1 and setup.env.inputs == []
    setup.monitor.complete(proof_for(setup, setup.monitor.jobs[-1][0], 'recording'))
    assert setup.task.state == 'awaiting_not_recording'
    assert setup.env.inputs == [] and setup.env.journals == []


def test_component_check_crossing_task_deadline_cannot_authorize_input(setup):
    setup.task.arm()
    observe(setup, 'idle')
    setup.env.now = setup.task._input_deadline - .05
    # Keep evidence fresh at entry while a slow component check expires the task.
    observe(setup, 'idle')
    setup.task._input_deadline = setup.env.now + .05
    def slow_guard():
        setup.env.now += .1
        return True
    setup.runner.guard = slow_guard
    assert setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


@pytest.mark.parametrize('change', ['scope', 'cancel', 'blocked', 'identity',
    'task_id', 'session_id', 'hotkey', 'deadline', 'task_reference'])
def test_contract_change_during_component_read_is_rechecked_before_input(setup, change):
    setup.task.arm()
    observe(setup, 'idle')
    def changing_guard():
        if change == 'scope':
            setup.preview.draft['selection']['player_id'] = '202'
        elif change == 'cancel':
            setup.runner.active = False
        elif change == 'blocked':
            setup.runner.coordinator.cancel()
        elif change == 'identity':
            setup.task.expected_identity = replace(setup.task.expected_identity, created=456)
        elif change in ('task_id', 'session_id'):
            setattr(setup.task, change, 'foreign-task')
        elif change == 'hotkey':
            setup.task.hotkey = 'Ctrl+F9'
        elif change == 'deadline':
            setup.task._input_deadline = None
        else:
            setup.runner.task = SimpleNamespace()
        return True
    setup.runner.guard = changing_guard
    assert setup.runner.input_guard('idle') is False
    assert setup.env.inputs == []


def test_real_qt_monitor_late_cancelled_generation_never_confirms_bridge_task(setup, qtbot):
    from cs2pov.services.nvidia_monitor import NvidiaMonitor
    entered, release = threading.Event(), threading.Event()
    def observer(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return proof_for(setup, kwargs['request'], 'idle')
    monitor = NvidiaMonitor(observer=observer, clock=lambda: setup.env.now)
    setup.runner.monitor = monitor
    setup.task.arm()
    before = setup.task.snapshot()
    try:
        setup.runner.poll()
        qtbot.waitUntil(entered.is_set, timeout=2000)
        setup.runner.cancel()
        release.set()
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert setup.task.snapshot() == before
        assert setup.runner.latest is None and setup.env.journals == []
        assert setup.env.inputs == []
    finally:
        release.set()
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        monitor.deleteLater()


def test_real_qt_monitor_foreign_nonce_becomes_unknown_without_confirmation(setup, qtbot):
    from cs2pov.services.nvidia_monitor import NvidiaMonitor
    def observer(*args, **kwargs):
        return replace(proof_for(setup, kwargs['request'], 'idle'), request_id='f'*32)
    monitor = NvidiaMonitor(observer=observer, clock=lambda: setup.env.now)
    setup.runner.monitor = monitor
    setup.task.arm()
    try:
        setup.runner.poll()
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert setup.runner.latest.state == 'unknown'
        assert setup.task.state == 'awaiting_not_recording'
        assert setup.env.journals == [] and setup.env.inputs == []
    finally:
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        monitor.deleteLater()
