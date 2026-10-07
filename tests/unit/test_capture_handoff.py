from types import SimpleNamespace

import pytest

from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError


@pytest.mark.parametrize('stage', ['ready', 'play_ready'])
def test_timer_handoff_failure_is_saved_closed_and_never_retried(tmp_path, qapp, stage):
    model = Workspace(tmp_path, automatic_recording=True)
    events = []
    preview = SimpleNamespace(state=stage, error='', automatic=True,
        console_confirmed=False, session=tmp_path / 'sessions' / 'preview-test',
        poll=lambda: None, pipe_cleanup_pending=False)
    def fail():
        events.append('handoff')
        raise DataError('handoff fault')
    def stop():
        events.append('stop')
        preview.state = 'complete'
    preview.prepare_playback = fail
    preview.stop = stop
    preview._persist = lambda: events.append(('persist', preview.error))
    model.start_recording = fail
    model._preview = preview
    model._auto_capture_requested = True
    model.set_busy(True)
    with model.library.db:
        model.library.db.execute('INSERT INTO sessions VALUES (?, ?)',
                                (preview.session.name, stage))
    try:
        model.poll_preview()
        assert not model.busy and model._preview is None
        assert not model._auto_capture_requested
        assert 'handoff fault' in model.task_text
        assert 'handoff fault' in preview.error
        assert events[:2] == ['handoff', 'stop']
        assert events[2][0] == 'persist'
        model.poll_preview()
        assert len(events) == 3
        assert model.library.unfinished() == []
    finally:
        model._preview_timer.stop()
        model.library.close()


def test_capture_rejects_missing_output_confirmation_before_launch(tmp_path, qapp):
    model = Workspace(tmp_path, automatic_recording=True)
    try:
        with pytest.raises(DataError, match='NVIDIA'):
            model.start_capture()
        assert model._preview is None and not model._auto_capture_requested
        assert not model.busy
    finally:
        model.library.close()
