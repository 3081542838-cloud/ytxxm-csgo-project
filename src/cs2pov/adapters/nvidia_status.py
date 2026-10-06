"""Bounded, read-only NVIDIA Overlay UIA prototype.

No keyboard, focus, InvokePattern, private IPC or process-memory operations are
present. A separate, disposable child performs COM reads; its parent enforces
a hard deadline and rechecks every captured window's process identity. This
prototype returns observations, not recording authorization. It deliberately
does not infer idle from missing UI, configuration, files, or old observations.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
import types
import uuid
import tempfile

from cs2pov.adapters.nvidia import parse_hotkey
from cs2pov.adapters.owned_process import ProcessIdentity, process_identity
from cs2pov.adapters.processes import process_names


MAX_WINDOWS = 4
MAX_NODES = 512  # Total across all selected Overlay windows, not per window.
MAX_DEPTH = 32  # RawView includes Chromium wrappers and gallery image layers.
MAX_NAME = 256
MAX_WIRE_BYTES = 96_000
MAX_AGE = 2.0
RECORD_LABELS = frozenset(('录制', 'Record'))
OTHER_MODULES = frozenset(('即时重放', 'Instant Replay', '直播', 'Broadcast',
                           '设置', 'Settings', '视频捕获', 'Video capture', '图库', 'Gallery'))
ACTIONS = {'开始': 'idle', 'Start': 'idle', '停止': 'recording',
           'Stop': 'recording', '停止并保存': 'recording',
           'Stop and save': 'recording', '录制中': 'recording', 'Recording': 'recording'}
CONTAINERS = frozenset((50000, 50026, 50033))  # Button, Group, Pane.
ACTION_TYPES = frozenset((50000, 50020, 50022))  # Button, Text, StatusBar.


class NvidiaStatusError(ValueError):
    pass


@dataclass(frozen=True)
class ObservationRequest:
    request_id: str
    executable: str
    hotkey: str
    requested_at: float


@dataclass(frozen=True)
class OverlayNode:
    parent: int | None
    name: str
    control_type: int
    offscreen: bool
    enabled: bool
    process_id: int


@dataclass(frozen=True)
class OverlayWindow:
    hwnd: int
    identity: ProcessIdentity
    visible: bool
    nodes: tuple[OverlayNode, ...]


@dataclass(frozen=True)
class OverlaySnapshot:
    request_id: str
    started_at: float
    observed_at: float
    complete: bool
    windows: tuple[OverlayWindow, ...]
    reason: str


@dataclass(frozen=True)
class NvidiaStatusEvidence:
    state: str
    reason: str
    request_id: str = ''
    observed_at: float | None = None
    identity: ProcessIdentity | None = None
    hwnd: int | None = None
    source: str = 'windows_uia_readonly_prototype'
    snapshot: OverlaySnapshot | None = None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _exe_key(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 1024 or '\0' in value
            or not PureWindowsPath(value).is_absolute() or value.startswith(('\\\\', '//'))
            or '..' in PureWindowsPath(value).parts
            or PureWindowsPath(value).name.casefold() != 'nvidia overlay.exe'):
        raise NvidiaStatusError('invalid_overlay_executable')
    return ntpath.normcase(ntpath.normpath(value))


def _request_valid(request):
    if (not isinstance(request, ObservationRequest)
            or re.fullmatch(r'[0-9a-f]{32}', request.request_id or '') is None
            or not _finite(request.requested_at)):
        raise NvidiaStatusError('invalid_request')
    _exe_key(request.executable)
    parse_hotkey(request.hotkey)


def _identity_valid(identity):
    if (not isinstance(identity, ProcessIdentity) or type(identity.pid) is not int
            or not 0 < identity.pid <= 0xFFFFFFFF or type(identity.created) is not int
            or identity.created <= 0):
        raise NvidiaStatusError('invalid_identity')
    _exe_key(identity.executable)


def _known_name(value, hotkey):
    if not isinstance(value, str) or len(value) > MAX_NAME or '\0' in value:
        raise NvidiaStatusError('invalid_element_name')
    value = ' '.join(value.split())
    allowed = RECORD_LABELS | OTHER_MODULES | ACTIONS.keys() | {'NVIDIA', 'NVIDIA Overlay'}
    if value in allowed or value in {f'{hotkey} - {action}' for action in ACTIONS}:
        return value
    return ''  # Do not output account names, Gallery paths or irrelevant UI.


def snapshot_to_payload(snapshot):
    return {
        'request_id': snapshot.request_id, 'started_at': snapshot.started_at,
        'observed_at': snapshot.observed_at, 'complete': snapshot.complete,
        'reason': snapshot.reason,
        'windows': [{
            'hwnd': window.hwnd, 'identity': {
                'pid': window.identity.pid, 'created': window.identity.created,
                'executable': window.identity.executable}, 'visible': window.visible,
            'nodes': [{'parent': node.parent, 'name': node.name,
                       'control_type': node.control_type, 'offscreen': node.offscreen,
                       'enabled': node.enabled, 'process_id': node.process_id}
                      for node in window.nodes]} for window in snapshot.windows],
    }


def snapshot_from_payload(payload):
    try:
        if (not isinstance(payload, dict)
                or set(payload) != {'request_id', 'started_at', 'observed_at',
                                    'complete', 'reason', 'windows'}
                or not isinstance(payload['request_id'], str)
                or re.fullmatch(r'[0-9a-f]{32}', payload['request_id']) is None
                or not _finite(payload['started_at']) or not _finite(payload['observed_at'])
                or payload['observed_at'] < payload['started_at']
                or type(payload['complete']) is not bool
                or not isinstance(payload['reason'], str) or len(payload['reason']) > 80
                or type(payload['windows']) is not list or len(payload['windows']) > MAX_WINDOWS):
            raise ValueError('bad snapshot')
        windows = []
        total = 0
        handles = set()
        for value in payload['windows']:
            if (not isinstance(value, dict)
                    or set(value) != {'hwnd', 'identity', 'visible', 'nodes'}
                    or type(value['hwnd']) is not int or value['hwnd'] <= 0
                    or value['hwnd'] in handles or type(value['visible']) is not bool
                    or type(value['nodes']) is not list
                    or not isinstance(value['identity'], dict)
                    or set(value['identity']) != {'pid', 'created', 'executable'}):
                raise ValueError('bad window')
            identity = ProcessIdentity(**value['identity'])
            _identity_valid(identity)
            handles.add(value['hwnd'])
            total += len(value['nodes'])
            if total > MAX_NODES:
                raise ValueError('too many nodes')
            nodes = []
            depths = []
            for index, data in enumerate(value['nodes']):
                if (not isinstance(data, dict) or set(data) != {
                    'parent', 'name', 'control_type', 'offscreen', 'enabled', 'process_id'}
                        or (data['parent'] is not None and
                            (type(data['parent']) is not int or not 0 <= data['parent'] < index))
                        or (index == 0) != (data['parent'] is None)
                        or not isinstance(data['name'], str) or len(data['name']) > MAX_NAME
                        or '\0' in data['name']
                        or type(data['control_type']) is not int
                        or not 50000 <= data['control_type'] < 50100
                        or type(data['offscreen']) is not bool or type(data['enabled']) is not bool
                        or type(data['process_id']) is not int or not 0 < data['process_id'] <= 0xFFFFFFFF):
                    raise ValueError('bad node')
                depth = 0 if data['parent'] is None else depths[data['parent']] + 1
                if depth > MAX_DEPTH:
                    raise ValueError('too deep')
                depths.append(depth)
                nodes.append(OverlayNode(**data))
            windows.append(OverlayWindow(value['hwnd'], identity, value['visible'], tuple(nodes)))
        return OverlaySnapshot(payload['request_id'], payload['started_at'],
                               payload['observed_at'], payload['complete'], tuple(windows), payload['reason'])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise NvidiaStatusError('invalid_worker_payload') from error


def _ancestors(nodes, index):
    parent = nodes[index].parent
    while parent is not None:
        yield parent
        parent = nodes[parent].parent


def _usable_ancestor(nodes, index):
    node = nodes[index]
    if node.offscreen is not False:
        return False
    if node.enabled is True:
        return True
    # The observed Chromium host has one disabled, unnamed structural Pane
    # directly under an enabled Window, while its actual controls are enabled.
    # This narrow exception never applies to a row, Button, Group, Document,
    # named module, deeper Pane, or an offscreen ancestor.
    return (node.control_type == 50033 and node.name == '' and node.parent == 0
            and nodes[0].control_type == 50032 and nodes[0].enabled is True
            and nodes[0].offscreen is False)


def classify_snapshot(snapshot, request, *, now=None, identity_query=process_identity, clock=None):
    now = time.monotonic() if now is None else now
    safe_snapshot = None
    def unknown(reason):
        return NvidiaStatusEvidence('unknown', reason, getattr(request, 'request_id', ''),
                                    snapshot=safe_snapshot)
    try:
        _request_valid(request)
        if not isinstance(snapshot, OverlaySnapshot):
            return unknown('invalid_snapshot')
        # The wire validator is also used on injected objects, not only JSON.
        snapshot = snapshot_from_payload(snapshot_to_payload(snapshot))
        if any(_exe_key(window.identity.executable) != _exe_key(request.executable)
               or any(_known_name(node.name, request.hotkey) != node.name for node in window.nodes)
               for window in snapshot.windows):
            return unknown('untrusted_source_or_names')
        safe_snapshot = snapshot
        if (not _finite(now) or snapshot.request_id != request.request_id
                or not request.requested_at <= snapshot.started_at <= snapshot.observed_at <= now
                or now - snapshot.observed_at > MAX_AGE
                or snapshot.observed_at - snapshot.started_at > MAX_AGE
                or snapshot.complete is not True or snapshot.reason):
            return unknown('stale_or_incomplete')
        if not snapshot.windows:
            return unknown('no_visible_overlay_window')
        states = []
        for window in snapshot.windows:
            if (window.visible is not True or not window.nodes
                    or _exe_key(window.identity.executable) != _exe_key(request.executable)
                    or identity_query(window.identity.pid) != window.identity):
                return unknown('process_identity_changed')
            nodes = window.nodes
            if any(node.process_id != window.identity.pid for node in nodes):
                return unknown('foreign_element')
            if nodes[0].offscreen is not False or nodes[0].enabled is not True:
                return unknown('overlay_not_visible_or_enabled')
            candidates = set()
            matching_sections = set()
            for index, item in enumerate(nodes):
                if item.name not in RECORD_LABELS or item.offscreen or not item.enabled:
                    continue
                scope = index if item.control_type in CONTAINERS else item.parent
                if (scope is None or nodes[scope].control_type not in CONTAINERS
                        or nodes[scope].offscreen is not False or nodes[scope].enabled is not True):
                    continue
                if any(nodes[parent].name in OTHER_MODULES or not _usable_ancestor(nodes, parent)
                       for parent in _ancestors(nodes, scope)):
                    continue
                section = [node for pos, node in enumerate(nodes)
                           if pos == scope or scope in _ancestors(nodes, pos)]
                # A whole panel containing Instant Replay is not a Record row.
                if any(node.name in OTHER_MODULES for node in section):
                    continue
                for action in section:
                    if (action.offscreen is not False or action.enabled is not True
                            or action.control_type not in ACTION_TYPES
                            or any(not _usable_ancestor(nodes, parent)
                                   for parent in _ancestors(nodes, nodes.index(action)))):
                        continue
                    name = action.name
                    prefix = request.hotkey + ' - '
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                    # A bare Start/Stop must be an actual enabled button.
                    elif name not in ('录制中', 'Recording') and action.control_type != 50000:
                        continue
                    if name in ACTIONS:
                        candidates.add(ACTIONS[name])
                        matching_sections.add(scope)
            if len(candidates) > 1 or len(matching_sections) > 1:
                return unknown('conflicting_ui_states')
            if candidates:
                states.append((candidates.pop(), window))
        if len(states) != 1:
            return unknown('conflicting_or_missing_record_section')
        state, window = states[0]
        # Recheck once more after classification; do not refresh observation time.
        if identity_query(window.identity.pid) != window.identity:
            return unknown('process_identity_changed')
        finished = now if clock is None else clock()
        if not _finite(finished) or finished < now or finished - snapshot.observed_at > MAX_AGE:
            return unknown('stale_or_incomplete')
        return NvidiaStatusEvidence(state, 'exact_record_section', request.request_id,
            snapshot.observed_at, window.identity, window.hwnd, snapshot=snapshot)
    except Exception:
        return unknown('invalid_or_unavailable_evidence')


def collect_snapshot(request, *, backend, clock=time.monotonic, max_windows=MAX_WINDOWS,
                     max_nodes=MAX_NODES, max_depth=MAX_DEPTH):
    _request_valid(request)
    if (type(max_windows) is not int or not 1 <= max_windows <= MAX_WINDOWS
            or type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES
            or type(max_depth) is not int or not 1 <= max_depth <= MAX_DEPTH):
        raise NvidiaStatusError('invalid_limits')
    started = clock()
    windows = []
    total = 0
    reason = ''
    try:
        for number, ref in enumerate(backend.windows(request.executable)):
            if number >= max_windows:
                raise NvidiaStatusError('window_limit')
            identity = ref.identity
            _identity_valid(identity)
            if (_exe_key(identity.executable) != _exe_key(request.executable)
                    or backend.identity(identity.pid) != identity
                    or backend.window_pid(ref.hwnd) != identity.pid
                    or backend.window_visible(ref.hwnd) is not True):
                raise NvidiaStatusError('window_identity_changed')
            nodes = []
            try:
                verifier = getattr(backend, 'verify_window', None)
                if callable(verifier):
                    verifier(ref)
                pending = [(backend.root(ref.hwnd), None, 0)]
                while pending:
                    if clock() - started > MAX_AGE:
                        raise NvidiaStatusError('collection_timeout')
                    element, parent, depth = pending.pop()
                    if depth > max_depth or total >= max_nodes:
                        raise NvidiaStatusError('tree_limit')
                    # PID is read first, before requesting potentially foreign content.
                    pid = backend.node_pid(element)
                    if pid != identity.pid or backend.identity(pid) != identity:
                        raise NvidiaStatusError('foreign_element')
                    data = backend.properties_of(element)
                    name = _known_name(data['name'], request.hotkey)
                    node = OverlayNode(parent, name, data['control_type'],
                                       data['offscreen'], data['enabled'], pid)
                    position = len(nodes)
                    nodes.append(node); total += 1
                    # Pull siblings one at a time and bound their collection too.
                    children = []
                    for child in backend.children(element):
                        if len(children) + len(pending) + total >= max_nodes:
                            raise NvidiaStatusError('tree_limit')
                        if depth >= max_depth:
                            raise NvidiaStatusError('depth_limit')
                        children.append((child, position, depth + 1))
                    pending.extend(reversed(children))
                if callable(verifier):
                    verifier(ref)
                if (backend.identity(identity.pid) != identity
                        or backend.window_pid(ref.hwnd) != identity.pid
                        or backend.window_visible(ref.hwnd) is not True):
                    raise NvidiaStatusError('window_identity_changed')
            finally:
                windows.append(OverlayWindow(ref.hwnd, identity, True, tuple(nodes)))
    except NvidiaStatusError as error:
        reason = str(error)
    except Exception as error:
        reason = _uia_failure_reason(error)
    observed = clock()
    if not _finite(observed) or observed < started or observed - started > MAX_AGE:
        reason = 'collection_timeout'
    return OverlaySnapshot(request.request_id, started, observed, not reason, tuple(windows), reason)


def _uia_failure_reason(error):
    # Exception messages can contain account names and paths. Only expose a
    # fixed type label and, for missing imports, a bounded comtypes module name.
    allowed = {'ModuleNotFoundError', 'ImportError', 'PermissionError', 'FileNotFoundError',
               'OSError', 'COMError', 'RuntimeError', 'AttributeError', 'TypeError', 'ValueError'}
    name = type(error).__name__
    reason = 'uia_read_failed:' + (name if name in allowed else 'Exception')
    module = getattr(error, 'name', None) if isinstance(error, ImportError) else None
    if (isinstance(module, str) and re.fullmatch(r'comtypes(?:\.[A-Za-z_][A-Za-z0-9_]{0,39}){0,5}', module)
            and len(reason) + 1 + len(module) <= 80):
        reason += ':' + module
    return reason


class _NativeUIA:
    """Only public Win32 metadata and UIA property/navigation calls, in a worker."""
    def __init__(self, executable, *, raw_view=False):
        if sys.platform != 'win32' or not Path(executable).is_file():
            raise NvidiaStatusError('overlay_executable_unavailable')
        self.exe = _exe_key(executable)
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.uia = self.walker = None
        self.raw_view = raw_view
        self._root_bindings = {}


    def identity(self, pid):
        return process_identity(pid)


    def window_pid(self, hwnd):
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            raise NvidiaStatusError('window_pid_unavailable')
        return pid.value


    def window_visible(self, hwnd):
        return bool(self.user32.IsWindowVisible(hwnd))


    def window_top_level(self, hwnd):
        return (bool(self.user32.IsWindow(hwnd)) and not self.user32.GetParent(hwnd)
                and self.user32.GetAncestor(hwnd, 2) == hwnd)  # GA_ROOT


    def windows(self, _executable):
        identities = {}
        for pid, name in process_names().items():
            if name.casefold() != 'nvidia overlay.exe':
                continue
            identity = self.identity(pid)
            if _exe_key(identity.executable) == self.exe:
                identities[pid] = identity
        result = []
        failure = []
        enumerated = 0
        def callback(hwnd, _):
            nonlocal enumerated
            try:
                enumerated += 1
                if enumerated > 4096:
                    raise NvidiaStatusError('enumeration_limit')
                pid = self.window_pid(hwnd)
                if pid in identities and self.window_visible(hwnd):
                    result.append(types.SimpleNamespace(hwnd=int(hwnd), identity=identities[pid]))
                    if len(result) > MAX_WINDOWS:
                        raise NvidiaStatusError('window_limit')
                return True
            except Exception:
                failure.append('window_enumeration_failed')
                return False
        success = self.user32.EnumWindows(self.callback_type(callback), 0)
        if not success or failure:
            raise NvidiaStatusError('window_enumeration_failed')
        if not result:
            return result
        self._load()
        def root_identity(hwnd):
            root = self.uia.ElementFromHandle(hwnd)
            return int(root.CurrentNativeWindowHandle), int(root.CurrentProcessId)
        self._root_bindings = {}
        return _canonical_windows(result, root_identity=root_identity,
            window_pid=self.window_pid, window_visible=self.window_visible,
            identity_query=self.identity, window_top_level=self.window_top_level,
            root_bindings=self._root_bindings)


    def verify_window(self, ref):
        binding = self._root_bindings.get(ref.hwnd)
        if binding is None:
            raise NvidiaStatusError('unverified_root_alias')
        native, pid, hidden = binding
        def root_identity(hwnd):
            root = self.uia.ElementFromHandle(hwnd)
            return int(root.CurrentNativeWindowHandle), int(root.CurrentProcessId)
        if hidden:
            _verify_hidden_root(ref, native, pid, root_identity=root_identity,
                window_pid=self.window_pid, window_visible=self.window_visible,
                identity_query=self.identity, window_top_level=self.window_top_level)
        elif (pid != ref.identity.pid or self.identity(pid) != ref.identity
              or self.window_pid(ref.hwnd) != pid or self.window_visible(ref.hwnd) is not True
              or root_identity(ref.hwnd) != (native, pid) or native != ref.hwnd):
            raise NvidiaStatusError('window_identity_changed')


    def _load(self):
        if self.uia is not None:
            return
        # MTA is private to this disposable worker. The memory-only gen package
        # avoids creating comtypes/gen. Audit protection below rejects any cache
        # fallback write rather than allowing it to modify a user directory.
        sys.coinit_flags = 0
        import comtypes
        system32 = _system32()
        package = types.ModuleType('comtypes.gen')
        # comtypes.client checks this path on import before gen_dir can be set
        # to None. A frozen PYZ module's parent does not exist, causing a cache
        # mkdir fallback. Use an existing Windows directory only for that
        # initialization check; the worker audit still forbids every write.
        package.__path__ = [str(system32)]
        sys.modules['comtypes.gen'] = package
        comtypes.gen = package
        import comtypes.client
        comtypes.client.gen_dir = None
        # Memory code generation needs one path for synthetic __file__ values.
        # An existing DLL is not a directory, so it cannot supply cached Python
        # modules to importlib; no generated code is read from a cache or saved.
        package.__path__ = [str(system32 / 'UIAutomationCore.dll')]
        module = comtypes.client.GetModule(str(system32 / 'UIAutomationCore.dll'))
        self.uia = comtypes.CoCreateInstance(module.CUIAutomation._reg_clsid_,
            interface=module.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self.walker = self.uia.RawViewWalker if self.raw_view else self.uia.ControlViewWalker


    def root(self, hwnd):
        self._load()
        return self.uia.ElementFromHandle(hwnd)


    def node_pid(self, node):
        return int(node.CurrentProcessId)


    def properties_of(self, node):
        return {'name': node.CurrentName, 'control_type': int(node.CurrentControlType),
                'offscreen': bool(node.CurrentIsOffscreen), 'enabled': bool(node.CurrentIsEnabled)}


    def children(self, node):
        child = self.walker.GetFirstChildElement(node)
        while child:
            yield child
            child = self.walker.GetNextSiblingElement(child)


def _verify_hidden_root(ref, native, pid, *, root_identity, window_pid, window_visible,
                        identity_query, window_top_level):
    if (not callable(identity_query) or not callable(window_top_level)
            or type(native) is not int or native <= 0 or native == ref.hwnd
            or pid != ref.identity.pid or identity_query(pid) != ref.identity
            or window_pid(ref.hwnd) != pid or window_visible(ref.hwnd) is not True
            or window_pid(native) != pid or window_visible(native) is not False
            or window_top_level(native) is not True):
        raise NvidiaStatusError('unverified_root_alias')
    # Native HWND/PID are read only after Win32 has proved this root belongs to
    # the same held Overlay process; no foreign root content is requested.
    if root_identity(ref.hwnd) != (native, pid) or root_identity(native) != (native, pid):
        raise NvidiaStatusError('unverified_root_alias')
    if (identity_query(pid) != ref.identity or window_pid(ref.hwnd) != pid
            or window_visible(ref.hwnd) is not True or window_pid(native) != pid
            or window_visible(native) is not False or window_top_level(native) is not True):
        raise NvidiaStatusError('window_identity_changed')


def _canonical_windows(refs, *, root_identity, window_pid, window_visible,
                        identity_query=None, window_top_level=None, root_bindings=None):
    """Deduplicate only verified roots, retaining a visible evidence HWND.

    A visible Chromium proxy can expose a hidden top-level UIA root. It must be
    stable, self-rooted, same PID/birth and hidden before/after verification.
    Independent roots continue to be independent, potentially conflicting UI.
    """
    indexed = {ref.hwnd: ref for ref in refs}
    if len(indexed) != len(refs): raise NvidiaStatusError('duplicate_window')
    result, selected, bindings, hidden_refs = [], set(), {}, []
    for ref in refs:
        native, pid = root_identity(ref.hwnd)
        if (type(native) is not int or native <= 0 or pid != ref.identity.pid or window_pid(ref.hwnd) != pid
                or window_visible(ref.hwnd) is not True):
            raise NvidiaStatusError('window_identity_changed')
        if native == ref.hwnd:
            representative, hidden = ref, False
        else:
            target = indexed.get(native)
            if target is not None:
                if (target.identity != ref.identity or window_pid(native) != pid
                        or window_visible(native) is not True or root_identity(native) != (native, pid)):
                    raise NvidiaStatusError('unverified_root_alias')
                representative, hidden = target, False
            else:
                _verify_hidden_root(ref, native, pid, root_identity=root_identity,
                    window_pid=window_pid, window_visible=window_visible,
                    identity_query=identity_query, window_top_level=window_top_level)
                hidden_refs.append((ref, native, pid))
                representative, hidden = ref, True
        key = (native, ref.identity)
        if key not in selected:
            selected.add(key)
            result.append(representative)
            bindings[representative.hwnd] = (native, pid, hidden)
    # Verify every proxy, including discarded duplicate proxies, after all
    # roots are resolved. A late change invalidates the entire observation.
    for ref, native, pid in hidden_refs:
        _verify_hidden_root(ref, native, pid, root_identity=root_identity,
            window_pid=window_pid, window_visible=window_visible,
            identity_query=identity_query, window_top_level=window_top_level)
    if refs and not result: raise NvidiaStatusError('unverified_root_alias')
    if root_bindings is not None:
        root_bindings.update(bindings)
    return result


def _system32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    api.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise NvidiaStatusError('system_directory_unavailable')
    return Path(buffer.value)


def _readonly_audit(event, args):
    """Worker-only guard against Python cache/log writes or child processes."""
    if event == 'open':
        _path, mode, flags = args
        if ((isinstance(mode, str) and any(char in mode for char in 'wax+'))
                or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise PermissionError('read-only observer')
    if event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.link', 'os.symlink',
                 'os.truncate', 'os.chmod', 'os.utime', 'subprocess.Popen', 'os.system',
                 'winreg.SetValue', 'winreg.SetValueEx', 'winreg.CreateKey', 'winreg.DeleteKey'}:
        raise PermissionError('read-only observer')


def _worker_main(*, raw_view=False, request_path=None, result_path=None):
    sys.dont_write_bytecode = True
    result_stream = None
    if (request_path is None) != (result_path is None): return 2
    if request_path is not None:
        from cs2pov.adapters.video import read_data_lease
        from cs2pov.storage.transaction import no_redirection
        try:
            request_path, result_path = Path(request_path), Path(result_path)
            if not request_path.is_absolute() or not result_path.is_absolute(): return 2
            with read_data_lease(request_path) as signature:
                if signature.bytes > 4096: return 2
                raw = request_path.read_bytes()
            no_redirection(result_path)
            # Open our new result before installing the worker's read-only audit.
            # Only this held stream can then be written; no path is reopened.
            result_stream = result_path.open('xb')
        except Exception:
            return 2
    else:
        if sys.stdin is None: return 2
        raw = sys.stdin.buffer.read(4097)
    sys.addaudithook(_readonly_audit)
    if len(raw) > 4096:
        return 2
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if set(data) != {'request_id', 'executable', 'hotkey', 'requested_at'}:
            return 2
        request = ObservationRequest(**data)
        _request_valid(request)
        backend = (_NativeUIA(request.executable, raw_view=True) if raw_view
                   else _NativeUIA(request.executable))
        value = collect_snapshot(request, backend=backend)
    except Exception:
        return 2
    output = json.dumps(snapshot_to_payload(value), ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(output) > MAX_WIRE_BYTES:
        return 2
    stream = result_stream if result_stream is not None else (sys.stdout.buffer if sys.stdout else None)
    if stream is None: return 2
    stream.write(output)
    stream.flush()
    if result_stream is not None:
        os.fsync(stream.fileno())
        stream.close()
    return 0


def _unique_pairs(pairs):
    value = {}
    for key, data in pairs:
        if key in value: raise ValueError('duplicate key')
        value[key] = data
    return value


def _reap_worker(process):
    try:
        if process.poll() is None:
            process.kill()  # Held Popen process, never a NVIDIA PID.
        process.communicate(timeout=1)
        return process.poll() is not None
    except Exception:
        return False


def observe_nvidia_status(executable, *, hotkey='Alt+F9', timeout=3.0,
                          popen=subprocess.Popen, clock=time.monotonic,
                          identity_query=process_identity, raw_view=False, request=None):
    """Call from a background thread; only its own COM child may be terminated.

    At most one child is created. A timeout or uncertain read never launches a
    retry and never changes NVIDIA. The result includes a filtered snapshot for
    diagnostics; a saved result must not be treated as a fresh later sample.
    """
    request = request or ObservationRequest(uuid.uuid4().hex, executable, hotkey, clock())
    process = None
    temporary = None
    def unknown(reason):
        return NvidiaStatusEvidence('unknown', reason, request.request_id)
    try:
        _request_valid(request)
        now = clock()
        if (_exe_key(request.executable) != _exe_key(executable) or request.hotkey != hotkey
                or not 0 <= now-request.requested_at <= MAX_AGE):
            return unknown('invalid_request_scope_or_age')
        if type(raw_view) is not bool:
            raise NvidiaStatusError('invalid_view')
        if not _finite(timeout) or not .05 <= timeout <= 10:
            raise NvidiaStatusError('invalid_timeout')
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        started = clock()
        prefix = ['--nvidia-status-worker'] if getattr(sys, 'frozen', False) else ['-m', 'cs2pov.adapters.nvidia_status']
        request_bytes = json.dumps({
            'request_id': request.request_id, 'executable': request.executable,
            'hotkey': request.hotkey, 'requested_at': request.requested_at},
            ensure_ascii=False, allow_nan=False).encode('utf-8')
        flags = ['--raw-worker' if raw_view else '--worker']
        frozen = getattr(sys, 'frozen', False)
        if not frozen:
            # sys.path edits in the GUI are not inherited by a Python child.
            # Use this product's source root, never an ambient import path.
            env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
        if frozen:
            temporary = tempfile.TemporaryDirectory(prefix='cs2pov-nvidia-')
            base = Path(temporary.name).absolute()
            request_file, result_file = base/'request.json', base/'result.json'
            with request_file.open('xb') as stream: stream.write(request_bytes)
            flags += ['--request', str(request_file), '--result', str(result_file)]
        process = popen([sys.executable, *prefix, *flags],
            stdin=subprocess.DEVNULL if frozen else subprocess.PIPE,
            stdout=subprocess.DEVNULL if frozen else subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=env)
        try:
            remaining = timeout - (clock() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired('readonly_nvidia_worker', timeout)
            output, _ = process.communicate(None if frozen else request_bytes, timeout=remaining)
        except subprocess.TimeoutExpired:
            if not _reap_worker(process):
                return unknown('worker_cleanup_unconfirmed')
            return unknown('worker_timeout')
        if process.returncode != 0:
            return unknown('worker_failed')
        if frozen:
            from cs2pov.adapters.video import read_data_lease
            with read_data_lease(result_file) as signature:
                if signature.bytes > MAX_WIRE_BYTES: return unknown('worker_output_limit')
                output = result_file.read_bytes()
        if not isinstance(output, bytes) or len(output) > MAX_WIRE_BYTES:
            return unknown('worker_output_limit')
        value = snapshot_from_payload(json.loads(output.decode('utf-8'), object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))))
        return classify_snapshot(value, request, now=clock(), identity_query=identity_query, clock=clock)
    except Exception:
        if process is not None and not _reap_worker(process):
            return unknown('worker_cleanup_unconfirmed')
        return unknown('observer_unavailable')
    finally:
        if temporary is not None:
            try: temporary.cleanup()
            except OSError: pass  # A failed owned-directory cleanup grants no input.


def worker_cli(argv):
    import argparse
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--worker', action='store_true')
    group.add_argument('--raw-worker', action='store_true')
    parser.add_argument('--request', type=Path)
    parser.add_argument('--result', type=Path)
    args = parser.parse_args(argv)
    return _worker_main(raw_view=args.raw_worker, request_path=args.request, result_path=args.result)


if __name__ == '__main__':
    raise SystemExit(worker_cli(sys.argv[1:]))
