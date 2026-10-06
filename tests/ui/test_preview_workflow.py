from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QMessageBox

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.disk import DiskSnapshot,MIN_VIDEO_BYTES,DiskError
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.ui.window import MainWindow


@pytest.fixture
def desktop(tmp_path,qtbot,monkeypatch):
    def unexpected_warning(*args): pytest.fail('Unexpected UI error: '+str(args[-1]))
    monkeypatch.setattr(QMessageBox,'warning',unexpected_warning)
    calls=[]
    def factory(directory,*args):
        session=SimpleNamespace(session=directory/'preview-one',state='idle',error='',
                                console_confirmed=False,restoration=None,console_step_token='initial-step')
        def start(): calls.append('start'); session.state='awaiting_console'
        def confirm(*, step_token=None):
            if session.state not in ('awaiting_console','awaiting_demo_console') or session.console_confirmed: raise DataError('duplicate confirmation')
            if step_token is not None and step_token != session.console_step_token: raise DataError('old step confirmation')
            calls.append('confirm');session.console_confirmed=True
        def poll(): calls.append('poll')
        def prepare_playback():
            calls.append('arm_playback'); session.state='awaiting_play_console'
        def play_clip():
            calls.append('play_clip'); session.state='awaiting_play_foreground'
        def check_playback_binding():
            calls.append('check_binding'); session.state='awaiting_end_console'
        def stop():
            calls.append('stop');session.state='complete';session.restoration={'gameinfo':'restored'}
        session.start=start;session.confirm_console=confirm;session.poll=poll;session.stop=stop
        session.prepare_playback=prepare_playback;session.play_clip=play_clip
        session.check_playback_binding=check_playback_binding
        calls.append(session)
        return session
    model=Workspace(tmp_path/'data',preview_factory=factory,
        disk_check=lambda path:DiskSnapshot(path,MIN_VIDEO_BYTES+1,MIN_VIDEO_BYTES))
    model.save_settings(replace(model.settings,installation=str(tmp_path/'game'),cfg=str(tmp_path/'cfg'),video_directory=str(tmp_path)))
    path=tmp_path/'sample.dem';path.write_bytes(b'original')
    model.import_demo(path)
    draft=model.library.draft();draft.update(status='parsed',fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    model.analysis=dict(schema=2,map='de_test',tick_rate=64,timeline=[[i,i+500] for i in range(129)],
        players=[dict(id='101',name='player',aliases=[],alive=[[0,128]])],
        rounds=[dict(id=1,number=1,start=1,end=128,complete=True)],deaths=[],kills=[],kill_issues=[])
    model.save_selection('101','round',round_id=1)
    window=MainWindow(model);qtbot.addWidget(window);window.show();window.navigate(1)
    yield model,window,calls
    model.stop_preview();model._preview_timer.stop();model.set_busy(False);window.close();model.library.close()


def test_preview_button_uses_shared_task_lock_confirmation_and_restore(desktop,qtbot):
    model,window,calls=desktop
    assert window.prepare.isEnabled() and not window.preview_confirm.isVisible()
    qtbot.mouseClick(window.prepare,Qt.MouseButton.LeftButton)
    session=calls[0]
    assert model.busy and model._preview is session
    assert not window.prepare.isEnabled() and not window.save_clip.isEnabled()
    assert window.preview_confirm.isEnabled() and window.preview_stop.isEnabled()
    assert '控制台' in window.preview_status.text()
    qtbot.mouseClick(window.preview_confirm,Qt.MouseButton.LeftButton)
    assert session.console_confirmed and not window.preview_confirm.isEnabled()
    assert '切回 CS2' in window.preview_status.text()
    qtbot.mouseClick(window.preview_stop,Qt.MouseButton.LeftButton)
    assert not model.busy and model._preview is None
    assert window.prepare.isEnabled() and not window.preview_stop.isVisible()
    assert '恢复完成' in window.preview_status.text()
    assert model.library.unfinished()==[] and calls.count('start')==calls.count('confirm')==calls.count('stop')==1


def test_pending_pipe_cleanup_exposes_public_retry_button_and_keeps_task_locked(desktop,qtbot):
    model,window,calls=desktop
    qtbot.mouseClick(window.prepare,Qt.MouseButton.LeftButton)
    session=calls[0]
    session.state='recovery_blocked'; session.pipe_cleanup_pending=True
    model.changed.emit()
    assert model.busy and not window.prepare.isEnabled()
    assert window.preview_stop.isEnabled() and window.preview_stop.text()=='重试安全收尾'
    original=session.stop
    def retry():
        session.pipe_cleanup_pending=False
        original()
    session.stop=retry
    qtbot.mouseClick(window.preview_stop,Qt.MouseButton.LeftButton)
    assert not model.busy and model._preview is None
    assert window.prepare.isEnabled() and calls.count('stop')==1


def test_pending_pipe_cleanup_during_owned_exit_does_not_offer_retry(desktop, qtbot):
    model, window, calls = desktop
    qtbot.mouseClick(window.prepare, Qt.MouseButton.LeftButton)
    session = calls[0]
    session.state = 'closing'; session.pipe_cleanup_pending = True
    try:
        model.changed.emit()
        assert model.busy and not window.prepare.isEnabled()
        assert not window.preview_stop.isEnabled()
    finally:
        session.pipe_cleanup_pending = False
        session.state = 'recovery_blocked'
        model.stop_preview()


def test_no_selection_no_analysis_or_recovery_never_enables_prepare(desktop):
    model,window,calls=desktop
    model.analysis=None;model.changed.emit();assert not window.prepare.isEnabled()
    with pytest.raises(DataError): model.start_preview()
    assert calls==[]
    model.recovery=['unfinished'];model.changed.emit();assert not window.prepare.isEnabled()


def test_low_space_blocks_before_preview_factory_and_does_not_start_game(desktop):
    model,_,calls=desktop
    def full(_): raise DiskError('below 10GB')
    model.disk_check=full
    with pytest.raises(DataError,match='10GB'):model.start_preview()
    assert calls==[] and not model.busy and model.library.unfinished()==[]


@pytest.mark.parametrize('hotkey', ['Alt+F8', 'Ctrl+F8', 'Shift+F8',
    'Ctrl+Alt+F8', 'Ctrl+Shift+F8', 'Alt+Shift+F8', 'Ctrl+Alt+Shift+F8'])
def test_playback_key_conflict_blocks_before_preview_factory(desktop, hotkey):
    model, _, calls = desktop
    model.save_settings(replace(model.settings, hotkey=hotkey))
    with pytest.raises(DataError, match='F8'):
        model.start_preview()
    assert calls == [] and model._preview is None and not model.busy
    assert model.library.unfinished() == []


def test_database_registration_failure_never_starts_preview_and_unlocks_ui(desktop):
    import sqlite3
    model, window, calls = desktop
    model.library.db.execute("CREATE TRIGGER fail_session_insert BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'injected registration failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='registration failure'):
        model.start_preview()
    assert 'start' not in calls
    assert calls.count('stop') == 1
    assert model._preview is None and not model.busy
    assert window.prepare.isEnabled() and window.save_clip.isEnabled()
    assert model.library.unfinished() == []


def test_database_poll_checkpoint_failure_closes_preview_and_keeps_recovery_block(desktop):
    model, window, calls = desktop
    model.start_preview()
    model.library.db.execute("CREATE TRIGGER fail_session_update BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END")
    model.poll_preview()
    assert calls.count('stop') == 1
    assert model._preview is None and not model.busy
    assert model.recovery and not window.prepare.isEnabled()
    assert 'checkpoint failure' in model.preview_status
    assert model.library.unfinished() == ['preview-one']
    assert not model._preview_timer.isActive()
    assert model.library.records() == []


def test_ready_preview_is_not_a_record_and_cancel_is_available(desktop):
    model,window,calls=desktop;model.start_preview()
    session=calls[0];session.state='ready';model.poll_preview()
    assert '预览准备完成' in window.preview_status.text() and model.busy
    assert not window.preview_confirm.isVisible() and window.preview_stop.isEnabled()
    assert model.library.records()==[]
    model.cancel()
    assert not model.busy and model.library.unfinished()==[]


def test_console_confirmation_is_required_again_after_map_loading(desktop,qtbot):
    model,window,calls=desktop;model.start_preview()
    session=calls[0];model.confirm_preview_console()
    session.state='loading';session.console_confirmed=False;model.poll_preview()
    assert not window.preview_confirm.isVisible()
    session.state='awaiting_demo_console';model.poll_preview()
    assert window.preview_confirm.isVisible() and window.preview_confirm.isEnabled()
    assert '再次打开' in window.preview_status.text() and '光标' in window.preview_status.text()
    qtbot.mouseClick(window.preview_confirm,Qt.MouseButton.LeftButton)
    assert session.console_confirmed and not window.preview_confirm.isEnabled()
    assert calls.count('confirm')==2 and model.busy
    qtbot.mouseClick(window.preview_stop,Qt.MouseButton.LeftButton)
    assert not model.busy and model.library.unfinished()==[]


def test_restore_failure_unlocks_editing_but_keeps_recovery_block_on_restart(desktop):
    model,window,calls=desktop;model.start_preview()
    session=calls[0];session.state='recovery_blocked';session.error='external update conflict'
    model.poll_preview()
    assert not model.busy and model.recovery and not window.prepare.isEnabled()
    assert 'conflict' in window.preview_status.text()
    reopened=Workspace(model.directory)
    assert reopened.recovery and reopened._preview is None
    reopened.library.close()


def test_hidden_playback_buttons_follow_verified_states_and_call_workspace_actions(desktop,qtbot):
    model,window,calls=desktop
    model.start_preview()
    session=calls[0]
    session.state='ready'; model.changed.emit(); qtbot.wait(10)
    assert window.preview_arm.isVisible() and window.preview_arm.isEnabled()
    assert not window.preview_play.isVisible() and not window.preview_binding.isVisible()
    qtbot.mouseClick(window.preview_arm,Qt.MouseButton.LeftButton)
    assert 'arm_playback' in calls

    session.state='play_ready'; model.changed.emit(); qtbot.wait(10)
    assert window.preview_play.isVisible() and window.preview_play.isEnabled()
    assert not window.preview_arm.isVisible()
    qtbot.mouseClick(window.preview_play,Qt.MouseButton.LeftButton)
    assert 'play_clip' in calls

    session.state='play_ended'; model.changed.emit(); qtbot.wait(10)
    assert window.preview_binding.isVisible() and window.preview_binding.isEnabled()
    qtbot.mouseClick(window.preview_binding,Qt.MouseButton.LeftButton)
    assert 'check_binding' in calls
    model.stop_preview()
    assert not model.busy


def test_ui_confirmation_cannot_authorize_a_new_step_or_other_session(desktop, qtbot, monkeypatch):
    model, window, calls = desktop
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda _, title, text: warnings.append(text))
    model.start_preview()
    session = calls[0]
    original = window._console_confirmation
    session.state = 'awaiting_demo_console'
    session.console_step_token = 'new-demo-step'
    # Submit the old display snapshot without a timer refresh first.
    qtbot.mouseClick(window.preview_confirm, Qt.MouseButton.LeftButton)
    assert not session.console_confirmed and calls.count('confirm') == 0
    assert warnings == ['old step confirmation']
    model.changed.emit()
    assert window._console_confirmation != original
    window.confirm_preview_console()
    assert session.console_confirmed and calls.count('confirm') == 1
    with pytest.raises(DataError, match='其他预览任务'):
        model.confirm_preview_console(session_id='previous-session', step_token='new-demo-step')
    assert calls.count('confirm') == 1


def test_new_required_action_scrolls_into_view_without_repeated_scroll(desktop, qtbot):
    model, window, _ = desktop
    window.resize(860, 620)
    qtbot.wait(30)
    scroll = window.stack.widget(1)
    scroll.verticalScrollBar().setValue(0)
    model.start_preview()
    qtbot.wait(50)
    target = window.preview_confirm
    viewport = scroll.viewport()
    assert viewport.rect().contains(target.mapTo(viewport, QPoint(0, 0)))
    assert viewport.rect().contains(target.mapTo(viewport, target.rect().bottomRight()))
    assert scroll.verticalScrollBar().value() > 0
    scroll.verticalScrollBar().setValue(0)
    model.changed.emit()
    qtbot.wait(30)
    assert scroll.verticalScrollBar().value() == 0
    previous = window._displayed_preview_step
    model.stop_preview()
    window.reveal_preview_step(previous)
    assert scroll.verticalScrollBar().value() == 0
