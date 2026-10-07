from types import SimpleNamespace
import pytest
from cs2pov.services.workspace import Workspace
from cs2pov.storage.settings import DataError


def test_capture_start_arms_continuation_before_preview():
    model = SimpleNamespace(busy=False, _auto_capture_requested=False,
                            ensure_idle=lambda: None, automatic_recording=False, input_check=None,
                            settings=SimpleNamespace(nvidia_path_confirmed=True))
    calls = []
    model.start_preview = lambda: calls.append(model._auto_capture_requested)
    Workspace.start_capture(model)
    assert calls == [True] and model._auto_capture_requested


def test_capture_failure_does_not_leave_automatic_continuation():
    model = SimpleNamespace(busy=False, _auto_capture_requested=False,
                            ensure_idle=lambda: None, automatic_recording=False, input_check=None,
                            settings=SimpleNamespace(nvidia_path_confirmed=True))
    def fail():
        raise DataError('missing configuration')
    model.start_preview = fail
    with pytest.raises(DataError):
        Workspace.start_capture(model)
    assert model._auto_capture_requested is False


def test_busy_capture_is_rejected_before_start():
    model = SimpleNamespace(busy=True, _auto_capture_requested=False)
    model.start_preview = lambda: pytest.fail('must not start a second preview')
    with pytest.raises(DataError):
        Workspace.start_capture(model)
    assert model._auto_capture_requested is False
