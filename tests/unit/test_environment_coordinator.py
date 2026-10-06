import json
from pathlib import Path
import time

import pytest
from PySide6.QtCore import QObject, QProcess, QTimer, QThread, Signal

from cs2pov.services import environment_coordinator as service
from cs2pov.services.environment_receipt import EnvironmentSnapshot
from cs2pov.services.environment_worker import EnvironmentObservationResult
from cs2pov.storage.settings import Settings


class ControlledProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, object)
    errorOccurred = Signal(object)
    ProcessState, ProcessError, ExitStatus = QProcess.ProcessState, QProcess.ProcessError, QProcess.ExitStatus

    def __init__(self, owner):
        super().__init__(owner)
        self.args, self.program, self.environment = None, None, None
        self._state = QProcess.ProcessState.NotRunning
        self.stdout = self.stderr = b""
        self.kills = self.starts = 0

    def setProgram(self, value): self.program = value
    def setArguments(self, value): self.args = value
    def setProcessEnvironment(self, value): self.environment = value
    def start(self): self.starts += 1; self._state = QProcess.ProcessState.Running
    def state(self): return self._state
    def kill(self): self.kills += 1
    def errorString(self): return "controlled child failure"
    def readAllStandardOutput(self): result = self.stdout; self.stdout = b""; return result
    def readAllStandardError(self): result = self.stderr; self.stderr = b""; return result
    def finish(self, code=0, status=QProcess.ExitStatus.NormalExit):
        self._state = QProcess.ProcessState.NotRunning
        self.finished.emit(code, status)


@pytest.fixture
def config(tmp_path):
    return Settings(installation=str(tmp_path / "CS2"), cfg=str(tmp_path / "cfg"),
                    video_directory=str(tmp_path / "videos")), tmp_path / "pov.vpk", tmp_path / "NVIDIA Overlay.exe", tmp_path / "NVIDIA App.exe"


@pytest.fixture
def coordinator(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "QProcess", ControlledProcess)
    owner = QObject()
    coordinator = service.EnvironmentCoordinator(owner, tmp_path / "data")
    yield coordinator
    if coordinator.busy:
        process = coordinator._process
        coordinator.cancel()
        process.finish(-1)
    coordinator.deleteLater()


def snapshot_for(request):
    paths = {key: getattr(request, key) for key in ("installation", "cfg_directory", "output_directory",
        "hud_resource_path", "nvidia_executable", "nvidia_app_executable")}
    return EnvironmentSnapshot.decode(paths | {"cs2_version": "1.41.8.8", "hud_sha256": "a" * 64,
        "nvidia_version": "128.4.13.34", "nvidia_executable_sha256": "b" * 64, "nvidia_app_sha256": "c" * 64,
        "nvidia_app_component_version": "128.4.13.34", "driver_version": "32.0.15.8157"})


def complete_result(coordinator, *, snapshot=True, reason="", request_id=None, stamp=None):
    request = coordinator._request
    observed = time.time() if stamp is None else stamp
    result = EnvironmentObservationResult(1, request_id or request.request_id, request.configuration_sha256,
        coordinator._started_wall if stamp is None else observed - .1,
        observed, snapshot_for(request) if snapshot else None, reason)
    coordinator._result_path.write_text(json.dumps(result.to_dict()), encoding="utf-8")
    return result


def test_start_is_async_scoped_single_task_and_success_preserves_original_observation(coordinator, config, qtbot):
    callbacks, signals = [], []
    coordinator.finished.connect(lambda result, reason: signals.append((result, reason)))
    assert coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    assert coordinator.busy and process.starts == 1
    assert not coordinator.start(*config, lambda *_: pytest.fail("busy second request cannot run"))
    assert coordinator.snapshot is None and coordinator.current_result is None
    result = complete_result(coordinator)
    process.finish()
    assert not coordinator.busy and callbacks == [(result, "")] and signals == callbacks
    assert coordinator.snapshot == result.snapshot and coordinator.current_result.observed_at == result.observed_at
    qtbot.wait(20)
    assert coordinator.current_result.observed_at == result.observed_at


def test_cancel_consumes_generation_and_waits_for_worker_end_before_new_request(coordinator, config):
    callbacks = []
    assert coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    complete_result(coordinator)
    coordinator.cancel()
    assert process.kills == 1 and coordinator.busy
    assert coordinator.snapshot is None
    assert not coordinator.start(*config, lambda *_: pytest.fail("unreaped worker cannot be replaced"))
    process.finish()
    assert len(callbacks) == 1 and callbacks[0][0] is None and "取消" in callbacks[0][1]
    assert not coordinator.busy and coordinator.current_result is None


def test_timeout_kills_only_owned_child_and_does_not_accept_late_success(coordinator, config):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    assert coordinator._timer.interval() == 15_000
    coordinator._timer.timeout.emit()
    assert process.kills == 1 and coordinator.busy
    complete_result(coordinator)
    process.finish()
    assert callbacks[0][0] is None and "超时" in callbacks[0][1]


@pytest.mark.parametrize("failure", ["wrong_nonce", "oversized", "malformed", "nonfinite", "future", "old", "crash"])
def test_invalid_or_stale_result_never_becomes_environment_proof(coordinator, config, failure):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    if failure == "wrong_nonce": complete_result(coordinator, request_id="b" * 32)
    elif failure == "oversized": coordinator._result_path.write_bytes(b" " * 65_537)
    elif failure == "malformed": coordinator._result_path.write_bytes(b"{")
    elif failure == "nonfinite": coordinator._result_path.write_bytes(b'{"observed_at":NaN}')
    elif failure == "future": complete_result(coordinator, stamp=time.time() + 60)
    elif failure == "old": complete_result(coordinator, stamp=time.time() - 60)
    else: complete_result(coordinator)
    process.finish(0, QProcess.ExitStatus.CrashExit if failure == "crash" else QProcess.ExitStatus.NormalExit)
    assert len(callbacks) == 1 and callbacks[0][0] is None and callbacks[0][1]
    assert not coordinator.busy and coordinator.snapshot is None


def test_unknown_current_environment_is_not_successful_proof(coordinator, config):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    result = complete_result(coordinator, snapshot=False, reason="驱动版本未知")
    coordinator._process.finish()
    assert callbacks == [(None, "驱动版本未知")]
    assert coordinator.current_result == result and coordinator.snapshot is None


def test_failed_to_start_is_consumed_once_and_next_request_can_retry(coordinator, config):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    process._state = QProcess.ProcessState.NotRunning
    process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
    process.finish(-1)
    assert len(callbacks) == 1 and callbacks[0][0] is None and not coordinator.busy
    assert coordinator.start(*config, lambda *_: None)


def test_old_process_events_cannot_kill_or_overwrite_new_generation(coordinator, config):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    old = coordinator._process
    complete_result(coordinator); old.finish()
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    new = coordinator._process
    old.stdout = b"x" * 70_000
    old.readyReadStandardOutput.emit(); old.finish(-1)
    assert coordinator.busy and new.kills == 0 and len(callbacks) == 1
    result = complete_result(coordinator); new.finish()
    assert callbacks[-1] == (result, "")


def test_bounded_child_stdout_stderr_fail_closed_without_gui_freeze(coordinator, config):
    callbacks = []
    coordinator.start(*config, lambda result, reason: callbacks.append((result, reason)))
    process = coordinator._process
    process.stdout = b"x" * 65_537
    process.readyReadStandardOutput.emit()
    assert process.kills == 1
    process.finish(-1)
    assert callbacks[0][0] is None and "大小" in callbacks[0][1]


def test_source_and_frozen_worker_program_arguments_use_only_owned_entry(coordinator, config, monkeypatch):
    coordinator.start(*config, lambda *_: None)
    process = coordinator._process
    assert process.args[:2] == ["-m", "cs2pov.services.environment_worker"]
    assert "--mode" in process.args and "observe" in process.args
    assert Path(process.program).is_absolute()
    assert process.environment.value("PYTHONPATH").endswith("src")
    coordinator.cancel(); process.finish(-1)
    monkeypatch.setattr(service.sys, "frozen", True, raising=False)
    coordinator.start(*config, lambda *_: None)
    assert coordinator._process.args[0] == "--environment-worker"


def test_real_qprocess_environment_check_does_not_block_event_loop_or_open_sqlite(config, qtbot, tmp_path):
    owner = QObject()
    coordinator = service.EnvironmentCoordinator(owner, tmp_path / "actual-data")
    callbacks, heartbeats, threads = [], [], []
    timer = QTimer(); timer.setInterval(1); timer.timeout.connect(lambda: heartbeats.append(1)); timer.start()
    def received(result, reason):
        callbacks.append((result, reason)); threads.append(QThread.currentThread())
    assert coordinator.start(*config, received)
    qtbot.waitUntil(lambda: not coordinator.busy, timeout=8000)
    timer.stop()
    assert len(callbacks) == 1 and callbacks[0][0] is None and callbacks[0][1]
    assert heartbeats and threads == [owner.thread()]
    assert not list(tmp_path.rglob("*.sqlite"))
    coordinator.deleteLater()
