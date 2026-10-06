"""Safe orchestration for an NVIDIA recording and its local output file.

The NVIDIA overlay is deliberately treated as an external, user-confirmed
state machine.  This service only coordinates the already guarded
``RecordingSession`` with read-only output discovery and the local library.
It never moves, renames, or deletes video files.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
import uuid

from cs2pov.adapters.video import (
    candidates_since,
    current_signature,
    VIDEO_SUFFIXES,
    probe_video,
    snapshot_directory,
    wait_stable,
)
from cs2pov.services.recording import RecordingScope, RecordingSession
from cs2pov.services.output_validation import (
    OutputDiscoveryRequest, OutputDiscoveryResult, OutputValidationRequest, OutputValidationResult,
    STOP_GRACE_SECONDS, candidate_id_for, decode_discovery_request, decode_discovery_result,
    decode_validation_request, decode_validation_result, discover_output_files,
    output_json_value, validate_output,
)
from cs2pov.storage.settings import DataError, local_path


class RecordingWorkflowError(DataError):
    """A recording cannot proceed without guessing at external state."""


@dataclass(frozen=True)
class RecordingWorkflowCheckpoint:
    state: str
    output_directory: str
    started_wall: float | None
    # Only JSON-compatible fields; no video contents are copied.
    baseline: tuple[tuple[str, int, int, int], ...]
    stopped_wall: float | None = None


class RecordingWorkflow:
    """Coordinate one confirmed recording and one user-selected output.

    The workflow intentionally exposes the confirmation steps instead of
    hiding them behind timers.  A caller can render each state in the UI and
    persist its own checkpoint through ``RecordingSession``.
    """

    OUTPUT_STATES = ('awaiting_video', 'discovering_video', 'awaiting_video_selection',
                     'awaiting_video_stable', 'validating_video', 'awaiting_content',
                     'output_cancelled', 'video_error', 'record_error', 'verified')

    def __init__(
        self,
        output_directory,
        hotkey,
        *,
        nvidia_path_confirmed,
        disk_checker,
        hotkey_adapter=None,
        game=None,
        preview_reader=None,
        end_reader=None,
        draft=None,
        expected_identity=None,
        checkpoint=None,
        workflow_checkpoint=lambda _value: None,
        session=None,
        session_factory=RecordingSession,
        snapshot=snapshot_directory,
        find_candidates=candidates_since,
        stabilize=wait_stable,
        probe=probe_video,
        signature_reader=current_signature,
        record_store=None,
        clock=time.time,
        session_clock=time.monotonic,
        confirmation_timeout=60,
        input_guard=None,
        started_wall=None,
        content_clock=time.monotonic,
        content_timeout=600,
        output_timeout=120,
    ):
        if not str(output_directory):
            raise RecordingWorkflowError('请选择明确的视频保存目录。')
        local_path(str(output_directory))
        self._output_directory = Path(output_directory).resolve()
        if not self.output_directory.is_dir():
            raise RecordingWorkflowError("视频保存目录不是有效文件夹。")
        self._snapshot = snapshot
        self._find_candidates = find_candidates
        self._stabilize = stabilize
        self._probe = probe
        self._signature_reader = signature_reader
        self.record_store = record_store
        self._clock = clock
        self._workflow_checkpoint = workflow_checkpoint
        if not callable(workflow_checkpoint):
            raise RecordingWorkflowError('必须配置有效的录制目录检查点接口。')
        if started_wall is not None:
            self._valid_wall_time(started_wall)
        self._started_wall = started_wall
        self._stopped_wall = None
        if not self._finite_number(content_timeout) or not 0 < content_timeout <= 3600:
            raise RecordingWorkflowError('成片确认的有效时间无效。')
        if not self._finite_number(output_timeout) or not 0 < output_timeout <= 300:
            raise RecordingWorkflowError('录制输出检查的有效时间无效。')
        self._content_clock, self._content_timeout = content_clock, float(content_timeout)
        self._output_timeout = float(output_timeout)
        self._output_issued = self._output_deadline = None
        self._draft_json = None
        self._frozen_scope = None
        if isinstance(draft, dict):
            self._frozen_scope = RecordingScope.freeze(draft)
            try:
                self._draft_json = json.dumps({key: draft.get(key, '' if key == 'map' else None)
                    for key in ('demo', 'fingerprint', 'selection', 'map')}, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise RecordingWorkflowError('录制草稿快照无效。') from error
        self._discovery_request = self._validation_request = None
        self._discovery_token = self._content_token = self._candidate_id = None
        self._content_issued = self._content_deadline = None
        self._record_payload = None
        self._restore_status = 'unknown'
        self.before = {}
        self.candidates = ()
        self.selected = None
        self.metadata = None
        self.record_id = None
        self.error = ""
        self.state = "idle"
        if session is None:
            self.session = session_factory(
                self.output_directory,
                hotkey,
                nvidia_path_confirmed=nvidia_path_confirmed,
                disk_checker=disk_checker,
                hotkey_adapter=hotkey_adapter,
                game=game,
                preview_reader=preview_reader,
                end_reader=end_reader,
                draft=draft,
                expected_identity=expected_identity,
                checkpoint=checkpoint,
                clock=session_clock,
                confirmation_timeout=confirmation_timeout,
                **({'input_guard': input_guard} if input_guard is not None else {}),
            )
        else:
            self.session = session
        self._task_id = getattr(self.session, 'task_id', None)

    @property
    def output_directory(self):
        return self._output_directory

    @property
    def confirmation_token(self):
        return self.session.confirmation_token

    @property
    def started_wall(self):
        return self._started_wall

    @property
    def stopped_wall(self):
        return self._stopped_wall

    @property
    def task_id(self):
        return self._task_id

    @property
    def discovery_token(self):
        return self._discovery_token

    @property
    def content_token(self):
        return self._content_token

    @property
    def candidate_id(self):
        return self._candidate_id

    @property
    def draft_snapshot(self):
        return json.loads(self._draft_json) if self._draft_json is not None else None

    @staticmethod
    def _finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _valid_wall_time(value):
        if not RecordingWorkflow._finite_number(value) or value < 0:
            raise RecordingWorkflowError('录制输出的开始时间无效，未发送热键。')
        return value

    def _save_context(self):
        value = RecordingWorkflowCheckpoint(self.state, str(self.output_directory), self._started_wall,
            tuple((str(item.path), item.bytes, item.mtime_ns, item.ctime_ns)
                  for item in sorted(self.before.values(), key=lambda item: str(item.path))), self._stopped_wall)
        try:
            self._workflow_checkpoint(value)
        except Exception as error:
            self.error = '录制目录检查点保存失败：' + str(error)
            # These writes happen before input. Consume the task instead of
            # letting a later UI poll turn a failed save into a retry.
            if self.session.state == 'stopped':
                self._revoke_output()
                self.state = 'record_error'
            else:
                try:
                    self.session.cancel()
                except Exception as cancel_error:
                    self.error += '\n取消状态保存失败：' + str(cancel_error)
            self._sync()
            raise RecordingWorkflowError(self.error) from error

    def _sync(self):
        # Stopped is the parent state of all output substeps. Timer polling
        # must not overwrite candidate selection, verification or its errors.
        if self.session.state != 'stopped' or self.state not in self.OUTPUT_STATES:
            if self.session.state != 'stopped':
                self._revoke_output()
            self.state = self.session.state
        return self.state

    def _delegate(self, name, *args, **kwargs):
        try:
            return getattr(self.session, name)(*args, **kwargs)
        finally:
            # A guarded session can become unknown and then raise. The UI
            # must see that state before a second click or any output action.
            self._sync()

    def _require_stopped(self):
        self._sync()
        if self.session.state != 'stopped':
            raise RecordingWorkflowError('必须先确认 NVIDIA 已停止录制。')

    def arm(self, preview_proof):
        if self.state != "idle":
            raise RecordingWorkflowError("录制流程已经开始，不能重复准备。")
        try:
            # The baseline is read before the first confirmation so an old
            # file can never be associated with this task.
            self.before = self._snapshot(self.output_directory)
            self._save_context()
            self._delegate('arm', preview_proof)
        except Exception:
            self.before = {}
            raise
        self._sync()
        return self.state

    def confirm_not_recording(self, *, step_token):
        self._delegate('confirm_not_recording', step_token=step_token)
        return self.state

    def send_start(self):
        self._sync()
        if self.state != 'start_ready':
            raise RecordingWorkflowError('必须先确认 NVIDIA 当前未录制。')
        # Freeze and persist before SendInput. A quickly written output must
        # not be rejected because its mtime precedes a post-input timestamp.
        self._started_wall = self._valid_wall_time(self._clock())
        self._save_context()
        self._delegate('send_start')
        return self.state

    def confirm_started(self, *, step_token):
        self._delegate('confirm_started', step_token=step_token)
        return self.state

    def mark_end(self, end_proof):
        self._delegate('mark_end', end_proof)
        return self.state

    def send_stop(self):
        self._sync()
        if self.state != 'stop_ready':
            raise RecordingWorkflowError('必须先确认已到达本次录制的结束范围。')
        stopped = self._valid_wall_time(self._clock())
        if self._started_wall is None or stopped < self._started_wall:
            raise RecordingWorkflowError('录制停止时间无效，未发送热键。')
        self._stopped_wall = stopped
        self._save_context()
        self._delegate('send_stop')
        return self.state

    def confirm_stopped(self, *, step_token):
        self._delegate('confirm_stopped', step_token=step_token)
        self._require_stopped()
        self.state = "awaiting_video"
        return self.state

    def cancel(self):
        if self.session.state == 'stopped':
            return self.cancel_output_validation()
        self._delegate('cancel')
        return self.state

    def poll(self):
        self._delegate('poll')
        return self.state

    def _revoke_output(self):
        self._discovery_request = self._validation_request = None
        self._discovery_token = self._content_token = None
        self._content_issued = self._content_deadline = None
        self._output_issued = self._output_deadline = None

    def _begin_output_deadline(self):
        now = self._content_clock()
        if not self._finite_number(now) or now < 0:
            raise RecordingWorkflowError('录制输出检查时钟无效。')
        self._output_issued, self._output_deadline = now, now + self._output_timeout

    def _require_output_scope(self):
        self._require_stopped()
        if self._draft_json is None or self._task_id != getattr(self.session, 'task_id', None):
            raise RecordingWorkflowError('录制输出缺少本任务的冻结草稿或身份。')
        if getattr(self.session, 'scope', self._frozen_scope) != self._frozen_scope:
            raise RecordingWorkflowError('录制输出的冻结范围与本任务不一致。')
        self._valid_wall_time(self._started_wall)
        self._valid_wall_time(self._stopped_wall)
        if self._stopped_wall < self._started_wall:
            raise RecordingWorkflowError('录制停止时间早于开始时间。')

    def begin_output_discovery(self):
        self._require_output_scope()
        if self.state not in ('awaiting_video', 'video_error', 'record_error', 'output_cancelled'):
            raise RecordingWorkflowError('当前不能开始发现录制输出。')
        self._revoke_output()
        request = OutputDiscoveryRequest(self.task_id, uuid.uuid4().hex, str(self.output_directory),
            self._started_wall, self._stopped_wall, tuple(self.before.values()))
        request = decode_discovery_request(output_json_value(request))
        self._begin_output_deadline()
        self._discovery_request = request
        self._discovery_token = request.request_id
        self.candidates, self.selected, self.metadata = (), None, None
        self._candidate_id = None
        self.error = ''
        self.state = 'discovering_video'
        self._save_context()
        return request

    def _require_request(self, request, current, expected_state):
        self._require_output_scope()
        if current is None or request != current or self.state != expected_state:
            raise RecordingWorkflowError('录制输出任务已取消、过期或不属于当前步骤。')
        now = self._content_clock()
        if not self._finite_number(now) or not self._output_issued <= now < self._output_deadline:
            self._revoke_output()
            self.state, self.error = 'video_error', '录制输出检查已过期，请重新检查视频。'
            self._save_context()
            raise RecordingWorkflowError(self.error)

    def commit_output_discovery(self, request, result):
        self._require_request(request, self._discovery_request, 'discovering_video')
        result = decode_discovery_result(output_json_value(result))
        if (result.task_id, result.request_id) != (request.task_id, request.request_id):
            raise RecordingWorkflowError('候选视频结果不属于本任务。')
        directory = self.output_directory
        for item in result.candidates:
            try:
                depth = len(item.path.relative_to(directory).parts)
            except ValueError as error:
                raise RecordingWorkflowError('候选视频超出本任务的输出目录。') from error
            if depth not in (1, 2) or item.path.suffix.casefold() not in VIDEO_SUFFIXES or self.before.get(item.path) == item or not any(
                request.started_wall <= timestamp / 1_000_000_000 <= request.stopped_wall + STOP_GRACE_SECONDS
                for timestamp in (item.ctime_ns, item.mtime_ns)):
                raise RecordingWorkflowError('候选视频超出本任务的冻结时间或目录范围。')
        self._require_request(request, self._discovery_request, 'discovering_video')
        self._discovery_request = None
        self._output_issued = self._output_deadline = None
        self.candidates = result.candidates
        self.selected = None
        self.state = 'awaiting_video_selection' if len(self.candidates) > 1 else 'awaiting_video_stable'
        if not self.candidates:
            self.state, self.error = 'awaiting_video', '没有发现本次录制产生的新视频。'
        self._save_context()
        if not self.candidates:
            raise RecordingWorkflowError(self.error)
        return self.candidates

    def fail_output_discovery(self, request, error):
        self._require_request(request, self._discovery_request, 'discovering_video')
        self._revoke_output()
        self.state, self.error = 'video_error', '读取录制目录失败：' + str(error)
        self._save_context()
        return self.state

    def discover_outputs(self):
        self._require_stopped()
        request = self.begin_output_discovery()
        try:
            result = discover_output_files(request, finder=self._find_candidates)
        except Exception as error:
            self.fail_output_discovery(request, error)
            raise RecordingWorkflowError(self.error) from error
        return self.commit_output_discovery(request, result)

    def choose_output(self, path, *, task_id=None, step_token=None):
        self._require_output_scope()
        if self.state != "awaiting_video_selection":
            raise RecordingWorkflowError("当前没有等待选择的视频。")
        if (task_id is not None and task_id != self.task_id) or (step_token is not None and step_token != self.discovery_token):
            raise RecordingWorkflowError('视频选择不属于本任务的当前发现步骤。')
        local_path(str(path))
        if '..' in Path(path).parts:
            raise RecordingWorkflowError('只能选择列表中明确显示的候选视频路径。')
        candidate = Path(path).absolute()
        matches = [item for item in self.candidates if item.path == candidate]
        if len(matches) != 1:
            raise RecordingWorkflowError("只能选择本次新发现的候选视频。")
        self.selected = matches[0]
        self.state = "awaiting_video_stable"
        self._save_context()
        return self.selected

    def begin_output_validation(self):
        self._require_output_scope()
        if self.state != "awaiting_video_stable":
            raise RecordingWorkflowError("请先发现并选择唯一的视频候选。")
        if self.selected is None:
            if len(self.candidates) != 1:
                raise RecordingWorkflowError("多个候选视频必须由使用者明确选择。")
            self.selected = self.candidates[0]
        request = decode_validation_request(output_json_value(OutputValidationRequest(self.task_id, uuid.uuid4().hex,
            str(self.output_directory), self.selected, self._started_wall, self._stopped_wall)))
        self._begin_output_deadline()
        self._validation_request = request
        self.state, self.error = 'validating_video', ''
        self._save_context()
        return request

    def fail_output_validation(self, request, error):
        self._require_request(request, self._validation_request, 'validating_video')
        self._revoke_output()
        self.state, self.error = 'video_error', '录制文件检查失败：' + str(error)
        self._save_context()
        return self.state

    def cancel_output_validation(self):
        self._require_stopped()
        if self.state == 'verified':
            raise RecordingWorkflowError('已确认的成片不能重复取消。')
        self._revoke_output()
        self.state, self.error = 'output_cancelled', '录制输出检查已取消；原视频保留。'
        self._save_context()
        return self.state

    def _check_current_file(self, metadata):
        current = self._signature_reader(metadata.path, directory=self.output_directory)
        if current != metadata.signature:
            raise RecordingWorkflowError('视频文件在探测或内容确认后变化，请重新检查。')

    def _save_record(self, record):
        if self.record_store is not None:
            try:
                self.record_store.save_record(self.record_id, record, self._restore_status)
            except Exception as error:
                self.error = '录制记录保存失败：' + str(error)
                self.state = 'record_error'
                self._revoke_output()
                raise RecordingWorkflowError(self.error) from error

    def commit_output_validation(self, request, result, *, record_id=None, payload=None, restore_status='unknown'):
        self._require_request(request, self._validation_request, 'validating_video')
        result = decode_validation_result(output_json_value(result))
        metadata = result.metadata
        if ((result.task_id, result.request_id) != (request.task_id, request.request_id)
                or metadata.path != request.candidate.path or metadata.signature.identity != request.candidate.identity):
            raise RecordingWorkflowError('视频探测结果不属于本任务选择的候选。')
        try:
            self._check_current_file(metadata)
        except Exception as error:
            self.fail_output_validation(request, error)
            raise RecordingWorkflowError(self.error) from error
        self._require_request(request, self._validation_request, 'validating_video')
        if restore_status not in ('unknown', 'complete', 'blocked'):
            raise RecordingWorkflowError('独立恢复结果无效。')
        snapshot = self.draft_snapshot
        candidate_id = candidate_id_for(self.task_id, metadata.signature)
        protected = dict(video=str(metadata.path), duration=metadata.duration, width=metadata.width,
            height=metadata.height, video_streams=metadata.video_streams, audio_streams=metadata.audio_streams,
            recording_state='stopped', video_result='metadata_verified', metadata_result='verified',
            content_result='unreviewed', draft_snapshot=snapshot, demo=snapshot['demo'],
            player=snapshot['selection']['player_id'], map=snapshot['map'], task_id=self.task_id,
            candidate_id=candidate_id, video_signature=output_json_value(metadata.signature), restore_status=restore_status)
        if payload is not None and type(payload) is not dict:
            raise RecordingWorkflowError('录制记录的附加内容必须是对象。')
        record = dict(payload or {})
        if 'content_confirmation' in record or any(key in record and record[key] != value for key, value in protected.items()):
            raise RecordingWorkflowError('附加内容不能覆盖本任务冻结的录制摘要或状态。')
        record.update(protected)
        try:
            serialized = json.dumps(record, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode('utf-8')) > 1_048_576:
                raise ValueError('record exceeds size limit')
        except (TypeError, ValueError, RecursionError) as error:
            raise RecordingWorkflowError('录制记录的附加内容不是有效的有界 JSON。') from error
        self._require_request(request, self._validation_request, 'validating_video')
        self.metadata = metadata
        self.record_id = record_id or uuid.uuid4().hex
        self._restore_status = restore_status
        self._candidate_id = candidate_id
        self._record_payload = serialized
        self._save_record(record)
        self._require_request(request, self._validation_request, 'validating_video')
        self._validation_request = None
        self._output_issued = self._output_deadline = None
        issued = self._content_clock()
        if not self._finite_number(issued) or issued < 0:
            self.state = 'video_error'
            raise RecordingWorkflowError('成片确认时钟无效。')
        self._content_issued, self._content_deadline = issued, issued + self._content_timeout
        self._content_token = uuid.uuid4().hex
        self.state = 'awaiting_content'
        self._save_context()
        return self.metadata

    def verify_output(self, *, record_id=None, payload=None, restore_status='unknown'):
        self._require_stopped()
        if self.state == 'awaiting_video':
            self.discover_outputs()
        request = self.begin_output_validation()
        try:
            result = validate_output(request, stabilize=self._stabilize, probe=self._probe)
        except Exception as error:
            self.fail_output_validation(request, error)
            raise RecordingWorkflowError(self.error) from error
        return self.commit_output_validation(request, result, record_id=record_id, payload=payload, restore_status=restore_status)

    def confirm_content(self, *, task_id, candidate_id, step_token, target_view_confirmed,
                        hud_confirmed, audio_confirmed, range_confirmed, clean_picture_confirmed,
                        head_seconds, tail_seconds):
        self._require_content(task_id, candidate_id, step_token)
        if any(value is not True for value in (target_view_confirmed, hud_confirmed, audio_confirmed,
                                               range_confirmed, clean_picture_confirmed)):
            raise RecordingWorkflowError('必须人工确认目标视角、HUD、可听声音、完整区间及无控制台或跳转画面。')
        if any(not self._finite_number(value) or not 0 <= value <= 2 for value in (head_seconds, tail_seconds)):
            raise RecordingWorkflowError('片头和片尾各自最多允许 2 秒余量。')
        try:
            self._check_current_file(self.metadata)
        except Exception as error:
            self._revoke_output()
            self.state, self.error = 'video_error', '成片确认的文件已变化：' + str(error)
            self._save_context()
            raise RecordingWorkflowError(self.error) from error
        # A file identity read can block. Its return must not revive a token
        # that expired or was cancelled while that port was working.
        self._require_content(task_id, candidate_id, step_token)
        record = json.loads(self._record_payload)
        record.update(video_result='verified', content_result='verified', content_confirmation=dict(
            task_id=task_id, candidate_id=candidate_id, step_token=step_token,
            target_view_confirmed=True, hud_confirmed=True, audio_confirmed=True,
            range_confirmed=True, clean_picture_confirmed=True,
            head_seconds=float(head_seconds), tail_seconds=float(tail_seconds),
            confirmed_at=self._valid_wall_time(self._clock())))
        self._require_content(task_id, candidate_id, step_token)
        self._revoke_output()
        self._save_record(record)
        self._record_payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if self.state != 'awaiting_content':
            raise RecordingWorkflowError('成片确认在保存期间被取消。')
        self._require_output_scope()
        self.state = 'verified'
        self._save_context()
        return self.metadata

    def _require_content(self, task_id, candidate_id, step_token):
        self._require_output_scope()
        if (self.state != 'awaiting_content' or self.content_token is None
                or (task_id, candidate_id, step_token) != (self.task_id, self.candidate_id, self.content_token)):
            raise RecordingWorkflowError('成片确认已取消、过期或不属于本任务的视频。')
        now = self._content_clock()
        if not self._finite_number(now) or not self._content_issued <= now < self._content_deadline:
            self._revoke_output()
            self.state, self.error = 'video_error', '成片确认已过期，请重新检查视频。'
            self._save_context()
            raise RecordingWorkflowError(self.error)
