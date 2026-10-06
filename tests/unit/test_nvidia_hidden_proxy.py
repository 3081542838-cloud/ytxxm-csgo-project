"""Observed Overlay proxy HWNDs may map to a stable hidden same-process UIA root."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cs2pov.adapters import nvidia_status as status
from cs2pov.adapters.owned_process import ProcessIdentity


IDENTITY = ProcessIdentity(20028, 134356000000000001, r'C:\NVIDIA\NVIDIA Overlay.exe')
REQUEST = status.ObservationRequest('a' * 32, IDENTITY.executable, 'Alt+F9', 100.)
PROXY, ROOT = 66370, 66378


class Metadata:
    def __init__(self, proxies=(PROXY,)):
        self.refs = [SimpleNamespace(hwnd=hwnd, identity=IDENTITY) for hwnd in proxies]
        self.roots = {hwnd: ROOT for hwnd in proxies}
        self.roots[ROOT] = ROOT
        self.pids = {hwnd: IDENTITY.pid for hwnd in self.roots}
        self.visible = {hwnd: hwnd != ROOT for hwnd in self.roots}
        self.top = {hwnd: True for hwnd in self.roots}
        self.identities = {IDENTITY.pid: IDENTITY}
        self.bindings = {}

    def root_identity(self, hwnd): return self.roots[hwnd], self.pids[hwnd]
    def query(self, pid): return self.identities.get(pid)

    def canonical(self, **overrides):
        arguments = dict(root_identity=self.root_identity, window_pid=self.pids.__getitem__,
                         window_visible=self.visible.__getitem__, identity_query=self.query,
                         window_top_level=self.top.__getitem__, root_bindings=self.bindings)
        arguments.update(overrides)
        return status._canonical_windows(self.refs, **arguments)


def test_actual_metadata_keeps_visible_proxy_and_records_same_birth_hidden_root():
    metadata = Metadata()
    assert metadata.canonical() == metadata.refs
    assert metadata.bindings[PROXY] == (ROOT, IDENTITY.pid, True)


def test_two_visible_proxies_dedupe_only_after_both_map_to_verified_same_hidden_root():
    metadata = Metadata((PROXY, PROXY + 1))
    assert metadata.canonical() == [metadata.refs[0]]
    assert metadata.bindings[PROXY] == (ROOT, IDENTITY.pid, True)


def test_independent_hidden_roots_remain_independent_and_conflicting_idle_evidence():
    metadata = Metadata((PROXY, PROXY + 1))
    second_root = ROOT + 1
    metadata.roots.update({PROXY + 1: second_root, second_root: second_root})
    metadata.pids[second_root] = IDENTITY.pid
    metadata.visible[second_root] = False
    metadata.top[second_root] = True
    refs = metadata.canonical()
    assert refs == metadata.refs
    nodes = (status.OverlayNode(None, 'NVIDIA Overlay', 50033, False, True, IDENTITY.pid),
             status.OverlayNode(0, '录制', 50026, False, True, IDENTITY.pid),
             status.OverlayNode(1, 'Alt+F9 - 开始', 50000, False, True, IDENTITY.pid))
    snapshot = status.OverlaySnapshot(REQUEST.request_id, 100.1, 100.2, True,
        tuple(status.OverlayWindow(ref.hwnd, IDENTITY, True, nodes) for ref in refs), '')
    evidence = status.classify_snapshot(snapshot, REQUEST, now=100.3, identity_query=metadata.query)
    assert evidence.state == 'unknown' and evidence.reason == 'conflicting_or_missing_record_section'


@pytest.mark.parametrize('failure', ['foreign_root_pid', 'birth_changed', 'proxy_hidden',
                                    'root_now_visible', 'root_not_self', 'root_is_child',
                                    'missing_identity_query', 'missing_top_level_query',
                                    'root_changes_during_verification'])
def test_unverified_or_unstable_hidden_root_is_rejected_without_binding(failure):
    metadata = Metadata()
    overrides = {}
    if failure == 'foreign_root_pid': metadata.pids[ROOT] = 999
    elif failure == 'birth_changed': metadata.identities[IDENTITY.pid] = replace(IDENTITY, created=IDENTITY.created + 1)
    elif failure == 'proxy_hidden': metadata.visible[PROXY] = False
    elif failure == 'root_now_visible': metadata.visible[ROOT] = True
    elif failure == 'root_not_self': metadata.roots[ROOT] = PROXY
    elif failure == 'root_is_child': metadata.top[ROOT] = False
    elif failure == 'missing_identity_query': overrides['identity_query'] = None
    elif failure == 'missing_top_level_query': overrides['window_top_level'] = None
    else:
        calls = 0
        def root(hwnd):
            nonlocal calls
            if hwnd == ROOT:
                calls += 1
                if calls > 1: return ROOT + 2, IDENTITY.pid
            return metadata.root_identity(hwnd)
        overrides['root_identity'] = root
    with pytest.raises(status.NvidiaStatusError): metadata.canonical(**overrides)
    assert metadata.bindings == {}


def _walk_backend(metadata, *, change_after_walk=False):
    backend = object.__new__(status._NativeUIA)
    backend.identity = metadata.query
    backend.window_pid = metadata.pids.__getitem__
    backend.window_visible = metadata.visible.__getitem__
    backend.window_top_level = metadata.top.__getitem__
    backend._root_bindings = metadata.bindings
    backend.windows = lambda _: metadata.canonical()
    backend._load = lambda: None
    nodes = []
    for name, kind in (('NVIDIA Overlay', 50033), ('录制', 50026), ('Alt+F9 - 开始', 50000)):
        nodes.append(SimpleNamespace(CurrentNativeWindowHandle=ROOT, CurrentProcessId=IDENTITY.pid,
            CurrentName=name, CurrentControlType=kind, CurrentIsOffscreen=False, CurrentIsEnabled=True))
    def element(hwnd):
        node = nodes[0]
        return SimpleNamespace(**{**vars(node), 'CurrentNativeWindowHandle': metadata.roots[hwnd],
                                  'CurrentProcessId': metadata.pids[hwnd]})
    backend.uia = SimpleNamespace(ElementFromHandle=element)
    def children(node):
        if node.CurrentName == 'NVIDIA Overlay': yield nodes[1]
        elif node is nodes[1]: yield nodes[2]
        elif change_after_walk: metadata.roots[PROXY] = ROOT + 5
    backend.children = children
    return backend


def test_collection_rechecks_proxy_binding_after_walk_and_preserves_visible_hwnd():
    metadata = Metadata()
    value = status.collect_snapshot(REQUEST, backend=_walk_backend(metadata), clock=lambda: 100.2)
    assert value.complete
    evidence = status.classify_snapshot(value, REQUEST, now=100.3, identity_query=metadata.query)
    assert evidence.state == 'idle' and evidence.hwnd == PROXY
    metadata = Metadata()
    value = status.collect_snapshot(REQUEST, backend=_walk_backend(metadata, change_after_walk=True),
                                    clock=lambda: 100.2)
    assert not value.complete
    assert status.classify_snapshot(value, REQUEST, now=100.3, identity_query=metadata.query).state == 'unknown'


@pytest.mark.parametrize(('error', 'expected'), [
    (ModuleNotFoundError('private/path/account', name='comtypes.persist'),
     'uia_read_failed:ModuleNotFoundError:comtypes.persist'),
    (ModuleNotFoundError('private/path/account', name='PRIVATE_ACCOUNT.secret'),
     'uia_read_failed:ModuleNotFoundError'),
    (RuntimeError('private/path/account'), 'uia_read_failed:RuntimeError'),
])
def test_failed_read_diagnostic_exposes_only_type_and_known_comtypes_module(error, expected):
    def fail(_): raise error
    backend = SimpleNamespace(windows=fail)
    value = status.collect_snapshot(REQUEST, backend=backend, clock=lambda: 100.2)
    assert not value.complete and value.reason == expected
    assert 'private' not in str(status.snapshot_to_payload(value))
    assert len(value.reason) <= 80
