"""Actual Qt controls must not bypass the single recording coordinator."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.disk import DiskSnapshot, MIN_VIDEO_BYTES
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.ui.window import MainWindow


class UiTask:
    def __init__(self, preview):
        self.preview = preview
        self.task_id = 'a' * 32
        self.state = 'idle'
        self.confirmation_token = None
        self.terminal = False
        self.durable = True
        self.error = ''
        self.events = []
        self.workflow = SimpleNamespace(session=SimpleNamespace(state='idle'))

    def arm(self):
        self.state = 'awaiting_not_recording'
        self.confirmation_token = 'not-recording'
        self.events.append('arm')

    def confirm(self, task_id, step_token, expected):
        if task_id != self.task_id or step_token != self.confirmation_token or self.state != expected:
            raise DataError('old recording confirmation')
        self.events.append((task_id, step_token))
        self.confirmation_token = None

    def confirm_not_recording(self, *, task_id, step_token):
        self.confirm(task_id, step_token, 'awaiting_not_recording')
        self.state = 'awaiting_start_foreground'

    def confirm_started(self, *, task_id, step_token):
        self.confirm(task_id, step_token, 'awaiting_started')
        self.state = 'awaiting_play_foreground'

    def confirm_stopped(self, *, task_id, step_token):
        self.confirm(task_id, step_token, 'awaiting_stopped')
        self.state = 'awaiting_end_console'
        self.preview.state = 'awaiting_end_console'
        self.preview.console_confirmed = False

    def confirm_console(self, *, task_id, session_id, step_token):
        if task_id != self.task_id or session_id != self.preview.session.name:
            raise DataError('old recording task')
        self.preview.confirm_console(step_token=step_token)

    def snapshot(self):
        return dict(schema=1, task_id=self.task_id, session_id=self.preview.session.name,
                    state=self.state, error=self.error, terminal=self.terminal, durable=self.durable)

    def poll(self):
        self.events.append('poll')

    def cancel(self):
        self.events.append('cancel')
        self.state = 'closing'
        self.preview.state = 'complete'


@pytest.fixture
def recording_desktop(tmp_path, qtbot, monkeypatch):
    def unexpected_warning(*args):
        pytest.fail('Unexpected UI error: ' + str(args[-1]))
    monkeypatch.setattr(QMessageBox, 'warning', unexpected_warning)
    calls = []
    def preview_factory(directory, *args):
        preview = SimpleNamespace(session=directory/'preview-one', state='idle', error='',
            console_confirmed=False, restoration=None, console_step_token='initial-step')
        def start():
            calls.append('start'); preview.state = 'awaiting_console'
        def stop():
            calls.append('stop'); preview.state = 'complete'; preview.restoration = {'gameinfo': 'restored'}
        def confirm(*, step_token=None):
            if step_token != preview.console_step_token:
                raise DataError('old step confirmation')
            calls.append('confirm'); preview.console_confirmed = True
        preview.start, preview.stop, preview.confirm_console = start, stop, confirm
        preview.poll = lambda: calls.append('poll')
        preview.play_clip = lambda: calls.append('play_clip')
        preview.check_playback_binding = lambda: calls.append('check_binding')
        preview.prepare_playback = lambda: calls.append('arm_playback')
        return preview
    model = Workspace(tmp_path/'data', preview_factory=preview_factory,
        disk_check=lambda path: DiskSnapshot(path, MIN_VIDEO_BYTES+1, MIN_VIDEO_BYTES))
    model.save_settings(replace(model.settings, installation=str(tmp_path/'game'),
        cfg=str(tmp_path/'cfg'), video_directory=str(tmp_path)))
    path = tmp_path/'sample.dem'; path.write_bytes(b'original')
    model.import_demo(path)
    draft = model.library.draft(); draft.update(status='parsed', fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    model.analysis = dict(schema=2, map='de_test', tick_rate=64,
        timeline=[[i, i+500] for i in range(129)],
        players=[dict(id='101', name='player', aliases=[], alive=[[0, 128]])],
        rounds=[dict(id=1, number=1, start=1, end=128, complete=True)], deaths=[], kills=[], kill_issues=[])
    model.save_selection('101', 'round', round_id=1)
    window = MainWindow(model)
    def finish(_window):
        # pytest-qt closes widgets before yield-fixture teardown. Finish the fake
        # task here so the real product close guard still applies in every test.
        model._preview_timer.stop()
        task = model._recording_task
        if task is not None:
            task.terminal = True; task.state = 'complete'; task.preview.state = 'complete'
            model.poll_preview()
        elif model._preview is not None:
            model.stop_preview()
        assert not model.busy
    qtbot.addWidget(window, before_close_func=finish)
    window.show(); window.navigate(1)
    model.save_settings(replace(model.settings, nvidia_path_confirmed=True))
    model.start_preview()
    model._preview_timer.stop()
    preview = model._preview
    preview.state = 'play_ready'
    preview.settings = model.settings
    preview.playback = SimpleNamespace(state='ready', triggered=False, binding_may_exist=True)
    made = []
    def factory(current):
        assert current is preview
        task = UiTask(current)
        made.append(task)
        return task
    model._recording_task_factory = factory
    model.changed.emit()
    yield model, window, preview, made, calls
    finish(window)
    model.library.close()


def test_recording_button_attaches_same_preview_and_locks_preview_inputs(recording_desktop, qtbot):
    model, window, preview, made, calls = recording_desktop
    assert window.recording_start.isVisible() and window.recording_start.isEnabled()
    qtbot.mouseClick(window.recording_start, Qt.MouseButton.LeftButton)
    task = made[0]
    assert model._recording_task is task and model._preview is preview and model.busy
    assert calls.count('start') == 1 and task.events.count('arm') == 1
    assert not window.preview_play.isVisible() and not window.preview_binding.isVisible()
    assert window.recording_confirm.isVisible() and window.recording_confirm.isEnabled()
    assert '未录制' in window.recording_confirm.text()
    assert not window.prepare.isEnabled() and not window.save_clip.isEnabled()
    qtbot.mouseClick(window.recording_confirm, Qt.MouseButton.LeftButton)
    assert (task.task_id, 'not-recording') in task.events
    assert task.state == 'awaiting_start_foreground' and not window.recording_confirm.isEnabled()
    for action in (model.play_preview_clip, model.check_preview_binding, model.prepare_preview_playback):
        with pytest.raises(DataError, match='录制'):
            action()
    assert calls.count('play_clip') == calls.count('check_binding') == calls.count('arm_playback') == 0


def test_preview_complete_alone_cannot_unlock_active_recording(recording_desktop):
    model, _, preview, made, _ = recording_desktop
    model.start_recording()
    task = made[0]
    preview.state = 'complete'
    model.poll_preview()
    assert model.busy and model._recording_task is task and model._preview is preview
    task.terminal = True
    task.state = 'complete'
    model.poll_preview()
    assert not model.busy and model._recording_task is None and model._preview is None
    assert model.library.records() == []


def test_recording_confirmation_carries_displayed_token(recording_desktop):
    model, window, _, made, _ = recording_desktop
    model.start_recording()
    task = made[0]
    displayed = window._recording_confirmation
    task.state = 'awaiting_started'
    task.confirmation_token = 'new-started-token'
    with pytest.raises(DataError, match='old recording'):
        window.confirm_recording_state()
    assert window._recording_confirmation == displayed
    assert not any(isinstance(event, tuple) for event in task.events)
    model.changed.emit()
    window.confirm_recording_state()
    assert (task.task_id, 'new-started-token') in task.events


def test_second_recording_task_is_rejected_without_factory(recording_desktop):
    model, _, _, made, _ = recording_desktop
    model.start_recording()
    with pytest.raises(DataError, match='录制'):
        model.start_recording()
    assert len(made) == 1


def test_unconfirmed_nvidia_path_never_arms_task(recording_desktop):
    model, _, _, made, _ = recording_desktop
    model.settings = replace(model.settings, nvidia_path_confirmed=False)
    with pytest.raises(DataError, match='目录'):
        model.start_recording()
    assert made == []


def test_cancel_uses_coordinator_and_waits_for_composite_cleanup(recording_desktop, qtbot):
    model, window, preview, made, calls = recording_desktop
    model.start_recording()
    task = made[0]
    qtbot.mouseClick(window.preview_stop, Qt.MouseButton.LeftButton)
    assert task.events.count('cancel') == 1 and calls.count('stop') == 0
    assert model.busy and model._preview is preview
    assert not window.recording_confirm.isVisible()


def test_db_failure_during_recording_requests_cleanup(recording_desktop):
    model, _, _, made, _ = recording_desktop
    model.start_recording()
    model.library.db.execute("CREATE TRIGGER recording_update_failure BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'recording checkpoint failure'); END")
    model.poll_preview()
    assert made[0].events.count('cancel') == 1
    assert model.busy and 'checkpoint failure' in model.preview_status
    assert model.library.records() == []


def test_non_durable_finish_warning_is_in_final_ui_refresh(recording_desktop):
    model, window, preview, made, _ = recording_desktop
    model.start_recording()
    task = made[0]
    task.terminal = True; task.state = 'complete'; task.durable = False; preview.state = 'complete'
    model.poll_preview()
    window.navigate(0)
    assert not model.busy
    assert '录制检查点未完整保存' in window.recovery_banner.text()
    assert window.recovery_banner.isVisible()
