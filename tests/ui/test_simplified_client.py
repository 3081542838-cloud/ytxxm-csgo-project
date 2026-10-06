"""The shipped automatic client must not expose the developer video checker."""
import pytest
from PySide6.QtWidgets import QLabel, QPushButton
from PySide6.QtWidgets import QMessageBox
from types import SimpleNamespace
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.ui.window import MainWindow


@pytest.fixture
def client(tmp_path, qtbot):
    model = Workspace(tmp_path / 'data', automatic_recording=True)
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    yield model, window
    window.close()
    model.library.close()


def test_client_does_not_create_probe_or_output_review_controls(client):
    model, window = client
    assert set(window.local_resource_buttons) == {'hud'}
    assert not hasattr(window, 'output_choose')
    assert not hasattr(window, 'content_checks')
    assert not hasattr(window, 'console_key')
    texts = [widget.text() for kind in (QLabel, QPushButton)
             for widget in window.findChildren(kind)]
    assert not any('ffprobe' in text or '视频检查工具' in text
                   or '试录路径' in text or '游戏控制台按键' in text
                   or '成片检查全部通过' in text for text in texts)
    assert any(text == '打开视频目录' for text in texts)
    assert window.recovery_list is not None
    assert window.nvidia_recovery_list is not None


@pytest.mark.parametrize('method,args', [
    ('attach_recording_output', (None,)),
    ('discover_recording_output', ()),
    ('validate_recording_output', ()),
])
def test_client_rejects_removed_video_checker_without_worker(client, monkeypatch, method, args):
    model, _ = client
    def forbidden(*args):
        raise AssertionError('Removed checker must not launch a process')
    monkeypatch.setattr(model, '_start_output_worker', forbidden)
    with pytest.raises(DataError, match='直接打开视频目录'):
        getattr(model, method)(*args)
    assert model._output_process is None
    assert not model.busy


def test_denied_input_stops_before_preview_or_file_deployment(client, monkeypatch):
    model, _ = client
    calls = []
    def blocked():
        raise DataError('Windows 拒绝自动按键')
    model.input_check = blocked
    monkeypatch.setattr(model, 'start_preview', lambda: calls.append('preview'))
    with pytest.raises(DataError, match='拒绝自动按键'):
        model.start_capture()
    assert calls == []
    assert model._preview is None
    assert not model._auto_capture_requested
    assert not list((model.directory / 'sessions').iterdir())


def test_terminal_recording_failure_is_presented_once(client, monkeypatch):
    model, window = client
    dialogs = []
    monkeypatch.setattr(QMessageBox, 'open', lambda dialog: dialogs.append(dialog))
    model._last_recording_task = SimpleNamespace(task_id='test-task', terminal=True,
        error='本次录制未启动：Windows 未发送录制热键（0/4，错误码5）。')
    window.show_recording_failure()
    window.show_recording_failure()
    assert len(dialogs) == 1
    assert '0/4' in dialogs[0].text()
    assert dialogs[0].windowTitle() == '录制未完成'
