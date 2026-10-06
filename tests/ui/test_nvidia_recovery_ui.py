from dataclasses import replace
import pytest
from PySide6.QtCore import Qt

from cs2pov.services.recording_recovery import RecordingRecoveryItem
from cs2pov.storage.settings import DataError
from test_preview_workflow import desktop


def blocked(model, session='old-task', digest='b'*64):
    item = RecordingRecoveryItem(session, model.directory/'sessions'/session if session else None,
        'a'*32, 'unknown', 'NVIDIA directory cannot be verified', digest)
    model.nvidia_recovery_items = [item]
    model.changed.emit()
    return item


def test_root_scan_error_remains_visible_and_cannot_be_acknowledged(desktop):
    model, window, _ = desktop
    item = blocked(model, session=None, digest=None)
    assert window.nvidia_recovery_list.item(0).data(Qt.ItemDataRole.UserRole) == item
    assert window.nvidia_recovery_list.item(0).text()
    assert not window.nvidia_recovery_resolve.isEnabled()
    window.navigate(0)
    assert window.recovery_banner.isVisible() and 'NVIDIA' in window.recovery_banner.text()


def test_nvidia_unknown_disables_prepare_independently_of_file_recovery(desktop):
    model, window, _ = desktop
    blocked(model)
    assert not model.recovery
    assert not window.prepare.isEnabled()
    assert window.nvidia_recovery_resolve.isEnabled()


def test_nvidia_selection_preserves_frozen_identity_and_digest(desktop):
    model, window, _ = desktop
    first = blocked(model)
    second = replace(first, session_id='second-task', directory=first.directory.parent/'second-task', digest='c'*64)
    model.nvidia_recovery_items = [first, second]; model.changed.emit()
    window.nvidia_recovery_list.setCurrentRow(1)
    model.changed.emit()
    assert window.nvidia_recovery_list.currentRow() == 1
    assert window.nvidia_recovery_list.currentItem().data(Qt.ItemDataRole.UserRole) == second
    calls = []
    model.resolve_recording_recovery = lambda item, **kw: calls.append((item, kw))
    window.resolve_nvidia_recovery()
    assert calls == [(second, dict(task_id=second.task_id, checkpoint_digest=second.digest))]


def test_old_recovery_item_cannot_authorize_changed_task(desktop):
    model, _, _ = desktop
    old = blocked(model)
    model.nvidia_recovery_items = [replace(old, digest='c'*64)]
    with pytest.raises(DataError, match='当前'):
        model.resolve_recording_recovery(old, task_id=old.task_id, checkpoint_digest=old.digest)
    assert list((model.directory/'sessions').iterdir()) == []
