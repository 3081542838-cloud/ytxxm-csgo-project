from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError
from cs2pov.ui.window import MainWindow


@pytest.fixture
def resource_desktop(tmp_path, qtbot, monkeypatch):
    model = Workspace(tmp_path / '应用数据')
    paths = {'hud': None, 'probe': None}
    registrations, warnings = [], []
    def register(role, path):
        registrations.append((role, path))
        paths[role] = Path(path)
        model.changed.emit()
    monkeypatch.setattr(model, 'register_local_resource', register, raising=False)
    monkeypatch.setattr(model, 'local_resource_path', lambda role: paths[role], raising=False)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[2]))
    window = MainWindow(model)
    qtbot.addWidget(window); window.show(); window.navigate(4)
    yield model, window, paths, registrations, warnings
    model.set_busy(False); window.close(); model.library.close()


def test_resource_controls_show_missing_paths_and_explicit_portable_prerequisites(resource_desktop):
    _, window, _, registrations, _ = resource_desktop
    assert set(window.local_resource_buttons) == {'hud', 'probe'}
    assert all('未设置' in item.text() for item in window.local_resource_labels.values())
    assert 'HUD' in window.local_resource_status.text() and '预览' in window.local_resource_status.text()
    assert 'ffprobe' in window.local_resource_status.text() and '检查' in window.local_resource_status.text()
    assert window.settings_save.isEnabled() and window.recovery_list is not None
    assert registrations == []


@pytest.mark.parametrize('role,filename,filter_text', [('hud', 'pov.vpk', '*.vpk'), ('probe', 'ffprobe.exe', 'ffprobe.exe')])
def test_file_selection_calls_only_the_role_registration_and_displays_confirmed_path(resource_desktop, monkeypatch,
                                                                                    tmp_path, role, filename, filter_text):
    _, window, paths, registrations, warnings = resource_desktop
    path = tmp_path / '中文 本地资源' / filename
    dialogs = []
    def dialog(*args):
        dialogs.append(args); return str(path), ''
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', dialog)
    window.local_resource_buttons[role].click()
    assert len(dialogs) == 1 and filter_text in dialogs[0][3]
    assert registrations == [(role, path)] and paths[role] == path and warnings == []
    assert window.local_resource_labels[role].text() == str(path)
    assert window.local_resource_labels[role].toolTip() == str(path)
    other = 'probe' if role == 'hud' else 'hud'
    assert '未设置' in window.local_resource_labels[other].text()


@pytest.mark.parametrize('role', ['hud', 'probe'])
def test_cancelled_resource_picker_does_not_register_or_change_existing_path(resource_desktop, monkeypatch, tmp_path, role):
    model, window, paths, registrations, warnings = resource_desktop
    previous = tmp_path / ('pov.vpk' if role == 'hud' else 'ffprobe.exe')
    paths[role] = previous; model.changed.emit()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: ('', ''))
    window.local_resource_buttons[role].click()
    assert registrations == [] and warnings == [] and paths[role] == previous
    assert window.local_resource_labels[role].text() == str(previous)


@pytest.mark.parametrize('role', ['hud', 'probe'])
def test_unreviewed_resource_uses_existing_error_presentation_without_showing_fake_success(resource_desktop,
                                                                                          monkeypatch, tmp_path, role):
    model, window, paths, registrations, warnings = resource_desktop
    path = tmp_path / ('unknown.vpk' if role == 'hud' else 'ffprobe.exe')
    def reject(*args):
        registrations.append(args)
        raise DataError('本地资源 SHA256 不是已审核版本。')
    monkeypatch.setattr(model, 'register_local_resource', reject)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(path), ''))
    window.local_resource_buttons[role].click()
    assert registrations == [(role, path)] and paths[role] is None
    assert warnings == ['本地资源 SHA256 不是已审核版本。']
    assert '未设置' in window.local_resource_labels[role].text()


def test_busy_blocks_resource_buttons_and_direct_picker_entry(resource_desktop, monkeypatch):
    model, window, _, registrations, _ = resource_desktop
    dialogs = []
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: dialogs.append(args) or ('C:/pov.vpk', ''))
    model.set_busy(True, '正在录制')
    assert all(not item.isEnabled() for item in window.local_resource_buttons.values())
    for button in window.local_resource_buttons.values(): button.click()
    window.pick_local_resource('hud')
    assert dialogs == [] and registrations == []
    model.set_busy(False)
    assert all(item.isEnabled() for item in window.local_resource_buttons.values())


def test_busy_started_while_file_dialog_open_rejects_late_selection(resource_desktop, monkeypatch, tmp_path):
    model, window, _, registrations, warnings = resource_desktop
    def dialog(*args):
        model.set_busy(True, '任务已经开始')
        return str(tmp_path / 'pov.vpk'), ''
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', dialog)
    window.local_resource_buttons['hud'].click()
    assert registrations == [] and warnings
    assert '未设置' in window.local_resource_labels['hud'].text()


def test_resource_path_getter_error_is_plain_visible_text_without_repeated_modal_dialog(resource_desktop, monkeypatch):
    model, window, _, registrations, warnings = resource_desktop
    def corrupt(_role): raise DataError('登记损坏，原文保留。')
    monkeypatch.setattr(model, 'local_resource_path', corrupt)
    model.changed.emit(); model.changed.emit()
    assert all('登记损坏' in item.text() for item in window.local_resource_labels.values())
    assert warnings == [] and registrations == []


def test_resource_refresh_preserves_unsaved_settings_and_restore_selection(resource_desktop, tmp_path):
    model, window, paths, _, _ = resource_desktop
    window.hotkey.setText('Ctrl+F8')
    window.path_edits['video_directory'].setText(str(tmp_path / '尚未保存'))
    paths['hud'] = tmp_path / 'pov.vpk'; model.changed.emit()
    assert window.hotkey.text() == 'Ctrl+F8'
    assert window.path_edits['video_directory'].text() == str(tmp_path / '尚未保存')
    assert window.recovery_list.currentItem().text() == '没有待恢复事务'
