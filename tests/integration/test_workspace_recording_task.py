"""Default Workspace recording wiring with real SQLite and atomic JSON.

No native input, game launch, original Demo copying, video probing or window
focus is permitted. The held preview uses real replay/hidden-playback checks.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pytest

from cs2pov.adapters.disk import DiskSnapshot, MIN_VIDEO_BYTES
from cs2pov.services.recording_recovery import scan_recording_recovery
from cs2pov.services.workspace import Workspace
from cs2pov.storage.transaction import atomic_write
from tests.recording_support import recording_setup, fresh


@pytest.fixture
def desktop(tmp_path, monkeypatch, qapp):
    _unused_task, preview, _unused_workflow, env = recording_setup(tmp_path)
    data = preview.session.parent.parent
    journal = dict(version=1, prepared=True, complete=False, entries={'gameinfo': dict(
        original=hashlib.sha256(b'original gameinfo').hexdigest(),
        deployed=hashlib.sha256(b'temporary gameinfo').hexdigest(), attributes=32,
        state='deployed', write_intent=True, protect_readonly=False)})

    def save_journal():
        atomic_write(preview.session/'journal.json', json.dumps(journal).encode('utf-8'))

    save_journal()
    finish_close = preview.finish_close
    def restore_owned_preview():
        finish_close()
        journal['complete'] = True
        journal['entries']['gameinfo']['state'] = 'restored'
        journal['entries']['gameinfo']['write_intent'] = False
        save_journal()
    preview.finish_close = restore_owned_preview

    class Nvidia:
        def __init__(self, *, game):
            assert game is preview.game
            self.game = game

        def send_once(self, key, game=None, *, expected_identity):
            assert game is self.game and game.verify() == expected_identity
            assert env.foreground == expected_identity.pid
            recorder = json.loads((preview.session/'recording-session.json').read_text(encoding='utf-8'))
            task = json.loads((preview.session/'recording-task.json').read_text(encoding='utf-8'))
            context = json.loads((preview.session/'recording-workflow.json').read_text(encoding='utf-8'))
            assert recorder['state'] in ('start_pending', 'stop_pending')
            assert recorder['expected_identity'] == asdict(expected_identity)
            assert recorder['confirmation_token'] is None
            assert task['state'] in ('awaiting_start_foreground', 'awaiting_stop_foreground')
            assert context['started_wall'] is not None
            env.inputs.append(key)
            env.events.append(('NVIDIA', key))

    monkeypatch.setattr('cs2pov.adapters.nvidia.Win32NvidiaHotkey', Nvidia)
    model = Workspace(data, disk_check=lambda path: DiskSnapshot(Path(path), MIN_VIDEO_BYTES+1, MIN_VIDEO_BYTES))
    # Simulate the already registered, prepared preview handed to stage 7.
    model.settings = preview.settings
    model.settings_file.save(asdict(model.settings))
    with model.library.db:
        model.library.db.execute('INSERT INTO sessions VALUES (?, ?)', (preview.session.name, preview.state))
    model._preview = preview
    model.set_busy(True, 'held prepared preview')
    yield model, preview, env
    if model._recording_task is not None:
        model._recording_task.cancel()
        model.poll_recording()
    model._preview_timer.stop()
    model.library.close()


def begin_recording(model, env):
    model.start_recording()
    task = model._recording_task
    assert task.state == 'awaiting_not_recording' and model.busy
    model.confirm_recording_state(task_id=task.task_id, state=task.state,
                                  step_token=task.confirmation_token)
    assert env.inputs == []
    fresh(env)
    env.foreground = env.identity.pid
    model.poll_recording()
    assert task.state == 'awaiting_started' and len(env.inputs) == 1
    return task


def test_capture_handoff_starts_direct_recording_and_playback_once(desktop):
    model, preview, env = desktop
    model.automatic_recording = True
    model._auto_capture_requested = True
    env.foreground = env.identity.pid
    model.poll_preview()
    assert model._recording_task is not None
    assert not model._auto_capture_requested
    assert (preview.session / 'recording-task.json').is_file()
    for _ in range(6):
        fresh(env)
        model.poll_preview()
    assert env.inputs == ['Alt+F9']
    assert env.keys == 1
    assert model._recording_task.state == 'recording'


def test_default_factory_success_uses_same_preview_game_scope_and_three_atomic_checkpoints(desktop, qtbot):
    model, preview, env = desktop
    before = Path(preview.draft['demo']).read_bytes()
    task = begin_recording(model, env)
    assert task.preview is preview and task.workflow.session.game is preview.game
    assert task.scope.demo == preview.draft['demo']
    assert task.expected_identity == env.identity
    env.foreground = 999
    model.confirm_recording_state(task_id=task.task_id, state=task.state,
                                  step_token=task.confirmation_token)
    assert env.keys == 0 and model.busy
    fresh(env)
    env.foreground = env.identity.pid
    model.poll_recording()
    assert env.keys == 1
    fresh(env, moving=True)
    model.poll_recording()
    fresh(env, end=True)
    model.poll_recording()
    assert task.state == 'awaiting_stopped' and len(env.inputs) == 2
    assert model.busy and env.queries == []
    env.foreground = 999
    model.confirm_recording_state(task_id=task.task_id, state=task.state,
                                  step_token=task.confirmation_token)
    assert task.state == 'awaiting_end_console' and model.busy and env.queries == []
    model.confirm_preview_console(task_id=task.task_id, session_id=preview.session.name,
                                  step_token=preview.console_step_token)
    assert env.queries == [] and model.busy
    env.foreground = env.identity.pid
    model.poll_recording()
    assert len(env.queries) == 1 and model.busy
    model.poll_recording()
    assert model._recording_task is None and model._output_process is not None and model.busy
    qtbot.waitUntil(lambda: model._output_process is None, timeout=15000)
    assert model._recording_task is None and model._preview is None and not model.busy
    assert model._last_recording_task is task and task.terminal and task.durable
    assert task.error == '' and task.workflow.session.state == 'stopped'
    assert env.launches == 0 and env.closes == env.restores == 1
    assert env.inputs == ['Alt+F9', 'Alt+F9'] and env.keys == 1
    assert model.library.unfinished() == [] and model.library.records() == []
    assert model.recovery == [] and model.nvidia_recovery_items == []
    assert scan_recording_recovery(model.directory) == []
    for name in ('recording-session.json', 'recording-workflow.json', 'recording-task.json'):
        assert (preview.session/name).is_file()
    assert json.loads((preview.session/'recording-task.json').read_text(encoding='utf-8')) == task.snapshot()
    assert list(preview.session.glob('.*.tmp')) == []
    assert list(preview.session.rglob('*.dem')) == []
    assert Path(preview.draft['demo']).read_bytes() == before


def test_default_factory_cancel_after_start_keeps_nvidia_unknown_after_files_complete(desktop):
    model, preview, env = desktop
    task = begin_recording(model, env)
    model.cancel()
    assert task.terminal and preview.state == 'complete' and task.workflow.session.state == 'unknown'
    assert not model.busy and model._recording_task is None
    assert len(env.inputs) == 1 and env.keys == 0 and env.closes == env.restores == 1
    assert model.library.unfinished() == [] and model.recovery == []
    assert model.nvidia_recovery_items and scan_recording_recovery(model.directory)
    checkpoint = json.loads((preview.session/'recording-session.json').read_text(encoding='utf-8'))
    assert checkpoint['state'] == 'unknown' and checkpoint['triggered'] == 'start'
    assert checkpoint['confirmation_token'] is None
    with pytest.raises(ValueError, match='NVIDIA'):
        model.preparation_gate()
    model.cancel()
    task.poll()
    assert len(env.inputs) == 1 and env.keys == 0 and env.closes == 1


def test_default_factory_database_failure_after_start_closes_and_preserves_both_recovery_states(desktop):
    model, preview, env = desktop
    model.start_recording()
    task = model._recording_task
    model.confirm_recording_state(task_id=task.task_id, state=task.state,
                                  step_token=task.confirmation_token)
    model.library.db.execute("CREATE TRIGGER fail_recording_update BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'recording database fault'); END")
    fresh(env)
    env.foreground = env.identity.pid
    model.poll_recording()
    assert task.terminal and preview.state == 'complete' and not model.busy
    assert task.workflow.session.state == 'unknown' and len(env.inputs) == 1 and env.keys == 0
    assert env.closes == env.restores == 1
    assert model.library.unfinished() == [preview.session.name]
    assert model.recovery and model.nvidia_recovery_items
    assert 'recording database fault' in model.recording_status
    assert json.loads((preview.session/'journal.json').read_text())['complete'] is True
    assert json.loads((preview.session/'recording-session.json').read_text(encoding='utf-8'))['state'] == 'unknown'
    events = list(env.events)
    model.poll_recording()
    assert env.events == events
