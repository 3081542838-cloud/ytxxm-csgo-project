import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from cs2pov.services.workspace import Workspace
from cs2pov.services.recording_recovery import RecordingRecoveryItem
from cs2pov.ui.window import MainWindow


@pytest.fixture
def desktop(tmp_path, qtbot):
    model = Workspace(tmp_path / "app")
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    yield model, window
    window.close()
    model.library.close()


def save(model, record_id, video, **extra):
    model.library.save_record(record_id, {"video": str(video), "player": "101", "map": "de_mirage",
        "video_result": "metadata_verified", "duration": 4.5, "width": 1920, "height": 1080,
        "audio_streams": 1, **extra}, "complete")


def test_home_and_records_share_correct_video_result_and_selection_survives_refresh(desktop, tmp_path):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "old", video)
    save(model, "selected", video)
    window.refresh()
    window.records_list.setCurrentRow(1)
    assert window.selected_record()["id"] == "old"
    assert "metadata_verified" in window.recent_list.text()
    save(model, "newest", video)
    window.refresh()
    assert window.selected_record()["id"] == "old"
    assert "metadata_verified" in window.records_list.currentItem().text()


@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"duration":"broken"}',
    '{"duration":NaN}', '{"width":false}', '{"video":42}'])
def test_corrupt_record_stays_visible_and_unopenable(desktop, raw):
    model, window = desktop
    with model.library.db:
        model.library.db.execute("INSERT INTO records VALUES (?, ?, ?, ?)", ("bad", raw, 1, "unknown"))
    window.refresh()
    assert "不可用" in window.records_list.item(0).text()
    assert "不可用" in window.recent_list.text()
    assert "损坏" in window.record_details.text()
    assert not window.record_open_video.isEnabled()
    assert not window.record_open_folder.isEnabled()
    assert window.record_remove.isEnabled()
    assert model.library.record("bad")["payload"] == raw


def test_missing_video_disables_open_and_retains_reference(desktop, tmp_path):
    model, window = desktop
    video = tmp_path / "missing.mp4"
    save(model, "r1", video)
    window.refresh()
    assert not window.record_open_video.isEnabled()
    assert not window.record_open_folder.isEnabled()
    assert "不存在" in window.record_details.text()
    assert model.library.record("r1")


@pytest.mark.parametrize("action", ["open_selected_record_video", "open_selected_record_folder"])
def test_open_rereads_row_and_rechecks_missing_file(desktop, tmp_path, monkeypatch, action):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "r1", video)
    window.refresh()
    assert window.record_open_video.isEnabled()
    video.unlink()
    opened, warned = [], []
    monkeypatch.setattr(window, "open_path", opened.append)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warned.append(args))
    getattr(window, action)()
    assert opened == [] and len(warned) == 1
    assert not window.record_open_video.isEnabled()


def test_open_uses_current_database_path_not_previous_display(desktop, tmp_path, monkeypatch):
    model, window = desktop
    first = tmp_path / "first.mp4"; first.write_bytes(b"one")
    second = tmp_path / "second.mp4"; second.write_bytes(b"two")
    save(model, "r1", first)
    window.refresh()
    save(model, "r1", second)
    opened = []
    monkeypatch.setattr(window, "open_path", opened.append)
    window.open_selected_record_video()
    assert opened == [str(second)]


def test_removed_row_cannot_open_stale_selection(desktop, tmp_path, monkeypatch):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "r1", video)
    window.refresh()
    model.library.remove_record("r1")
    opened = []
    monkeypatch.setattr(window, "open_path", opened.append)
    window.open_selected_record_folder()
    assert opened == []


def test_remove_only_database_row_and_busy_blocks_record_actions(desktop, tmp_path):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"original")
    save(model, "r1", video)
    window.refresh()
    model.set_busy(True, "working")
    assert not window.record_remove.isEnabled()
    assert not window.record_open_video.isEnabled()
    model.set_busy(False)
    window.record_remove.click()
    assert model.library.records() == []
    assert video.read_bytes() == b"original"


@pytest.mark.parametrize("action", ["open_selected_record_video", "open_selected_record_folder"])
def test_corrupted_updated_row_cannot_open_previous_valid_video(desktop, tmp_path, monkeypatch, action):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "r1", video)
    window.refresh()
    with model.library.db:
        model.library.db.execute("UPDATE records SET payload=? WHERE id='r1'", ('{"video":42}',))
    opened, warned = [], []
    monkeypatch.setattr(window, "open_path", opened.append)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warned.append(args))
    getattr(window, action)()
    assert opened == [] and len(warned) == 1
    assert "损坏" in window.record_details.text()
    assert not window.record_open_video.isEnabled()


@pytest.mark.parametrize("filename,is_directory", [("clip.exe", False), ("clip.mp4", True)])
def test_non_video_or_directory_reference_cannot_enable_open(desktop, tmp_path, filename, is_directory):
    model, window = desktop
    path = tmp_path / filename
    if is_directory:
        path.mkdir()
    else:
        path.write_bytes(b"not a video")
    save(model, "r1", path)
    window.refresh()
    assert not window.record_open_video.isEnabled()
    assert not window.record_open_folder.isEnabled()
    assert model.library.record("r1")


def test_selection_change_updates_button_availability(desktop, tmp_path):
    model, window = desktop
    valid = tmp_path / "valid.mp4"; valid.write_bytes(b"video")
    save(model, "valid", valid)
    save(model, "missing", tmp_path / "missing.mp4")
    window.refresh()
    assert window.selected_record()["id"] == "missing"
    assert not window.record_open_video.isEnabled()
    window.records_list.setCurrentRow(1)
    assert window.selected_record()["id"] == "valid"
    assert window.record_open_video.isEnabled()


def test_reprepare_routes_current_record_to_workspace_without_starting_game(desktop, tmp_path, monkeypatch):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    snapshot = {"demo": str(tmp_path / "original.dem"), "selection": {"start_tick": 1}}
    save(model, "r1", video, draft_snapshot=snapshot)
    called = []
    def reprepare(record_id):
        called.append(record_id)
        return snapshot
    monkeypatch.setattr(model, "reprepare_record", reprepare, raising=False)
    monkeypatch.setattr(model, "start_preview", lambda *_: pytest.fail("reprepare cannot start game"))
    monkeypatch.setattr(model, "start_recording", lambda *_: pytest.fail("reprepare cannot start recording"))
    window.refresh()
    window.navigate(3)
    assert window.record_reprepare.isEnabled()
    window.record_reprepare.click()
    assert called == ["r1"] and window.stack.currentIndex() == 1


@pytest.mark.parametrize("guard", ["busy", "recovery", "nvidia_recovery"])
def test_reprepare_ui_is_blocked_by_busy_or_unfinished_recovery(desktop, tmp_path, monkeypatch, guard):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "r1", video, draft_snapshot={"demo": str(tmp_path / "original.dem")})
    monkeypatch.setattr(model, "reprepare_record", lambda *_: pytest.fail("blocked reprepare must not run"), raising=False)
    if guard == "busy":
        model.busy = True
    elif guard == "recovery":
        model.recovery = ["unrestored"]
    else:
        model.nvidia_recovery_items = [RecordingRecoveryItem(None, None, None, "unknown", "unfinished", None)]
    window.refresh()
    assert not window.record_reprepare.isEnabled()
    model.busy = False
    model.recovery = []
    model.nvidia_recovery_items = []


def test_metadata_content_and_restore_are_independently_visible(desktop, tmp_path):
    model, window = desktop
    video = tmp_path / "clip.mp4"; video.write_bytes(b"video")
    save(model, "r1", video, metadata_result="verified", content_result="unreviewed")
    window.refresh()
    assert "文件检查：verified" in window.record_details.text()
    assert "画面与声音检查：unreviewed" in window.record_details.text()
    assert "恢复：complete" in window.record_details.text()


def test_record_actions_remain_inside_minimum_window_width(desktop, qtbot, tmp_path):
    model, window = desktop
    save(model, "long", tmp_path / ("录像" * 90 + ".mp4"))
    window.refresh()
    window.navigate(3)
    window.resize(860, 620)
    qtbot.wait(30)
    assert window.width() == 860
    for action in (window.record_open_video, window.record_open_folder, window.record_reprepare, window.record_remove):
        point = action.mapTo(window, action.rect().topRight())
        assert 0 <= point.x() < window.width()
