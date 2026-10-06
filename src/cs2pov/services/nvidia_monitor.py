"""One asynchronous NVIDIA read; no input or NVIDIA process manipulation.

The production observer owns one disposable COM child and enforces its
three-second read deadline plus at most one second of child cleanup. A global
Qt pool owns the runnable: closing a view never destroys an active QThread.
An observation remains scoped evidence, not recording authorization.
"""

import math
import ntpath
import time

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, QCoreApplication, Qt, Signal, Slot

from cs2pov.adapters.nvidia_status import (
    ObservationRequest, NvidiaStatusEvidence, OverlaySnapshot, MAX_AGE,
    _request_valid, observe_nvidia_status,
)
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.storage.settings import DataError


class NvidiaMonitorError(DataError):
    pass


class _WorkerSignals(QObject):
    completed = Signal(int, object, object)


class _ObservationJob(QRunnable):
    def __init__(self, generation, request, observer):
        super().__init__()
        self.generation, self.request, self.observer = generation, request, observer
        # This object has no view/monitor parent and survives while the pool
        # owns the runnable, even if its receiving QObject has been deleted.
        self.signals = _WorkerSignals()

    def run(self):
        request = self.request
        try:
            result = self.observer(request.executable, hotkey=request.hotkey,
                                   request=request, raw_view=True, timeout=3)
        except Exception:
            result = NvidiaStatusEvidence('unknown', 'observer_failed', request.request_id)
        self.signals.completed.emit(self.generation, request, result)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class NvidiaMonitor(QObject):
    """Deliver ``on_result(evidence)`` on the main Qt thread, once at most.

    Cancel revokes the generation/callback. ``busy`` stays true until the
    physical worker returns, so cancellation cannot launch a second child
    beside an unreaped first one. It never kills or changes NVIDIA.
    """

    finished = Signal(object, object)  # Request and its unmodified/scoped result.

    def __init__(self, parent=None, *, observer=observe_nvidia_status, clock=time.monotonic, pool=None):
        super().__init__(parent)
        if not callable(observer) or not callable(clock):
            raise NvidiaMonitorError('NVIDIA 观察接口或时钟无效。')
        self._observer, self._clock = observer, clock
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        if not callable(getattr(self._pool, 'start', None)):
            raise NvidiaMonitorError('NVIDIA 观察缺少有效的后台线程池。')
        self._busy = False
        self._generation = 0
        self._job_generation = self._request = self._callback = self._job = None
        self._require_main_thread()

    @property
    def busy(self):
        return self._busy

    def _require_main_thread(self):
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() != app.thread() or self.thread() != app.thread():
            raise NvidiaMonitorError('NVIDIA 观察的调度和提交必须在 Qt 主线程。')

    def start(self, request, on_result):
        self._require_main_thread()
        if self.busy:
            return False
        try:
            if type(request) is not ObservationRequest or not callable(on_result):
                raise ValueError('invalid request or callback')
            _request_valid(request)
        except (TypeError, ValueError, OverflowError) as error:
            raise NvidiaMonitorError('NVIDIA 观察请求的路径、热键或身份无效。') from error
        self._generation += 1
        generation = self._generation
        job = _ObservationJob(generation, request, self._observer)
        job.signals.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._job_generation, self._request, self._callback, self._job = generation, request, on_result, job
        self._busy = True
        try:
            self._pool.start(job)
        except Exception as error:
            self._generation += 1
            self._busy = False
            self._job_generation = self._request = self._callback = self._job = None
            raise NvidiaMonitorError('NVIDIA 后台观察未能开始。') from error
        return True

    def cancel(self):
        self._require_main_thread()
        self._generation += 1
        self._callback = None
        # Do not clear a shared pool or touch NVIDIA. The production observer
        # reaps its own bounded child; completion clears the physical busy bit.

    def _scoped_result(self, request, result):
        def unknown(reason):
            return NvidiaStatusEvidence('unknown', reason, request.request_id)
        if (type(result) is not NvidiaStatusEvidence or result.request_id != request.request_id
                or result.state not in ('idle', 'recording', 'unknown')
                or result.source != 'windows_uia_readonly_prototype'
                or type(result.reason) is not str or len(result.reason) > 80):
            return unknown('observer_scope_mismatch')
        if result.snapshot is not None and (type(result.snapshot) is not OverlaySnapshot
                or result.snapshot.request_id != request.request_id):
            return unknown('observer_scope_mismatch')
        if result.state != 'unknown':
            try:
                now = self._clock()
            except Exception:
                return unknown('observer_clock_unavailable')
            if (type(result.identity) is not ProcessIdentity
                    or type(result.identity.executable) is not str
                    or type(result.identity.pid) is not int or not 0 < result.identity.pid <= 0xFFFFFFFF
                    or type(result.identity.created) is not int or result.identity.created <= 0
                    or type(result.hwnd) is not int or result.hwnd <= 0
                    or result.reason != 'exact_record_section'
                    or ntpath.normcase(ntpath.normpath(result.identity.executable)) != ntpath.normcase(ntpath.normpath(request.executable))
                    or not _finite(now) or not _finite(result.observed_at)
                    or not request.requested_at <= result.observed_at <= now
                    or now - result.observed_at > MAX_AGE):
                return unknown('observer_scope_or_age_mismatch')
        return result

    @Slot(int, object, object)
    def _deliver(self, generation, request, result):
        self._require_main_thread()
        if (not self.busy or generation != self._job_generation or request != self._request):
            return  # A delayed/duplicate signal cannot discharge a new worker.
        callback = self._callback if generation == self._generation else None
        self._busy = False
        self._job_generation = self._request = self._callback = self._job = None
        if callback is None:
            return
        evidence = self._scoped_result(request, result)
        if generation != self._generation:
            return
        callback(evidence)
        if generation == self._generation:
            self.finished.emit(request, evidence)
