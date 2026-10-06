"""A read-only UIA observation must never turn ambiguous UI into authorization."""
from dataclasses import replace
import ctypes
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from cs2pov.adapters.nvidia_status import (
    NvidiaStatusError, OverlayNode, OverlayWindow, OverlaySnapshot,
    ObservationRequest, classify_snapshot, collect_snapshot,
    snapshot_from_payload, snapshot_to_payload,
)
from cs2pov.adapters import nvidia_status
from cs2pov.adapters.owned_process import ProcessIdentity


EXE = r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe'
IDENTITY = ProcessIdentity(20616, 134355820960000000, EXE)
REQUEST = ObservationRequest('a' * 32, EXE, 'Alt+F9', 100.0)


def node(parent, name, kind=50020, *, pid=20616, offscreen=False, enabled=True):
    return OverlayNode(parent, name, kind, offscreen, enabled, pid)


def sample(action='开始', *, chord=True):
    nodes = [node(None, 'NVIDIA Overlay', 50032), node(0, '录制', 50026)]
    nodes.append(node(1, f'Alt+F9 - {action}' if chord else action, 50000))
    return OverlaySnapshot(REQUEST.request_id, 100.1, 100.2, True,
        (OverlayWindow(123, IDENTITY, True, tuple(nodes)),), '')


def classify(value, *, request=REQUEST, now=100.3, query=lambda _: IDENTITY):
    return classify_snapshot(value, request, now=now, identity_query=query)


@pytest.mark.parametrize(('action', 'expected'), [
    ('开始', 'idle'), ('停止', 'recording'), ('停止并保存', 'recording'),
    ('录制中', 'recording'),
])
def test_exact_visible_enabled_manual_recording_section(action, expected):
    assert classify(sample(action)).state == expected


def test_exact_section_button_can_be_read_without_hotkey_but_not_unscoped_text():
    assert classify(sample(chord=False)).state == 'idle'
    value = sample(chord=False)
    window = value.windows[0]
    value = replace(value, windows=(replace(window, nodes=(window.nodes[0],
        node(0, '开始', 50000))),))
    assert classify(value).state == 'unknown'


@pytest.mark.parametrize('wrong_source', ['即时重放', 'Instant Replay', '设置', 'Settings', '图库', '直播'])
def test_other_overlay_modules_cannot_provide_idle(wrong_source):
    value = sample()
    window = value.windows[0]
    nodes = list(window.nodes)
    nodes[1] = node(0, wrong_source, 50026)
    assert classify(replace(value, windows=(replace(window, nodes=tuple(nodes)),))).state == 'unknown'


@pytest.mark.parametrize('action', ['正在开始', '未录制', '开始录制？', 'Alt+F10 - 开始', '已保存', 'on', 'false'])
def test_unverified_labels_or_saved_toast_do_not_mean_current_idle(action):
    assert classify(sample(action, chord=False)).state == 'unknown'


@pytest.mark.parametrize('field, bad', [('offscreen', True), ('enabled', False),
                                      ('offscreen', 0), ('enabled', 1), ('process_id', 999)])
def test_hidden_disabled_or_foreign_elements_never_authorize(field, bad):
    value = sample()
    window = value.windows[0]
    nodes = list(window.nodes); nodes[2] = replace(nodes[2], **{field: bad})
    assert classify(replace(value, windows=(replace(window, nodes=tuple(nodes)),))).state == 'unknown'


def test_conflicting_actions_windows_or_broad_container_are_unknown():
    value = sample()
    window = value.windows[0]
    conflict = replace(window, nodes=(*window.nodes, node(1, '停止', 50000)))
    assert classify(replace(value, windows=(conflict,))).state == 'unknown'
    other = sample('停止').windows[0]
    assert classify(replace(value, windows=(window, replace(other, hwnd=124)))).state == 'unknown'
    broad = replace(window, nodes=(*window.nodes, node(1, '即时重放', 50000)))
    assert classify(replace(value, windows=(broad,))).state == 'unknown'


def test_recording_settings_ancestor_does_not_become_manual_capture_section():
    value = sample(chord=False)
    window = value.windows[0]
    nodes = (window.nodes[0], node(0, '设置', 50033), node(1, '录制', 50026), node(2, '开始', 50000))
    assert classify(replace(value, windows=(replace(window, nodes=nodes),))).state == 'unknown'


def test_hidden_ancestor_or_duplicate_manual_sections_are_ambiguous():
    value = sample()
    window = value.windows[0]
    nodes = (window.nodes[0], node(0, '', 50033, offscreen=True),
             node(1, '录制', 50026), node(2, '开始', 50000))
    assert classify(replace(value, windows=(replace(window, nodes=nodes),))).state == 'unknown'
    nodes = (*window.nodes, node(0, '录制', 50026), node(3, '开始', 50000))
    assert classify(replace(value, windows=(replace(window, nodes=nodes),))).state == 'unknown'


def test_slow_identity_recheck_cannot_extend_observation_freshness():
    current = [100.3]
    def query(_):
        current[0] += 1.1
        return IDENTITY
    value = classify_snapshot(sample(), REQUEST, now=current[0],
                              identity_query=query, clock=lambda: current[0])
    assert value.state == 'unknown'


@pytest.mark.parametrize('change', [
    {'request_id': 'b' * 32}, {'started_at': 99.9}, {'observed_at': 103.0},
    {'started_at': float('nan')}, {'observed_at': float('inf')},
    {'complete': False}, {'complete': 1}, {'windows': ()},
])
def test_old_replayed_partial_or_malformed_observation_is_unknown(change):
    assert classify(replace(sample(), **change)).state == 'unknown'


def test_observation_expiry_pid_reuse_wrong_exe_and_metadata_failure_are_unknown():
    assert classify(sample(), now=102.201).state == 'unknown'
    assert classify(sample(), query=lambda _: replace(IDENTITY, created=IDENTITY.created + 1)).state == 'unknown'
    assert classify(sample(), query=lambda _: replace(IDENTITY, executable=EXE.replace('Overlay', 'App'))).state == 'unknown'
    def failure(_): raise OSError('query denied')
    assert classify(sample(), query=failure).state == 'unknown'


def test_decode_is_strict_bounded_and_round_trips_without_recomputing_freshness():
    payload = snapshot_to_payload(sample())
    decoded = snapshot_from_payload(payload)
    assert decoded == sample() and classify(decoded).state == 'idle'
    assert classify(decoded, now=110).state == 'unknown'
    for mutation in (
        {'complete': 1}, {'windows': 'not a list'}, {'observed_at': float('nan')},
        {'request_id': 'short'}, {'extra': True},
    ):
        with pytest.raises(NvidiaStatusError): snapshot_from_payload({**payload, **mutation})
    payload['windows'][0]['nodes'][2]['enabled'] = 1
    with pytest.raises(NvidiaStatusError): snapshot_from_payload(payload)


class Backend:
    def __init__(self, *, foreign=False, endless=False):
        self.reads = []; self.queries = []; self.foreign = foreign; self.endless = endless
        self.properties = {
            0: dict(name='NVIDIA Overlay', control_type=50032, offscreen=False, enabled=True),
            1: dict(name='录制', control_type=50026, offscreen=False, enabled=True),
            2: dict(name='Alt+F9 - 开始', control_type=50000, offscreen=False, enabled=True),
            3: dict(name='ACCOUNT PRIVATE VALUE', control_type=50020, offscreen=False, enabled=True),
        }
    def windows(self, _exe): return [SimpleNamespace(hwnd=123, identity=IDENTITY)]
    def identity(self, pid):
        self.queries.append(pid)
        return IDENTITY if pid == IDENTITY.pid else ProcessIdentity(pid, 123, r'C:\other\secret.exe')
    def window_pid(self, _): return IDENTITY.pid
    def window_visible(self, _): return True
    def root(self, _): return 0
    def node_pid(self, value): return 999 if self.foreign and value == 2 else IDENTITY.pid
    def properties_of(self, value): self.reads.append(value); return self.properties[value]
    def children(self, value):
        if self.endless:
            while True: yield 1
        elif value == 0: yield from (1, 3)
        elif value == 1: yield 2


def test_collect_reads_only_matching_process_and_filters_unneeded_names_from_output():
    backend = Backend()
    value = collect_snapshot(REQUEST, backend=backend, clock=lambda: 100.2)
    assert value.complete and classify(value).state == 'idle'
    assert 'ACCOUNT PRIVATE VALUE' not in str(snapshot_to_payload(value))
    assert 3 in backend.reads
    backend = Backend(foreign=True)
    value = collect_snapshot(REQUEST, backend=backend, clock=lambda: 100.2)
    assert not value.complete and classify(value).state == 'unknown'
    assert 2 not in backend.reads  # Foreign process content was never requested.


def test_node_depth_and_window_limits_leave_partial_observation_unknown():
    value = collect_snapshot(REQUEST, backend=Backend(endless=True), clock=lambda: 100.2,
                             max_nodes=8, max_depth=4)
    assert not value.complete and len(value.windows[0].nodes) <= 8
    assert classify(value).state == 'unknown'
    backend = Backend()
    backend.windows = lambda _: [SimpleNamespace(hwnd=x, identity=IDENTITY) for x in range(1, 7)]
    value = collect_snapshot(REQUEST, backend=backend, clock=lambda: 100.2, max_windows=2)
    assert not value.complete and classify(value).state == 'unknown'


def test_process_identity_changes_after_walk_invalidate_entire_snapshot():
    backend = Backend()
    calls = [IDENTITY, IDENTITY, IDENTITY, IDENTITY, IDENTITY,
             replace(IDENTITY, created=IDENTITY.created + 1)]
    backend.identity = lambda _: calls.pop(0) if calls else replace(IDENTITY, created=IDENTITY.created + 1)
    value = collect_snapshot(REQUEST, backend=backend, clock=lambda: 100.2)
    assert not value.complete and classify(value).state == 'unknown'


@pytest.mark.parametrize(('event', 'args'), [
    ('open', ('cache.py', 'w', os.O_WRONLY)),
    ('open', ('cache.py', None, os.O_CREAT)),
    ('os.mkdir', ('user-cache', 511, -1)),
    ('os.remove', ('cache.py', -1)),
    ('subprocess.Popen', ('external.exe', [], None, None)),
    ('winreg.SetValueEx', (None, 'Recording', 0, 4, 1)),
])
def test_worker_audit_rejects_writes_and_any_nested_process(event, args):
    with pytest.raises(PermissionError): nvidia_status._readonly_audit(event, args)
    nvidia_status._readonly_audit('open', ('module.py', 'r', os.O_RDONLY))


def test_worker_entry_uses_one_readonly_backend_and_serializes_only_filtered_nodes(monkeypatch):
    request = dict(request_id=REQUEST.request_id, executable=EXE, hotkey=REQUEST.hotkey,
                   requested_at=REQUEST.requested_at)
    output = io.BytesIO(); hooks = []; backends = []
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(sys, 'addaudithook', hooks.append)
    monkeypatch.setattr(sys, 'dont_write_bytecode', False)
    backend = Backend()
    def factory(exe): backends.append(exe); return backend
    monkeypatch.setattr(nvidia_status, '_NativeUIA', factory)
    collect = nvidia_status.collect_snapshot
    monkeypatch.setattr(nvidia_status, 'collect_snapshot',
                        lambda request, **kwargs: collect(request, clock=lambda: 100.2, **kwargs))
    assert nvidia_status._worker_main() == 0
    assert hooks == [nvidia_status._readonly_audit] and backends == [EXE]
    assert b'ACCOUNT PRIVATE VALUE' not in output.getvalue()
    value = snapshot_from_payload(json.loads(output.getvalue()))
    assert classify(value).state == 'idle'


@pytest.mark.parametrize('raw', [b'x' * 4097, b'{bad', b'{}', b'{"executable":"other.exe"}'])
def test_invalid_worker_request_never_creates_uia_or_reads_desktop(monkeypatch, raw):
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(sys, 'addaudithook', lambda _: None)
    monkeypatch.setattr(sys, 'dont_write_bytecode', False)
    def forbidden(_): raise AssertionError('invalid request reached UIA')
    monkeypatch.setattr(nvidia_status, '_NativeUIA', forbidden)
    assert nvidia_status._worker_main() == 2


def test_native_window_enumeration_never_fetches_other_app_content(monkeypatch):
    backend = object.__new__(nvidia_status._NativeUIA)
    backend.exe = nvidia_status._exe_key(EXE)
    backend.callback_type = lambda value: value
    visible = {123: True, 124: False, 125: True, 126: True}
    pids = {123: 20616, 124: 20616, 125: 99, 126: 88}
    queried = []
    def enum(callback, _):
        for hwnd in pids:
            if not callback(hwnd, 0): return False
        return True
    backend.user32 = SimpleNamespace(EnumWindows=enum)
    root_reads = []
    backend._load = lambda: None
    def root(hwnd):
        root_reads.append(hwnd)
        return SimpleNamespace(CurrentNativeWindowHandle=hwnd, CurrentProcessId=pids[hwnd])
    backend.uia = SimpleNamespace(ElementFromHandle=root)
    backend.window_visible = lambda hwnd: visible[hwnd]
    backend.window_pid = lambda hwnd: pids[hwnd]
    def query(pid):
        queried.append(pid)
        return IDENTITY if pid == 20616 else replace(IDENTITY, pid=pid,
            executable=r'D:\alternate\NVIDIA Overlay.exe')
    backend.identity = query
    monkeypatch.setattr(nvidia_status, 'process_names', lambda: {
        20616: 'NVIDIA Overlay.exe', 88: 'NVIDIA Overlay.exe', 99: 'private.exe'})
    result = backend.windows(EXE)
    assert root_reads == [123]  # Never obtain a UIA root for another app or hidden window.
    assert [(value.hwnd, value.identity) for value in result] == [(123, IDENTITY)]
    assert queried == [20616, 88]  # Other App is not opened or read.
