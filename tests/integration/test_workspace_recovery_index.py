"""Real file journals and SQLite failures must remain recoverable through the UI."""
from dataclasses import replace
import json
import sqlite3
import stat
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from cs2pov.adapters.disk import DiskSnapshot, MIN_VIDEO_BYTES
from cs2pov.adapters.demo import fingerprint
from cs2pov.services import hud_trial
from cs2pov.services.hud_trial import prepare_trial
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.storage.transaction import attributes, set_attributes
from cs2pov.ui.window import MainWindow
from test_hud_trial import candidate, check_fixture


def restored_db_only_workspace(tmp_path, monkeypatch):
    """Inject a genuine SQLite checkpoint failure into the product poll path."""
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: True)
    check, original = check_fixture(tmp_path)
    root = tmp_path / "data"
    calls = []

    def factory(directory, *_args):
        preview = SimpleNamespace(session=directory / "preview-db-fault", state="idle",
                                  error="", console_confirmed=False, restoration=None)

        def start():
            calls.append("start")
            preview.transaction = prepare_trial(check, preview.session, candidate())
            preview.transaction.deploy()
            preview.state = "awaiting_console"

        def stop():
            calls.append("stop")
            preview.restoration = preview.transaction.restore()
            preview.state = "complete" if preview.transaction.data["complete"] else "recovery_blocked"

        preview.start, preview.stop, preview.poll = start, stop, lambda: None
        calls.append(preview)
        return preview

    model = Workspace(root, preview_factory=factory,
        disk_check=lambda path: DiskSnapshot(path, MIN_VIDEO_BYTES + 1, MIN_VIDEO_BYTES))
    model.save_settings(replace(model.settings, installation=str(check.installation),
        cfg=str(check.protected_configs[0].parent), video_directory=str(check.output)))
    check.demo.write_bytes(b"test fixture demo, never launched")
    model.import_demo(check.demo)
    draft = model.library.draft()
    draft.update(status="parsed", fingerprint=fingerprint(check.demo))
    model.library.save_draft(draft)
    model.analysis = dict(schema=2, map="de_test", tick_rate=64,
        timeline=[[i, i + 500] for i in range(129)],
        players=[dict(id="101", name="player", aliases=[], alive=[[0, 128]])],
        rounds=[dict(id=1, number=1, start=1, end=128, complete=True)],
        deaths=[], kills=[], kill_issues=[])
    model.save_selection("101", "round", round_id=1)
    model.start_preview(candidate())
    preview = calls[0]
    assert preview.state == "awaiting_console"
    assert (check.installation / "game/csgo/pov.vpk").exists()
    model.library.db.execute("""CREATE TRIGGER fail_session_update BEFORE UPDATE ON sessions
        BEGIN SELECT RAISE(ABORT, 'injected index checkpoint failure'); END""")
    model.poll_preview()
    assert calls.count("start") == calls.count("stop") == 1
    assert model._preview is None and not model.busy and not model._preview_timer.isActive()
    assert preview.transaction.data["complete"] is True
    assert model.library.unfinished() == [preview.session.name]
    assert model.recovery == ["数据库未完成会话：" + preview.session.name]
    assert (check.installation / "game/csgo/gameinfo.gi").read_bytes() == original
    assert check.protected_configs[0].read_bytes() == b"user settings"
    assert not (check.installation / "game/csgo/pov.vpk").exists()
    model.library.close()
    # The next startup has a writable database; the stale task is still genuine.
    with sqlite3.connect(root / "library.sqlite") as database:
        database.execute("DROP TRIGGER fail_session_update")
    fresh = Workspace(root)
    assert fresh.library.unfinished() == [preview.session.name]
    return fresh, preview.session, check, original


def test_checkpoint_failure_restored_journal_reconciles_from_actual_ui(tmp_path, monkeypatch, qtbot):
    model, session, check, original = restored_db_only_workspace(tmp_path, monkeypatch)
    # An independently unresolved NVIDIA checkpoint is not file restoration.
    recording = session / "recording-session.json"
    recording.write_text('{"state":"unknown","triggered":"start"}', encoding="utf-8")
    evidence = {path: (path.read_bytes(), attributes(path), path.stat().st_mtime_ns)
                for path in [*session.iterdir(), check.protected_configs[0],
                             check.installation / "game/csgo/gameinfo.gi"] if path.is_file()}
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _, title, text: warnings.append(text))
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    window.navigate(4)
    window.recovery_list.setCurrentRow(0)
    assert window.restore_button.isEnabled()
    qtbot.mouseClick(window.restore_button, Qt.MouseButton.LeftButton)
    assert warnings == []
    assert model.library.unfinished() == [] and model.recovery == []
    assert window.home_import.isEnabled()
    assert (check.installation / "game/csgo/gameinfo.gi").read_bytes() == original
    assert not (check.installation / "game/csgo/pov.vpk").exists()
    assert json.loads((session / "journal.json").read_text())["complete"] is True
    assert model.library.records() == []
    assert all((path.read_bytes(), attributes(path), path.stat().st_mtime_ns) == before
               for path, before in evidence.items())
    assert json.loads(recording.read_text())["state"] == "unknown"
    window.close()
    model.library.close()


@pytest.mark.parametrize("changed", ["current-content", "current-attributes", "backup", "live-game"])
def test_complete_journal_index_reconciliation_rechecks_and_preserves_conflicts(
        tmp_path, monkeypatch, qapp, changed):
    model, session, check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    config = check.protected_configs[0]
    if changed == "current-content":
        config.write_bytes(b"later legitimate user edit")
    elif changed == "current-attributes":
        set_attributes(config, attributes(config) | stat.FILE_ATTRIBUTE_READONLY)
    elif changed == "backup":
        (session / "config_0.backup").write_bytes(b"damaged backup")
    else:
        monkeypatch.setattr(hud_trial, "cs2_closed", lambda: False)
    content, attrs = config.read_bytes(), attributes(config)
    journal = (session / "journal.json").read_bytes()
    with pytest.raises((DataError, RuntimeError)):
        model.restore_session(session)
    assert config.read_bytes() == content and attributes(config) == attrs
    assert (session / "journal.json").read_bytes() == journal
    assert model.library.unfinished() == [session.name] and model.recovery
    assert model.library.records() == []
    set_attributes(config, attrs & ~stat.FILE_ATTRIBUTE_READONLY)
    model.library.close()


def test_recovery_ui_never_derives_target_from_display_text(tmp_path, monkeypatch, qtbot):
    model, session, _check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _, title, text: warnings.append(text))
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    window.navigate(4)
    window.recovery_list.setCurrentRow(0)
    item = window.recovery_list.currentItem()
    item.setText(str(tmp_path / "outside-data"))
    qtbot.mouseClick(window.restore_button, Qt.MouseButton.LeftButton)
    assert warnings == []
    assert model.library.unfinished() == [] and model.recovery == []
    assert not (tmp_path / "outside-data").exists()
    assert session.is_dir()
    window.close()
    model.library.close()


@pytest.mark.parametrize("missing", ["journal.json", "trial-context.json"])
def test_missing_recovery_evidence_never_completes_index(tmp_path, monkeypatch, qapp, missing):
    model, session, _check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    (session / missing).unlink()
    with pytest.raises((DataError, RuntimeError, OSError)):
        model.restore_session(session)
    assert model.library.unfinished() == [session.name]
    model.refresh_recovery()
    assert model.recovery
    model.library.close()


@pytest.mark.parametrize("session_id", ["..", "../outside", "E:\\outside", "a/b", "a\\b"])
def test_untrusted_database_id_is_not_an_actionable_recovery_path(tmp_path, monkeypatch, qtbot, session_id):
    model = Workspace(tmp_path / "data")
    with model.library.db:
        model.library.db.execute("INSERT INTO sessions VALUES (?, 'running')", (session_id,))
    model.refresh_recovery()
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    window.navigate(4)
    window.recovery_list.setCurrentRow(0)
    assert model.recovery and model.recovery_items[0].directory is None
    assert not window.restore_button.isEnabled()
    assert model.library.unfinished() == [session_id]
    window.close()
    model.library.close()


def test_recovery_selection_survives_refresh_and_keeps_structured_target(tmp_path, monkeypatch, qtbot):
    model, session, _check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    with model.library.db:
        model.library.db.execute("INSERT INTO sessions VALUES ('missing-second', 'running')")
    model.refresh_recovery()
    window = MainWindow(model)
    qtbot.addWidget(window)
    window.show()
    window.navigate(4)
    window.recovery_list.setCurrentRow(1)
    selected = window.recovery_list.currentItem().data(Qt.ItemDataRole.UserRole)
    assert selected.session_id == "missing-second"
    assert selected.directory == model.directory / "sessions/missing-second"
    model.changed.emit()
    assert window.recovery_list.currentItem().data(Qt.ItemDataRole.UserRole) == selected
    window.recovery_list.setCurrentRow(0)
    assert window.recovery_list.currentItem().data(Qt.ItemDataRole.UserRole).directory == session
    assert "restored" in window.recovery_details.text()
    window.close()
    model.library.close()


def test_game_starting_during_readonly_verification_preserves_stale_index(tmp_path, monkeypatch, qapp):
    model, session, check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    checks = iter([True, False])
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: next(checks))
    journal = (session / "journal.json").read_bytes()
    config = check.protected_configs[0]
    before = config.read_bytes(), attributes(config), config.stat().st_mtime_ns
    with pytest.raises(DataError, match="CS2"):
        model.restore_session(session)
    assert model.library.unfinished() == [session.name]
    assert (session / "journal.json").read_bytes() == journal
    assert (config.read_bytes(), attributes(config), config.stat().st_mtime_ns) == before
    model.library.close()


@pytest.mark.parametrize("field,value", [
    ("protected_configs", [{}]), ("protected_configs", [[]]), ("protected_configs", [1]),
    ("protected_configs", None), ("gameinfo_sha256", None), ("gameinfo_sha256", "invalid"),
    ("gameinfo_sha256", "F" * 64), ("patch_version", {}), ("installation", None),
])
def test_malformed_recovery_context_is_a_safe_data_error(
        tmp_path, monkeypatch, qapp, field, value):
    model, session, check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    context = session / "trial-context.json"
    raw = json.loads(context.read_text())
    if value is None:
        raw.pop(field)
    else:
        raw[field] = value
    context.write_text(json.dumps(raw))
    config = check.protected_configs[0]
    before = config.read_bytes(), attributes(config), config.stat().st_mtime_ns
    journal = (session / "journal.json").read_bytes()
    with pytest.raises(DataError):
        model.restore_session(session)
    assert model.library.unfinished() == [session.name]
    assert (session / "journal.json").read_bytes() == journal
    assert (config.read_bytes(), attributes(config), config.stat().st_mtime_ns) == before
    model.library.close()


def test_final_process_guard_mutating_an_earlier_file_is_detected_readonly(
        tmp_path, monkeypatch, qapp):
    model, session, check, _original = restored_db_only_workspace(tmp_path, monkeypatch)
    gameinfo = check.installation / "game/csgo/gameinfo.gi"
    before_attrs = attributes(gameinfo)
    journal = (session / "journal.json").read_bytes()
    calls = []

    def closed_with_external_change():
        calls.append(1)
        if len(calls) == 2:
            gameinfo.write_bytes(b"external change after the first full scan")
        return True

    monkeypatch.setattr(hud_trial, "cs2_closed", closed_with_external_change)
    with pytest.raises(DataError, match="gameinfo"):
        model.restore_session(session)
    assert len(calls) == 2
    assert gameinfo.read_bytes() == b"external change after the first full scan"
    assert attributes(gameinfo) == before_attrs
    assert (session / "journal.json").read_bytes() == journal
    assert model.library.unfinished() == [session.name]
    model.library.close()
