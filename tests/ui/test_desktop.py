from dataclasses import replace
import json
from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileDialog
from cs2pov.adapters.disk import DiskError, DiskSnapshot, MIN_VIDEO_BYTES
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.ui.window import MainWindow, ICON


@pytest.fixture
def desktop(tmp_path, qtbot):
    model = Workspace(tmp_path / "应用数据")
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    yield model, window
    model.set_busy(False)
    window.close()
    model.library.close()


def test_five_pages_real_empty_state_and_shrimp_icon(desktop, qtbot):
    model, window = desktop
    assert window.stack.count() == 5
    for index, nav in enumerate(window.nav):
        qtbot.mouseClick(nav, Qt.MouseButton.LeftButton)
        assert window.stack.currentIndex() == index and nav.isChecked()
    assert "暂无录制记录" in window.recent_list.text()
    assert "尚未检查" in window.home_environment.text()
    assert not window.prepare.isEnabled()
    assert not window.recovery_link.isVisible()
    assert not QIcon(str(ICON)).isNull()
    assert {s.width() for s in QIcon(str(ICON)).availableSizes()} == {16,24,32,48,64,128,256}
    assert model.library.records() == []


def test_directory_picker_cancel_then_save_and_restart(desktop, monkeypatch, tmp_path):
    model, window = desktop
    model.save_settings(replace(model.settings, console_key='Slash'))
    window.navigate(4)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: "")
    window.pick_directory("video_directory")
    assert window.path_edits["video_directory"].text() == ""
    directory = tmp_path / "中文 视频"; directory.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(directory))
    window.pick_directory("video_directory")
    assert not hasattr(window, 'console_key')
    window.settings_save.click()
    assert model.settings.video_directory == str(directory)
    assert model.settings.console_key == "Slash"
    assert not model.settings.output_verified
    fresh = Workspace(model.directory)
    assert fresh.settings.video_directory == str(directory)
    assert fresh.settings.console_key == "Slash"
    fresh.library.close()


def test_path_confirmation_is_not_success_and_change_clears_it(desktop, tmp_path):
    model, window = desktop
    model.save_settings(replace(model.settings, video_directory=str(tmp_path)))
    window.nvidia_confirm.setChecked(True)
    window.save_settings()
    assert model.settings.nvidia_path_confirmed and not model.settings.output_verified
    model.save_settings(replace(model.settings, video_directory=str(tmp_path / "new")))
    assert not model.settings.nvidia_path_confirmed
    assert "未验证" in window.home_video.text()


@pytest.mark.parametrize("available,passes", [(MIN_VIDEO_BYTES-1,False),(MIN_VIDEO_BYTES,True),(MIN_VIDEO_BYTES+1,True)])
def test_disk_gate_and_home_settings_share_results(desktop, tmp_path, available, passes):
    model, window = desktop
    model.save_settings(replace(model.settings, video_directory=str(tmp_path)))
    def checker(path):
        if available < MIN_VIDEO_BYTES:
            raise DiskError("不足 10GB")
        return DiskSnapshot(path, available, MIN_VIDEO_BYTES)
    model.disk_check = checker
    if passes:
        assert model.preparation_gate().available_bytes == available
        assert "最低空间" in window.home_video.text()
    else:
        with pytest.raises(DataError, match="不足"):
            model.preparation_gate()
    assert window.home_video.text() == window.settings_status.text()


def test_query_failure_clears_previous_success_and_retry_refreshes(desktop, tmp_path):
    model, window = desktop
    model.save_settings(replace(model.settings, video_directory=str(tmp_path)))
    model.disk_check = lambda p: DiskSnapshot(p, MIN_VIDEO_BYTES, MIN_VIDEO_BYTES)
    assert model.check_video()
    def fail(_): raise DiskError("空间无法读取")
    model.disk_check = fail
    assert model.check_video() is None
    assert "无法读取" in window.home_video.text()
    with pytest.raises(DataError): model.preparation_gate()
    model.disk_check = lambda p: DiskSnapshot(p, MIN_VIDEO_BYTES, MIN_VIDEO_BYTES)
    assert model.check_video()


def test_recovery_priority_reset_preserves_backups_and_journal(desktop):
    model, window = desktop
    session = model.directory / "sessions/orphan"; session.mkdir()
    journal = session / "journal.json"; journal.write_bytes(b"corrupt")
    backup = model.directory / "backups/important"; backup.write_bytes(b"original")
    model.refresh_recovery()
    assert model.recovery and not window.home_import.isEnabled()
    assert not window.continue_draft.isEnabled()
    with pytest.raises(DataError, match="恢复"):
        model.preparation_gate()
    window.reset_settings()
    assert journal.read_bytes() == b"corrupt" and backup.read_bytes() == b"original"
    assert model.recovery


def test_busy_freezes_settings_hud_import_but_cancel_does_not_fake_completion(desktop, tmp_path):
    model, window = desktop
    model.set_busy(True, "正在录制")
    assert all(not control.isEnabled() for control in window.editor_widgets)
    assert not window.home_import.isEnabled()
    assert window.cancel_button.isEnabled()
    with pytest.raises(DataError): model.save_settings(model.settings)
    with pytest.raises(DataError): model.preset_action("copy", "builtin")
    with qtbot_signal(model.cancellation_requested):
        window.cancel_button.click()
    assert model.busy and "等待" in window.current_task.text()
    model.set_busy(False)
    assert window.settings_save.isEnabled()


class qtbot_signal:
    def __init__(self, signal): self.signal, self.fired = signal, False
    def __enter__(self): self.signal.connect(self.mark)
    def mark(self): self.fired = True
    def __exit__(self, *_):
        self.signal.disconnect(self.mark)
        assert self.fired


def test_demo_import_references_original_and_home_continue_shared(desktop, monkeypatch, tmp_path):
    model, window = desktop
    demo = tmp_path / "原比赛.dem"; demo.write_bytes(b"demo sample")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(demo), ""))
    window.home_import.click()
    assert model.library.draft()["demo"] == str(demo)
    assert str(demo) in window.draft_summary.text()
    assert window.continue_draft.isEnabled()
    assert demo.read_bytes() == b"demo sample"
    assert list(model.directory.rglob("*.dem")) == []


def test_hud_save_copy_and_restart(desktop):
    model, window = desktop
    window.hud_fields["hud_scale"].setValue(.8)
    window.hud_fields["viewmodel_z"].setValue(-1)
    window.hud_save.click()
    window.hud_copy.click()
    assert window.preset_choice.currentData() != "builtin"
    window.hud_default.click()
    fresh = Workspace(model.directory)
    assert fresh.presets.default != "builtin"
    assert next(p for p in fresh.presets.items if p.id == fresh.presets.default).hud_scale == .8
    fresh.library.close()


def test_resize_and_keyboard_navigation_remain_accessible(desktop, qtbot):
    _, window = desktop
    window.resize(860, 620)
    window.nav[2].setFocus()
    qtbot.keyClick(window.nav[2], Qt.Key.Key_Space)
    assert window.stack.currentIndex() == 2
    assert window.stack.width() > 500
    window.resize(1440, 1000)
    assert window.nav[2].isVisible()


def test_readonly_refresh_does_not_discard_unsaved_edits(desktop, tmp_path):
    model, window = desktop
    window.path_edits["video_directory"].setText(str(tmp_path / "未保存目录"))
    window.hud_fields["hud_scale"].setValue(.7)
    model.refresh_recovery()
    model.check_video()
    assert window.path_edits["video_directory"].text() == str(tmp_path / "未保存目录")
    assert window.hud_fields["hud_scale"].value() == .7


def test_record_page_opens_details_and_removes_only_library_row(desktop, qtbot, tmp_path):
    model, window = desktop
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    model.library.save_record("r1", {
        "player": "101", "map": "de_test", "video": str(video),
        "video_result": "verified", "duration": 4.5, "width": 1920,
        "height": 1080, "audio_streams": 1,
    }, "complete")
    model.changed.emit()
    window.navigate(3)
    window.records_list.setCurrentRow(0)
    assert window.record_open_video.isEnabled()
    assert "clip.mp4" in window.record_details.text()
    assert "1920×1080" in window.record_details.text()
    window.record_remove.click()
    assert model.library.records() == []
    assert video.read_bytes() == b"video"
    assert not window.record_remove.isEnabled()


def test_rapid_navigation_and_resize_keep_joined_selection_on_current_page(desktop, qtbot):
    model, window = desktop
    for index in (3, 1, 4, 2):
        window.nav[index].click()
    qtbot.waitUntil(lambda: window.sidebar_surface.get_y() == window.nav[2].geometry().top(), timeout=2000)
    assert window.stack.currentIndex() == 2
    window.resize(900, 720)
    qtbot.waitUntil(lambda: window.sidebar_surface.get_y() == window.nav[2].geometry().top(), timeout=2000)
    window.navigate(1)
    qtbot.waitUntil(lambda: window.sidebar_surface.get_y() == window.nav[1].geometry().top(), timeout=2000)
