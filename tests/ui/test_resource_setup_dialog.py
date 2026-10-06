"""Exercise first-run controls without starting any real worker or network."""
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest
from PySide6.QtCore import QObject, QProcess as RealProcess, Signal
from PySide6.QtWidgets import QDialog

from cs2pov.ui import resource_setup


class FakeProcess(QObject):
    finished = Signal(int, object)
    errorOccurred = Signal(object)
    ProcessState = RealProcess.ProcessState
    instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_state = self.ProcessState.NotRunning
        self.starts = 0
        self.kills = 0
        self.waits = []
        self.instances.append(self)

    def state(self):
        return self.current_state

    def setProcessEnvironment(self, value):
        self.environment = value

    def setProgram(self, value):
        self.program = value

    def setArguments(self, value):
        self.arguments = value

    def start(self):
        self.starts += 1
        self.current_state = self.ProcessState.Running

    def kill(self):
        self.kills += 1
        self.current_state = self.ProcessState.NotRunning

    def waitForFinished(self, timeout):
        self.waits.append(timeout)
        return True

    def errorString(self):
        return 'fake worker start failed'


class FakeTimer(QObject):
    timeout = Signal()
    callbacks = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.milliseconds = None

    @staticmethod
    def singleShot(_milliseconds, callback):
        FakeTimer.callbacks.append(callback)

    def setSingleShot(self, value):
        self.single_shot = value

    def start(self, milliseconds):
        self.active = True
        self.milliseconds = milliseconds

    def stop(self):
        self.active = False


@pytest.fixture
def setup_dialog(tmp_path, monkeypatch, qtbot):
    FakeProcess.instances = []
    FakeTimer.callbacks = []
    monkeypatch.setattr(resource_setup, 'QProcess', FakeProcess)
    monkeypatch.setattr(resource_setup, 'QTimer', FakeTimer)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    ready = {'value': True}
    service = ModuleType('cs2pov.services.resource_setup')
    service.resources_ready = lambda _root: ready['value']
    monkeypatch.setitem(sys.modules, service.__name__, service)
    (tmp_path / 'data').mkdir()
    dialog = resource_setup.ResourceSetupDialog(tmp_path)
    qtbot.addWidget(dialog)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    return dialog, ready, accepted


def start(dialog):
    callback = FakeTimer.callbacks.pop(0)
    callback()
    assert dialog.process.starts == 1
    assert dialog.deadline.active


def finish(dialog, report=None, *, code=0):
    if report is not None:
        dialog.result.write_text(json.dumps(report), encoding='utf-8')
    dialog.process.current_state = FakeProcess.ProcessState.NotRunning
    dialog.process.finished.emit(code, RealProcess.ExitStatus.NormalExit)


def test_success_accepts_only_after_worker_and_real_resource_check(setup_dialog):
    dialog, _ready, accepted = setup_dialog
    start(dialog)
    assert accepted == []
    assert dialog.process.arguments[:2] == ['--resource-worker', '--root']
    assert Path(dialog.process.arguments[2]) == dialog.root
    assert dialog.process.environment.value('PYINSTALLER_RESET_ENVIRONMENT') == '1'
    assert not dialog.retry.isEnabled()
    assert dialog.deadline.milliseconds == 180000
    finish(dialog, {'ok': True})
    assert accepted == [True]
    assert QDialog.result(dialog) == QDialog.DialogCode.Accepted
    assert not dialog.deadline.active


@pytest.mark.parametrize('case', ['worker_error', 'nonzero_exit', 'not_ready', 'missing_report'])
def test_failure_stays_open_and_offers_retry(setup_dialog, case):
    dialog, ready, accepted = setup_dialog
    start(dialog)
    if case == 'not_ready':
        ready['value'] = False
    finish(dialog, None if case == 'missing_report' else {
        'ok': case != 'worker_error', 'error': 'network unavailable'},
        code=1 if case == 'nonzero_exit' else 0)
    assert accepted == []
    assert dialog.retry.isEnabled()
    assert '资源准备未完成' in dialog.message.text()
    assert not dialog.deadline.active
    previous_result = dialog.result
    dialog.retry.click()
    assert dialog.process.starts == 2
    assert dialog.result != previous_result
    assert not dialog.retry.isEnabled()


def test_failed_to_start_shows_error_and_retry(setup_dialog):
    dialog, _ready, accepted = setup_dialog
    start(dialog)
    dialog.process.current_state = FakeProcess.ProcessState.NotRunning
    dialog.process.errorOccurred.emit(RealProcess.ProcessError.FailedToStart)
    assert accepted == []
    assert '准备进程无法启动' in dialog.message.text()
    assert dialog.retry.isEnabled()
    assert not dialog.deadline.active


def test_cancel_kills_only_its_worker_and_ignores_success(setup_dialog):
    dialog, _ready, accepted = setup_dialog
    unrelated = FakeProcess()
    unrelated.start()
    start(dialog)
    dialog.cancel.click()
    assert dialog.process.kills == 1
    assert dialog.process.waits == [1000]
    assert unrelated.kills == 0
    assert dialog.cancelled
    assert not dialog.deadline.active
    finish(dialog, {'ok': True})
    assert accepted == []


def test_cancel_before_initial_callback_does_not_start_worker(setup_dialog):
    dialog, _ready, accepted = setup_dialog
    dialog.reject()
    FakeTimer.callbacks.pop(0)()
    assert dialog.process.starts == 0
    assert dialog.process.kills == 0
    assert not dialog.deadline.active
    assert accepted == []


def test_timeout_ignores_late_success_until_explicit_retry(setup_dialog):
    dialog, _ready, accepted = setup_dialog
    start(dialog)
    dialog.deadline.timeout.emit()
    assert dialog.expired
    assert dialog.process.kills == 1
    finish(dialog, {'ok': True})
    assert accepted == []
    assert '超时' in dialog.message.text()
    assert dialog.retry.isEnabled()
    dialog.retry.click()
    assert not dialog.expired
    assert dialog.process.starts == 2
    finish(dialog, {'ok': True})
    assert accepted == [True]


def test_ready_resources_do_not_create_dialog_or_start_worker(setup_dialog, monkeypatch):
    dialog, _ready, _accepted = setup_dialog
    count = len(FakeProcess.instances)
    monkeypatch.setattr(resource_setup, 'ResourceSetupDialog',
        lambda _root: pytest.fail('ready resources must bypass setup dialog'))
    assert resource_setup.ensure_resources(dialog.root) is True
    assert len(FakeProcess.instances) == count
    assert all(process.starts == 0 for process in FakeProcess.instances)
