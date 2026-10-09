from pathlib import Path
from dataclasses import asdict
import pytest
from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QMessageBox
from cs2pov.services.workspace import Workspace
from cs2pov.ui.window import MainWindow
from cs2pov.adapters.demo import fingerprint, DemoError


def simple_analysis():
    return {"schema": 1, "parser": "demoparser2-0.42.0", "map": "de_test", "tick_rate": 64,
            "duration": 2, "timeline": [[i, 500 + i] for i in range(129)],
            "players": [{"id": "101", "name": "同名", "aliases": [], "alive": [[0, 128]]},
                        {"id": "202", "name": "同名", "aliases": [], "alive": [[0, 100]]}],
            "rounds": [{"id": 1, "number": 1, "start": 0, "end": 128, "complete": True}],
            "deaths": [{"player": "202", "tick": 101}]}


@pytest.fixture
def desktop(tmp_path, qtbot):
    model = Workspace(tmp_path / "data")
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    yield model, window
    if model._parse_process is not None:
        model.cancel()
        qtbot.waitUntil(lambda: not model.busy, timeout=5000)
    window.close()
    model.library.close()


def test_background_cancel_remains_responsive_and_retry_has_no_stale_result(desktop, tmp_path, qtbot, monkeypatch):
    model, window = desktop
    path = tmp_path / "bad.dem"; path.write_bytes(b"broken")
    model.import_demo(path)
    real_arguments = QProcess.setArguments
    monkeypatch.setattr(QProcess, "setArguments", lambda p, _: real_arguments(p, ["-c", "import time; time.sleep(10)"]))
    window.parse_button.click()
    assert model.busy and not window.player_choice.isEnabled()
    seen = []
    QTimer.singleShot(0, lambda: seen.append("UI event loop responsive"))
    qtbot.waitUntil(lambda: bool(seen), timeout=1000)
    model.cancel()
    qtbot.waitUntil(lambda: not model.busy, timeout=5000)
    assert model.analysis is None and model.library.draft()["status"] == "cancelled"
    assert model._parse_process is None and window.parse_button.isEnabled()
    monkeypatch.setattr(QProcess, "setArguments", real_arguments)
    model.start_parse()
    qtbot.waitUntil(lambda: not model.busy, timeout=10000)
    assert model.analysis is None and model.library.draft()["status"] == "failed"
    assert "解析失败" in window.demo_summary.text()
    assert not window.prepare.isEnabled()


def test_selection_is_shared_and_freezes_default_hud_snapshot(desktop, tmp_path):
    model, window = desktop
    path = tmp_path / "test.dem"; path.write_bytes(b"sample")
    model.import_demo(path)
    draft = model.library.draft()
    draft.update(status="parsed", fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    model.analysis = simple_analysis()
    model.changed.emit()
    assert window.player_choice.count() == 2
    window.player_choice.setCurrentIndex(1)
    window.range_mode.setCurrentIndex(window.range_mode.findData("round"))
    window.save_clip.click()
    saved = model.library.draft()["selection"]
    assert saved["player_id"] == "202" and saved["end_tick"] == 100
    assert "截短" in window.clip_summary.text()
    original_hud = saved["hud"]
    from dataclasses import replace
    model.save_preset(replace(model.presets.items[0], hud_scale=.7))
    assert model.library.draft()["selection"]["hud"] == original_hud
    reopened = Workspace(model.directory)
    assert reopened.library.draft()["selection"] == saved
    assert reopened.analysis is None  # Restart never auto-launches parsing/gameplay.
    reopened.library.close()
    model.remove_demo()
    assert model.library.draft() is None and path.read_bytes() == b"sample"


def test_source_changed_prevents_saving_stale_selection(desktop, tmp_path):
    model, _ = desktop
    path = tmp_path / "test.dem"; path.write_bytes(b"sample")
    model.import_demo(path)
    draft = model.library.draft(); draft["fingerprint"] = fingerprint(path)
    model.library.save_draft(draft)
    model.analysis = simple_analysis()
    path.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="变化"):
        model.save_selection("101", "round", round_id=1)
    assert "selection" not in model.library.draft()


def test_drag_accepts_one_local_demo_and_rejects_multiple(desktop, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl, QPoint, QPointF, Qt
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    model, window = desktop
    path = tmp_path / "拖入.dem"; path.write_bytes(b"reference")
    mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(path))])
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(drop)
    assert drop.isAccepted() and model.library.draft()["demo"] == str(path)
    assert path.read_bytes() == b"reference"
    mime.setUrls([QUrl.fromLocalFile(str(path)), QUrl.fromLocalFile(str(path))])
    event = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dragEnterEvent(event)
    assert not event.isAccepted()


def test_highlight_selection_numeric_core_and_snapshot_restore(desktop, tmp_path):
    model, window = desktop
    path = tmp_path / "highlight.dem"; path.write_bytes(b"sample")
    model.import_demo(path)
    draft = model.library.draft(); draft.update(status="parsed", fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    data = simple_analysis()
    data["kills"] = [{"player": "101", "victim": "202", "tick": t} for t in (30, 80)]
    data["kill_issues"] = [{"player": "101", "tick": 85, "reason": "missing"}]
    model.analysis = data; model.changed.emit()
    window.range_mode.setCurrentIndex(window.range_mode.findData("highlight"))
    assert window.highlight_choice.count() == 1 and window.save_clip.isEnabled()
    assert "可能不完整" in window.highlight_note.text()
    window.range_start.setValue(1.5)
    assert window.timeline.start == 30
    window.range_end.setValue(.1)
    assert window.timeline.end == 80
    window.save_clip.click()
    saved = model.library.draft()["selection"]
    assert saved["kill_ticks"] == [30, 80] and saved["mode"] == "highlight"
    assert saved["start_tick"] == 30 and saved["end_tick"] == 80
    with pytest.raises(DemoError, match="全部击杀"):
        model.save_selection("101", "highlight", candidate_id="101:1", start_tick=31, end_tick=80)
    assert model.library.draft()["selection"] == saved
    window.player_choice.setCurrentIndex(1)
    assert not window.timeline.ticks and not window.save_clip.isEnabled()
    assert window.highlight_choice.count() == 0
    # A fresh parse restores the saved editable range from the same source hash.
    import copy
    model.analysis = copy.deepcopy(data); model.changed.emit()
    assert (window.timeline.start, window.timeline.end) == (30, 80)
    model.remove_demo()
    assert not window.timeline.ticks and not window.save_clip.isEnabled()


def test_unavailable_highlight_is_visible_but_cannot_save(desktop, tmp_path):
    model, window = desktop
    data = simple_analysis()
    data["kills"] = [{"player": "202", "tick": t} for t in (30, 101)]
    model.analysis = data; model.changed.emit()
    window.player_choice.setCurrentIndex(1)
    window.range_mode.setCurrentIndex(window.range_mode.findData("highlight"))
    assert window.highlight_choice.count() == 1
    assert "不可录" in window.highlight_choice.currentText()
    assert not window.save_clip.isEnabled() and not window.timeline.ticks


def test_native_radar_default_hidden_and_refresh_does_not_change_choice(desktop):
    model, window = desktop
    # User explicitly replaced the gated POV radar with official spectator radar.
    assert window.radar_option.isEnabled() and not window.radar_option.isChecked()
    model.changed.emit()
    window.save_preset()
    assert window.radar_option.isEnabled() and not window.radar_option.isChecked()
    window.radar_option.setChecked(True)
    window.save_preset()
    model.changed.emit()
    assert window.radar_option.isChecked()
    assert model.presets.items[0].show_radar is True
    model.set_busy(True)
    assert not window.radar_option.isEnabled()
    model.set_busy(False)
    assert window.radar_option.isEnabled() and window.radar_option.isChecked()
    window.reset_preset()
    assert not window.radar_option.isChecked()
    assert model.presets.items[0].show_radar is False


def test_native_radar_selection_freezes_and_reopens(desktop, tmp_path):
    model, window = desktop
    window.radar_option.setChecked(True)
    window.save_preset()
    prepare_saved_time(model, tmp_path)
    saved = model.library.draft()['selection']
    assert saved['hud']['show_radar'] is True
    window.radar_option.setChecked(False)
    window.save_preset()
    assert model.library.draft()['selection']['hud']['show_radar'] is True
    reopened = Workspace(model.directory)
    try:
        assert reopened.library.draft()['selection']['hud']['show_radar'] is True
        assert reopened.presets.items[0].show_radar is False
    finally:
        reopened.library.close()


def prepare_saved_time(model, tmp_path):
    path = tmp_path / "saved-time.dem"
    path.write_bytes(b"sample")
    model.import_demo(path)
    draft = model.library.draft()
    draft.update(status="parsed", fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    data = simple_analysis()
    data["kills"] = [{"player": "101", "victim": "202", "tick": tick} for tick in (30, 80)]
    model.analysis = data
    model.save_selection("101", "time", start_seconds=.25, end_seconds=1.5)
    return data


def test_initial_window_restores_time_draft_with_available_highlight(tmp_path, qtbot):
    model = Workspace(tmp_path / "initial-data")
    prepare_saved_time(model, tmp_path)
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    window.navigate(1)
    try:
        assert window.range_mode.currentData() == "time"
        assert window.player_choice.currentData() == "101"
        assert (window.range_start.value(), window.range_end.value()) == (.25, 1.5)
        assert window.highlight_choice.count() == 1
        assert not window.timeline.isVisible()
        assert "0–2 秒" in window.clip_summary.text()
    finally:
        window.close()
        model.library.close()


def test_changed_saved_selection_restores_editor_without_new_analysis(desktop, tmp_path):
    model, window = desktop
    data = prepare_saved_time(model, tmp_path)
    window.range_mode.setCurrentIndex(window.range_mode.findData("highlight"))
    assert (window.range_start.value(), window.range_end.value()) == (0, 2)
    # The existing analysis remains the same, while a caller selects a new
    # task. Its summary and editable controls must refer to that same task.
    model.save_selection("101", "time", start_seconds=.5, end_seconds=1.25)
    assert model.analysis is data
    assert window.range_mode.currentData() == "time"
    assert (window.range_start.value(), window.range_end.value()) == (.5, 1.25)
    assert "0–1 秒" in window.clip_summary.text()


def test_ordinary_refresh_preserves_unsaved_time_and_highlight_edits(desktop, tmp_path):
    model, window = desktop
    prepare_saved_time(model, tmp_path)
    saved = model.library.draft()["selection"]
    window.range_start.setValue(.375)
    window.range_end.setValue(1.625)
    model.refresh_recovery()
    model.check_video()
    assert window.range_mode.currentData() == "time"
    assert (window.range_start.value(), window.range_end.value()) == (.375, 1.625)
    window.range_mode.setCurrentIndex(window.range_mode.findData("highlight"))
    window.range_start.setValue(.25)
    window.range_end.setValue(1.5)
    expected = (window.timeline.start, window.timeline.end)
    model.changed.emit()
    assert window.range_mode.currentData() == "highlight"
    assert (window.timeline.start, window.timeline.end) == expected
    assert (window.range_start.value(), window.range_end.value()) == (.25, 1.5)
    assert model.library.draft()["selection"] == saved


def test_new_analysis_restores_saved_time_after_unsaved_highlight_edit(desktop, tmp_path):
    import copy
    model, window = desktop
    data = prepare_saved_time(model, tmp_path)
    window.range_mode.setCurrentIndex(window.range_mode.findData("highlight"))
    window.range_start.setValue(.5)
    model.analysis = copy.deepcopy(data)
    model.changed.emit()
    assert window.range_mode.currentData() == "time"
    assert (window.range_start.value(), window.range_end.value()) == (.25, 1.5)
    assert window.timeline.start == 0 and window.timeline.end == 128


@pytest.mark.parametrize('edit', ['start', 'end', 'player', 'mode'])
def test_unsaved_clip_changes_block_preparing_old_draft(desktop, tmp_path, edit, monkeypatch):
    model, window = desktop
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: pytest.fail('Unexpected warning: ' + str(args[-1])))
    prepare_saved_time(model, tmp_path)
    saved = model.library.draft()['selection']
    assert window.prepare.isEnabled()
    if edit == 'start': window.range_start.setValue(.5)
    elif edit == 'end': window.range_end.setValue(1.75)
    elif edit == 'player': window.player_choice.setCurrentIndex(window.player_choice.findData('202'))
    else:
        window.range_mode.setCurrentIndex(window.range_mode.findData('round'))
        assert not window.save_clip.isEnabled()  # A time draft has no selected round.
        window.round_choice.setCurrentIndex(0)
    assert not window.prepare.isEnabled()
    assert '先保存' in window.clip_edit_status.text()
    model.changed.emit()
    assert not window.prepare.isEnabled() and model.library.draft()['selection'] == saved
    window.save_clip.click()
    assert window.prepare.isEnabled() and window.clip_edit_status.text() == ''
    assert model.library.draft()['selection'] != saved


def test_unsaved_highlight_drag_blocks_until_saved_and_round_truncation_stays_ready(desktop, tmp_path, monkeypatch):
    model, window = desktop
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: pytest.fail('Unexpected warning: ' + str(args[-1])))
    prepare_saved_time(model, tmp_path)
    window.range_mode.setCurrentIndex(window.range_mode.findData('highlight'))
    window.save_clip.click()
    assert window.prepare.isEnabled()
    window.timeline.active = 0
    window.timeline.move_handle(20)
    assert window.timeline.start == 20 and not window.prepare.isEnabled()
    window.save_clip.click()
    assert model.library.draft()['selection']['start_tick'] == 20 and window.prepare.isEnabled()
    window.player_choice.setCurrentIndex(window.player_choice.findData('202'))
    window.range_mode.setCurrentIndex(window.range_mode.findData('round'))
    window.round_choice.setCurrentIndex(0)
    window.save_clip.click()
    assert model.library.draft()['selection']['end_tick'] == 100
    assert window.prepare.isEnabled() and '截短' in window.clip_summary.text()


def test_saved_tick_seconds_match_spin_precision_for_non_integer_rate(desktop, tmp_path):
    model, window = desktop
    data = prepare_saved_time(model, tmp_path)
    data['tick_rate'] = 63.7
    model.save_selection('101', 'time', start_seconds=.25, end_seconds=1.5)
    saved = model.library.draft()['selection']
    assert window.range_start.value() == round(saved['start_seconds'], 6)
    assert window.range_end.value() == round(saved['end_seconds'], 6)
    assert window.prepare.isEnabled()


def test_multikill_default_survives_analysis_refresh(desktop, tmp_path):
    model, window = desktop
    path = tmp_path / "default.dem"; path.write_bytes(b"sample")
    model.import_demo(path)
    draft = model.library.draft(); draft.update(status="parsed", fingerprint=fingerprint(path))
    model.library.save_draft(draft)
    model.analysis = simple_analysis()
    model.analysis["kills"] = [{"player": "101", "tick": 32}, {"player": "101", "tick": 96}]
    model.analysis["kill_issues"] = []
    model.changed.emit()
    assert window.range_mode.currentData() == "highlight"
    assert window.range_mode.itemData(0) == "highlight"
    assert (window.timeline.start, window.timeline.end) == (0, 128)
    assert (window.range_start.value(), window.range_end.value()) == (0, 2)
