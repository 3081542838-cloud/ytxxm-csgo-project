from dataclasses import replace
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication, QThread, QTimer

from cs2pov.adapters.nvidia_status import ObservationRequest, NvidiaStatusEvidence, OverlaySnapshot
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.storage.settings import DataError


EXE = r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe'


def request():
    return ObservationRequest('a' * 32, EXE, 'Alt+F9', time.monotonic())


def monitor_api():
    from cs2pov.services.nvidia_monitor import NvidiaMonitor
    return NvidiaMonitor


def unknown(value):
    return NvidiaStatusEvidence('unknown', 'no_visible_recording_section', value.request_id)


def known(value, *, now=None):
    observed = time.monotonic() if now is None else now
    return NvidiaStatusEvidence('idle', 'exact_record_section', value.request_id, observed,
        ProcessIdentity(123, 456, EXE), 789, snapshot=OverlaySnapshot(value.request_id,
            value.requested_at, observed, True, (), ''))


def test_monitor_starts_one_async_read_and_delivers_on_main_qt_thread(qtbot):
    NvidiaMonitor = monitor_api()
    calls, results, heartbeat = [], [], []
    entered, release = threading.Event(), threading.Event()
    value = request()
    def observer(executable, **kwargs):
        calls.append((executable, kwargs, QThread.currentThread()))
        entered.set(); assert release.wait(2)
        return unknown(value)
    monitor = NvidiaMonitor(observer=observer)
    timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: heartbeat.append(1)); timer.start()
    started = time.monotonic()
    try:
        assert monitor.start(value, lambda result: results.append((result, QThread.currentThread())))
        assert time.monotonic() - started < .25
        assert monitor.busy and not monitor.start(value, lambda _: pytest.fail('a second child is forbidden'))
        qtbot.waitUntil(entered.is_set, timeout=2000); qtbot.wait(30)
        assert heartbeat and results == []
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert len(calls) == 1
        assert calls[0][:2] == (EXE, dict(hotkey='Alt+F9', request=value, raw_view=True, timeout=3))
        assert calls[0][2] != QCoreApplication.instance().thread()
        assert results == [(unknown(value), QCoreApplication.instance().thread())]
    finally:
        release.set(); timer.stop()
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        monitor.deleteLater()


def test_cancel_invalidates_late_result_and_keeps_busy_until_physical_worker_finishes(qtbot):
    NvidiaMonitor = monitor_api()
    value = request(); entered, release = threading.Event(), threading.Event()
    results, calls = [], []
    def observer(*args, **kwargs):
        calls.append(kwargs['request']); entered.set(); assert release.wait(2); return unknown(value)
    monitor = NvidiaMonitor(observer=observer)
    try:
        assert monitor.start(value, results.append)
        qtbot.waitUntil(entered.is_set, timeout=2000)
        monitor.cancel(); monitor.cancel()
        assert monitor.busy and not monitor.start(replace(value, request_id='b' * 32), results.append)
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert calls == [value] and results == []
        next_value = replace(value, request_id='b' * 32, requested_at=time.monotonic())
        monitor._observer = lambda *_args, **kwargs: unknown(kwargs['request'])
        assert monitor.start(next_value, results.append)
        qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert results == [unknown(next_value)]
    finally:
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000); monitor.deleteLater()


@pytest.mark.parametrize('result_kind', ['wrong_nonce', 'wrong_type', 'wrong_snapshot_nonce', 'wrong_executable', 'stale', 'future', 'wrong_source'])
def test_monitor_never_delivers_usable_state_from_wrong_scope_or_expired_queue_result(qtbot, result_kind):
    NvidiaMonitor = monitor_api()
    value = request(); evidence = known(value)
    changes = {
        'wrong_nonce': replace(evidence, request_id='b' * 32),
        'wrong_type': object(),
        'wrong_snapshot_nonce': replace(evidence, snapshot=replace(evidence.snapshot, request_id='b' * 32)),
        'wrong_executable': replace(evidence, identity=replace(evidence.identity, executable='C:/other/NVIDIA Overlay.exe')),
        'stale': replace(evidence, observed_at=value.requested_at-3),
        'future': replace(evidence, observed_at=value.requested_at+10),
        'wrong_source': replace(evidence, source='unverified_private_source'),
    }
    monitor = NvidiaMonitor(observer=lambda *_args, **_kwargs: changes[result_kind])
    results = []
    assert monitor.start(value, results.append)
    qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
    assert len(results) == 1 and results[0].state == 'unknown' and results[0].request_id == value.request_id
    assert results[0].snapshot is None
    monitor.deleteLater()


def test_monitor_preserves_valid_observation_timestamp_without_refreshing_it(qtbot):
    NvidiaMonitor = monitor_api()
    value = request(); evidence = known(value)
    monitor = NvidiaMonitor(observer=lambda *_args, **_kwargs: evidence)
    results = []
    assert monitor.start(value, results.append)
    qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
    assert results == [evidence] and results[0].observed_at == evidence.observed_at
    monitor.deleteLater()


def test_observer_exception_is_unknown_and_does_not_retry_or_leave_monitor_busy(qtbot):
    NvidiaMonitor = monitor_api()
    calls, results = [], []
    def fail(*_args, **_kwargs): calls.append(1); raise OSError('UIA worker unavailable')
    monitor = NvidiaMonitor(observer=fail); value = request()
    assert monitor.start(value, results.append)
    qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
    assert calls == [1] and results[0].state == 'unknown' and results[0].request_id == value.request_id
    monitor.deleteLater()


@pytest.mark.parametrize('change', [{'request_id': 'wrong'}, {'hotkey': 'Alt+F9;quit'},
    {'executable': 'relative/NVIDIA Overlay.exe'}, {'executable': 'C:/other.exe'},
    {'requested_at': float('nan')}, {'requested_at': True}, {'requested_at': 10**1000}])
def test_invalid_observation_request_cannot_start_a_worker(qtbot, change):
    NvidiaMonitor = monitor_api()
    calls = []
    monitor = NvidiaMonitor(observer=lambda *_args, **_kwargs: calls.append(1))
    with pytest.raises(DataError): monitor.start(replace(request(), **change), lambda _: None)
    assert not monitor.busy and calls == []
    monitor.deleteLater()


def test_monitor_owner_can_be_deleted_while_global_pool_job_finishes_without_a_qthread_destructor(qtbot):
    NvidiaMonitor = monitor_api()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    results = []; value = request()
    def observer(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(2); return unknown(value)
        finally: finished.set()
    monitor = NvidiaMonitor(observer=observer)
    assert monitor.start(value, results.append)
    try:
        qtbot.waitUntil(entered.is_set, timeout=2000)
        monitor.cancel(); monitor.deleteLater()
        qtbot.wait(20)
        release.set(); qtbot.waitUntil(finished.is_set, timeout=2000); qtbot.wait(20)
        assert results == []
    finally: release.set()


def test_old_generation_or_wrong_request_cannot_clear_current_physical_worker(qtbot):
    NvidiaMonitor = monitor_api()
    value = request(); entered, release = threading.Event(), threading.Event()
    results = []
    def observer(*_args, **kwargs):
        entered.set(); assert release.wait(2); return unknown(kwargs['request'])
    monitor = NvidiaMonitor(observer=observer)
    try:
        assert monitor.start(value, results.append)
        qtbot.waitUntil(entered.is_set, timeout=2000)
        generation = monitor._job_generation
        monitor._deliver(generation - 1, value, unknown(value))
        monitor._deliver(generation, replace(value, request_id='b' * 32), unknown(value))
        assert monitor.busy and results == []
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert results == [unknown(value)]
        entered.clear(); release.clear()
        next_value = replace(value, request_id='b' * 32, requested_at=time.monotonic())
        assert monitor.start(next_value, results.append)
        qtbot.waitUntil(entered.is_set, timeout=2000)
        monitor._deliver(generation, value, unknown(value))
        assert monitor.busy and results == [unknown(value)]
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
        assert results == [unknown(value), unknown(next_value)]
    finally:
        release.set(); qtbot.waitUntil(lambda: not monitor.busy, timeout=2000); monitor.deleteLater()


@pytest.mark.parametrize('change', [{'executable': None}, {'pid': True}, {'created': 0}])
def test_malformed_observer_identity_downgrades_to_unknown_instead_of_breaking_queued_slot(qtbot, change):
    NvidiaMonitor = monitor_api()
    value = request(); evidence = known(value)
    evidence = replace(evidence, identity=replace(evidence.identity, **change))
    monitor = NvidiaMonitor(observer=lambda *_args, **_kwargs: evidence)
    results = []
    assert monitor.start(value, results.append)
    qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
    assert len(results) == 1 and results[0].state == 'unknown'
    monitor.deleteLater()


def test_monitor_clock_failure_cannot_leave_usable_state_or_raise_out_of_queued_slot(qtbot):
    NvidiaMonitor = monitor_api()
    value = request(); evidence = known(value)
    def clock(): raise OSError('clock source failed')
    monitor = NvidiaMonitor(observer=lambda *_args, **_kwargs: evidence, clock=clock)
    results = []
    assert monitor.start(value, results.append)
    qtbot.waitUntil(lambda: not monitor.busy, timeout=2000)
    assert len(results) == 1 and results[0].state == 'unknown'
    monitor.deleteLater()
