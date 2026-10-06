"""Accept read-only UIA observations for one existing RecordingTask.

This service never invokes UI, sends a key, starts a process or polls a timer.
The caller observes each captured request asynchronously, then delivers its
result. Consumed requests and confirmations are never retried. Actual NVIDIA
inputs remain the RecordingSession's separately guarded, durable operation.
"""
from copy import deepcopy
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import math
import ntpath
from pathlib import PureWindowsPath
import re
import time
import uuid

from cs2pov.adapters.nvidia_status import (ObservationRequest, NvidiaStatusEvidence,
    classify_snapshot, snapshot_to_payload)
from cs2pov.adapters.owned_process import ProcessIdentity, process_identity
from cs2pov.services.recording import RecordingScope
from cs2pov.services.recording_task import RecordingTask
from cs2pov.storage.settings import DataError


class AutomaticRecordingError(DataError):
    pass


@dataclass(frozen=True)
class OverlayWindowIdentity:
    hwnd: int
    pid: int
    visible: bool


@dataclass(frozen=True)
class ObservationStep:
    task_id: str
    session_id: str
    state: str
    confirmation_token: str
    request: ObservationRequest
    generation: int


def window_identity(hwnd):
    """Public Win32 reads only; never focuses, opens or manipulates a window."""
    if type(hwnd) is not int or hwnd <= 0:
        raise AutomaticRecordingError('NVIDIA 窗口句柄无效。')
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindow.restype = wintypes.BOOL
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsWindowVisible.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    if not api.IsWindow(hwnd) or not api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        raise AutomaticRecordingError('NVIDIA 窗口已经消失或无法核验。')
    return OverlayWindowIdentity(hwnd, int(pid.value), bool(api.IsWindowVisible(hwnd)))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _overlay_key(executable):
    if (not isinstance(executable, str) or not 0 < len(executable) <= 1024
            or any(ord(char) < 32 for char in executable)
            or not PureWindowsPath(executable).is_absolute()
            or executable.startswith(('\\\\', '//')) or '..' in PureWindowsPath(executable).parts
            or PureWindowsPath(executable).name.casefold() != 'nvidia overlay.exe'):
        raise AutomaticRecordingError('NVIDIA 观察来源必须是完整的本机 Overlay 可执行文件路径。')
    return ntpath.normcase(ntpath.normpath(executable))


class AutomaticRecording:
    MAX_AGE = 2.0
    MAX_REQUESTS = 256
    SOURCE = 'windows_uia_readonly_prototype'
    ACTIONS = {'awaiting_not_recording': ('idle', 'confirm_not_recording', 'awaiting_start_foreground'),
               'awaiting_started': ('recording', 'confirm_started', 'awaiting_play_foreground'),
               'awaiting_stopped': ('idle', 'confirm_stopped', 'awaiting_end_console')}

    def __init__(self, task, *, overlay_identity, checkpoint, component_guard,
                 identity_query=process_identity, window_query=window_identity,
                 clock=time.monotonic):
        if type(task) is not RecordingTask:
            raise AutomaticRecordingError('自动确认必须绑定现有录制协调器。')
        if (not isinstance(overlay_identity, ProcessIdentity)
                or type(overlay_identity.pid) is not int or not 0 < overlay_identity.pid <= 0xFFFFFFFF
                or type(overlay_identity.created) is not int or overlay_identity.created <= 0):
            raise AutomaticRecordingError('NVIDIA 来源缺少完整的进程身份。')
        _overlay_key(overlay_identity.executable)
        if any(not callable(value) for value in (checkpoint, component_guard, identity_query, window_query, clock)):
            raise AutomaticRecordingError('自动确认缺少持久化、组件核验或只读身份检查接口。')
        self._task = task
        self._overlay_identity = overlay_identity
        self.checkpoint, self.component_guard = checkpoint, component_guard
        self.identity_query, self.window_query, self.clock = identity_query, window_query, clock
        self._workflow, self._preview = task.workflow, task.preview
        self._task_id, self._session_id = task.task_id, task.session_id
        self._scope, self._game_identity = task.scope, task.expected_identity
        self._hotkey, self._directory = task.hotkey, task.output_directory
        self._pending = None
        self._used = set()
        self._issued = set()
        self._generation = self._journal_sequence = 0
        self._accepting = self._blocked = False
        self._idle_evidence = self._recording_evidence = self._stopped_evidence = None
        self._recording_started_at = None
        self._guard_contract()

    def _now(self):
        now = self.clock()
        if not _finite(now):
            raise AutomaticRecordingError('自动确认时钟无效。')
        return now

    def _guard_contract(self):
        task = self.task
        try:
            if (self._blocked or task.terminal or task.cleanup_requested
                    or task.workflow is not self._workflow or task.preview is not self._preview
                    or task.task_id != self._task_id or task.session_id != self._session_id
                    or task.scope != self._scope or RecordingScope.freeze(task.preview.draft) != self._scope
                    or task.expected_identity != self._game_identity or task.hotkey != self._hotkey
                    or task.output_directory != self._directory):
                raise AutomaticRecordingError('自动确认所属任务、范围或配置已经改变或结束。')
            task._contract()  # Same owned PID/-insecure/directory contract as manual confirmation.
            if self.identity_query(self.overlay_identity.pid) != self.overlay_identity:
                raise AutomaticRecordingError('NVIDIA 进程身份已经改变。')
            if self.component_guard() is not True:
                raise AutomaticRecordingError('NVIDIA 可执行文件版本或 hash 无法重新核验。')
        except AutomaticRecordingError:
            raise
        except Exception as error:
            raise AutomaticRecordingError('录制或 NVIDIA 来源身份核验失败：' + str(error)) from error

    def _guard_step(self, step):
        self._guard_contract()
        if (not isinstance(step, ObservationStep) or step.task_id != self._task_id
                or step.session_id != self._session_id or step.state not in self.ACTIONS
                or self.task.state != step.state or self.task.workflow.session.state != step.state
                or not isinstance(step.confirmation_token, str) or not step.confirmation_token
                or self.task.confirmation_token != step.confirmation_token
                or step.request.hotkey != self._hotkey
                or _overlay_key(step.request.executable) != _overlay_key(self.overlay_identity.executable)):
            raise AutomaticRecordingError('NVIDIA 观察结果不属于当前录制步骤。')
        deadline = self.task.workflow.session.deadline
        if not _finite(deadline) or self._now() >= deadline:
            raise AutomaticRecordingError('原录制确认期限已过；观察不会延长期限。')

    def capture(self):
        if self._accepting:
            raise AutomaticRecordingError('正在消费观察结果，不能重入或替换请求。')
        self._guard_contract()
        token = self.task.confirmation_token
        if self.task.state not in self.ACTIONS or not isinstance(token, str) or not token:
            raise AutomaticRecordingError('当前任务不在需要 NVIDIA 状态确认的步骤。')
        if self._generation >= self.MAX_REQUESTS:
            self._blocked = True
            raise AutomaticRecordingError('本轮观察请求已达数量限额，不能继续自动确认。')
        nonce = uuid.uuid4().hex
        if not isinstance(nonce, str) or re.fullmatch('[0-9a-f]{32}', nonce) is None or nonce in self._issued:
            self._blocked = True
            raise AutomaticRecordingError('NVIDIA 观察请求 nonce 无效或重复。')
        self._generation += 1
        self._issued.add(nonce)
        request = ObservationRequest(nonce, self.overlay_identity.executable,
                                     self._hotkey, self._now())
        step = ObservationStep(self._task_id, self._session_id, self.task.state, token,
                               request, self._generation)
        self._guard_step(step)
        self._pending = step  # A new read request revokes any prior delayed result.
        return step

    def _guard_evidence(self, step, evidence):
        self._guard_step(step)
        expected, _, _ = self.ACTIONS[step.state]
        if (type(evidence) is not NvidiaStatusEvidence or evidence.source != self.SOURCE
                or evidence.state != expected or evidence.reason != 'exact_record_section'
                or evidence.request_id != step.request.request_id
                or evidence.identity != self.overlay_identity
                or type(evidence.hwnd) is not int or evidence.hwnd <= 0
                or not _finite(evidence.observed_at)
                or not 0 <= self._now() - evidence.observed_at <= self.MAX_AGE):
            raise AutomaticRecordingError('NVIDIA 状态未知、冲突、过期或来自其他来源。')
        try:
            classified = classify_snapshot(evidence.snapshot, step.request, now=self._now(),
                                           identity_query=self.identity_query, clock=self.clock)
            if (classified.state != expected or classified.reason != evidence.reason
                    or classified.request_id != evidence.request_id
                    or classified.observed_at != evidence.observed_at
                    or classified.identity != evidence.identity or classified.hwnd != evidence.hwnd
                    or classified.source != evidence.source):
                raise AutomaticRecordingError('NVIDIA 原始观察快照不能证明当前状态。')
            window = self.window_query(evidence.hwnd)
            if (type(window) is not OverlayWindowIdentity or window.hwnd != evidence.hwnd
                    or type(window.pid) is not int or window.pid != self.overlay_identity.pid
                    or window.visible is not True):
                raise AutomaticRecordingError('NVIDIA 窗口与当前进程不一致或已不可见。')
        except AutomaticRecordingError:
            raise
        except Exception as error:
            raise AutomaticRecordingError('NVIDIA 当前窗口或快照无法重新核验。') from error
        self._guard_step(step)
        if not 0 <= self._now() - evidence.observed_at <= self.MAX_AGE:
            raise AutomaticRecordingError('NVIDIA 状态在核验过程中已过期。')
        recorder = self.task.workflow.session
        if step.state == 'awaiting_not_recording':
            if (recorder.triggered != 'none' or recorder.start_attempted_at is not None
                    or recorder.stop_attempted_at is not None or self._idle_evidence is not None):
                raise AutomaticRecordingError('未录制证据不能覆盖已经消费的录制尝试。')
        elif step.state == 'awaiting_started':
            if (self._idle_evidence is None or self._recording_evidence is not None
                    or recorder.triggered != 'start' or not _finite(recorder.start_attempted_at)
                    or evidence.snapshot.started_at < recorder.start_attempted_at
                    or evidence.observed_at <= recorder.start_attempted_at):
                raise AutomaticRecordingError('录制中证据不是本轮开始尝试之后的新状态。')
        else:
            if (self._recording_evidence is None or self._stopped_evidence is not None
                    or recorder.triggered != 'stop' or not _finite(recorder.stop_attempted_at)
                    or recorder.started_at != self._recording_started_at
                    or recorder.stop_attempted_at < self._recording_started_at
                    or evidence.snapshot.started_at < recorder.stop_attempted_at
                    or evidence.observed_at <= recorder.stop_attempted_at
                    or evidence.observed_at <= self._recording_evidence.observed_at):
                raise AutomaticRecordingError('停止需要本轮已认证录制中到新未录制状态的转换。')

    def _journal(self, step, evidence, action, status):
        self._journal_sequence += 1
        value = {'schema': 1, 'sequence': self._journal_sequence, 'status': status,
                 'task_id': self._task_id, 'session_id': self._session_id,
                 'state': step.state, 'confirmation_token': step.confirmation_token,
                 'request_id': step.request.request_id, 'request': asdict(step.request),
                 'action': action, 'source': evidence.source, 'observed_at': evidence.observed_at,
                 'overlay_identity': asdict(self.overlay_identity), 'hwnd': evidence.hwnd,
                 'scope': asdict(self._scope), 'game_identity': asdict(self._game_identity),
                 'hotkey': self._hotkey, 'output_directory': str(self._directory),
                 'task_state': self.task.state, 'recording_state': self.task.workflow.session.state,
                 'snapshot': snapshot_to_payload(evidence.snapshot)}
        try:
            self.checkpoint(deepcopy(value))
        except Exception as error:
            self._blocked = True
            raise AutomaticRecordingError('自动确认状态证据保存失败，不能重试或授权：' + str(error)) from error

    def accept(self, step, evidence):
        if (self._accepting or type(step) is not ObservationStep
                or step is not self._pending or step.request.request_id in self._used):
            raise AutomaticRecordingError('观察请求已被替换、消费或不属于本轮任务。')
        self._accepting = True
        self._pending = None
        self._used.add(step.request.request_id)
        confirmation_attempted = False
        try:
            self._guard_evidence(step, evidence)
            _, action, next_state = self.ACTIONS[step.state]
            self._journal(step, evidence, action, 'consumed')  # Durable observation precedes confirmation.
            self._guard_evidence(step, evidence)  # Persistence cannot extend TTL or change task ownership.
            confirmation_attempted = True
            getattr(self.task, action)(task_id=step.task_id, step_token=step.confirmation_token)
            self._guard_contract()
            if (self.task.state != next_state or self.task.confirmation_token is not None
                    or not 0 <= self._now() - evidence.observed_at <= self.MAX_AGE
                    or self.window_query(evidence.hwnd) != OverlayWindowIdentity(
                        evidence.hwnd, self.overlay_identity.pid, True)):
                self._blocked = True
                raise AutomaticRecordingError('确认后任务或新鲜状态无法核验，不得重复确认。')
            if step.state == 'awaiting_not_recording':
                self._idle_evidence = evidence
            elif step.state == 'awaiting_started':
                self._recording_evidence = evidence
                self._recording_started_at = self.task.workflow.session.started_at
            else:
                self._stopped_evidence = evidence
            self._journal(step, evidence, action, 'confirmed')
            return self.snapshot()
        except AutomaticRecordingError:
            if confirmation_attempted:
                self._blocked = True
            raise
        except Exception as error:
            self._blocked = True
            raise AutomaticRecordingError('自动确认未能完成；本请求不会重试：' + str(error)) from error
        finally:
            self._accepting = False

    def cancel(self):
        self._blocked = True
        self._pending = None
        return self.snapshot()

    @property
    def blocked(self):
        return self._blocked

    @property
    def task(self):
        return self._task

    @property
    def overlay_identity(self):
        return self._overlay_identity

    def snapshot(self):
        return {'schema': 1, 'task_id': self._task_id, 'session_id': self._session_id,
                'scope': asdict(self._scope), 'overlay_identity': asdict(self.overlay_identity),
                'blocked': self._blocked, 'request_count': self._generation,
                'pending_request_id': None if self._pending is None else self._pending.request.request_id,
                'consumed_requests': len(self._used),
                'idle_request_id': None if self._idle_evidence is None else self._idle_evidence.request_id,
                'recording_request_id': None if self._recording_evidence is None else self._recording_evidence.request_id,
                'stopped_request_id': None if self._stopped_evidence is None else self._stopped_evidence.request_id}
