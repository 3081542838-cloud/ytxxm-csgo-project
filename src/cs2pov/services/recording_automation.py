"""Qt bridge for fresh read-only status and the existing guarded task.

No input is implemented here. Unknown data immediately revokes the input
guard; a new read never retries a consumed NVIDIA toggle.
"""
import ctypes
from ctypes import wintypes
from dataclasses import asdict
import hashlib
from pathlib import Path
import time
import uuid

from cs2pov.adapters.nvidia_status import ObservationRequest, classify_snapshot
from cs2pov.adapters.owned_process import process_identity
from cs2pov.adapters.video import read_lease
from cs2pov.services.automatic_recording import AutomaticRecording, window_identity
from cs2pov.services.environment_receipt import read_file_version
from cs2pov.services.recording import RecordingScope
from cs2pov.storage.settings import DataError

NVIDIA_COMPONENT_VERSION = '128.4.13.34'
NVIDIA_OVERLAY_SHA256 = '8e2053aef3f72c0d2cf3af88da7216ab5504156eed07d8367eb243630f2c1cae'
NVIDIA_APP_SHA256 = '561e976bf741a5a540060e37408afa1cebb14765d6d4c8c5c5208c23d948c263'

def overlay_executable():
    import os
    return Path(os.environ.get('ProgramFiles', r'C:\Program Files'))/'NVIDIA Corporation/NVIDIA App/CEF/NVIDIA Overlay.exe'

def component_guard(executable):
    """Only this locally observed provider build is eligible for automation."""
    for path, expected in ((Path(executable), NVIDIA_OVERLAY_SHA256),
                           (Path(executable).with_name('NVIDIA App.exe'), NVIDIA_APP_SHA256)):
        with read_lease(path):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected: return False
            if read_file_version(path) != NVIDIA_COMPONENT_VERSION: return False
    return True

def visible_overlay_identity(executable):
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.IsWindowVisible.argtypes=[wintypes.HWND]; api.IsWindowVisible.restype=wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype=wintypes.DWORD
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    api.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]; api.EnumWindows.restype=wintypes.BOOL
    identities={}
    def collect(hwnd, _):
        if api.IsWindowVisible(hwnd):
            pid=wintypes.DWORD(); api.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            try:
                identity=process_identity(pid.value)
                if Path(identity.executable)==Path(executable): identities[identity.pid]=identity
            except Exception: pass
        return True
    if not api.EnumWindows(callback_type(collect),0) or len(identities)!=1:
        raise DataError('未找到唯一可核验的 NVIDIA 浮窗进程；请先打开 NVIDIA 浮窗再收起。')
    return next(iter(identities.values()))

class RecordingAutomation:
    def __init__(self, task, monitor, checkpoint, *, identity=None,
                 guard=None, clock=time.monotonic, overlay_cycle_factory=None):
        self.task,self.monitor,self.clock=task,monitor,clock
        self.identity=identity or visible_overlay_identity(overlay_executable())
        self.guard=guard or (lambda:component_guard(self.identity.executable))
        self.coordinator=AutomaticRecording(task,overlay_identity=self.identity,
            checkpoint=checkpoint,component_guard=self.guard,clock=clock)
        self.scope,self.game_identity=task.scope,task.expected_identity
        self.latest=self.request=None
        self.active=True
        self.error=''
        self.overlay_cycle = (overlay_cycle_factory(monitor, self._cycle_guard)
                              if overlay_cycle_factory else None)
        self._cycle_contract = None

    def _cycle_guard(self):
        try:
            task = self.task
            frozen = self._cycle_contract
            if (not self.active or frozen is None or task.terminal or task.cleanup_requested
                    or frozen != (task.state, task.scope, task.expected_identity,
                                   task._input_deadline, task.workflow.session.deadline)
                    or task.scope != self.scope or self.guard() is not True
                    or process_identity(self.identity.pid) != self.identity):
                return False
            task._contract()
            task._endpoint(at_end=task.state in ('awaiting_stop_foreground', 'awaiting_stopped'))
            return frozen == (task.state, task.scope, task.expected_identity,
                               task._input_deadline, task.workflow.session.deadline)
        except Exception:
            return False

    def input_guard(self, expected):
        try:
            task=self.task; evidence=self.latest; request=self.request
            deadline=task._input_deadline
            frozen=(task.preview,task.workflow,task.state,task.scope,task.expected_identity,task.hotkey,task.output_directory)
            if (not self.active or self.coordinator.blocked or task.terminal or task.cleanup_requested or evidence is None
                    or deadline is None or self.clock()>=deadline
                    or request is None or evidence.state!=expected or evidence.identity!=self.identity
                    or evidence.request_id!=request.request_id or task.scope!=self.scope
                    or RecordingScope.freeze(task.preview.draft)!=self.scope
                    or task.expected_identity!=self.game_identity or task.hotkey!=request.hotkey
                    or self.guard() is not True): return False
            result=classify_snapshot(evidence.snapshot,request,now=self.clock(),clock=self.clock)
            if result!=evidence or process_identity(self.identity.pid)!=self.identity: return False
            window=window_identity(evidence.hwnd)
            if not window.visible or window.pid!=self.identity.pid: return False
            recorder=task.workflow.session
            if expected=='recording':
                if (recorder.start_attempted_at is None or evidence.snapshot.started_at<recorder.start_attempted_at
                        or evidence.observed_at<=recorder.start_attempted_at): return False
            task._contract()
            return (self.active and not self.coordinator.blocked and self.task is task
                and not task.terminal and not task.cleanup_requested
                and self.latest is evidence and self.request is request
                and frozen==(task.preview,task.workflow,task.state,task.scope,task.expected_identity,task.hotkey,task.output_directory)
                and RecordingScope.freeze(task.preview.draft)==self.scope
                and task._input_deadline==deadline
                and self.clock()<deadline and 0<=self.clock()-evidence.observed_at<=2)
        except Exception:
            return False

    def poll(self):
        task=self.task
        if not self.active or task.terminal or task.cleanup_requested or self.monitor.busy: return
        if task.state not in (*AutomaticRecording.ACTIONS,'awaiting_start_foreground',
                             'awaiting_play_foreground','recording','awaiting_stop_foreground'): return
        if self.overlay_cycle is not None:
            if self.overlay_cycle.active or self.overlay_cycle.failed: return
            if task.state in ('recording', 'awaiting_play_foreground'): return
            # A completed cycle already supplies the pending input's evidence.
            expected = 'idle' if task.state == 'awaiting_start_foreground' else 'recording'
            if task.state in ('awaiting_start_foreground', 'awaiting_stop_foreground') and self.input_guard(expected):
                return
        try:
            step=self.coordinator.capture() if task.state in AutomaticRecording.ACTIONS else None
            request=step.request if step else ObservationRequest(uuid.uuid4().hex,
                self.identity.executable,task.hotkey,self.clock())
            captured=(task.task_id,task.session_id,task.scope,task.expected_identity)
            def result(evidence):
                if (not self.active or task.terminal or task.cleanup_requested
                        or captured!=(task.task_id,task.session_id,task.scope,task.expected_identity)): return
                self.latest,self.request=evidence,request
                if self.overlay_cycle is not None and self.overlay_cycle.failed:
                    self.error=evidence.reason
                    task.cancel()
                    return
                if step is not None and task.state==step.state:
                    expected=AutomaticRecording.ACTIONS[step.state][0]
                    if evidence.state==expected:
                        try: self.coordinator.accept(step,evidence)
                        except Exception as error:
                            self.error=str(error); task.cancel()
                elif task.state=='recording' and evidence.state=='idle':
                    self.error='录制过程中 NVIDIA 已显示未录制；禁止再次切换热键，请检查成片。'
                    task.cancel()
            if self.overlay_cycle is None:
                self.monitor.start(request,result)
            else:
                self._cycle_contract=(task.state, task.scope, task.expected_identity,
                                      task._input_deadline, task.workflow.session.deadline)
                deadline=(task._input_deadline if task.state in ('awaiting_start_foreground','awaiting_stop_foreground')
                          else task.workflow.session.deadline)
                self.overlay_cycle.start(request,result,identity=self.identity,deadline=deadline)
        except Exception as error:
            self.error=str(error); self.latest=None; task.cancel()

    def cancel(self):
        self.active=False; self.latest=None
        if self.overlay_cycle is not None: self.overlay_cycle.cancel()
        else: self.monitor.cancel()
