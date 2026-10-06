"""One NVIDIA attempt on the existing preview, game and file transaction.

``complete`` means execution and file cleanup have settled; NVIDIA status is
reported independently.  Consumed input is never retried, including after a
checkpoint error.  This coordinator does not launch games or probe videos.
"""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import time

from cs2pov.adapters.nvidia import ensure_playback_hotkey_compatible
from cs2pov.services.recording import RecordingScope
from cs2pov.storage.settings import DataError


class RecordingTaskError(DataError):
    pass


class RecordingTask:
    START_FOREGROUND_SECONDS = 30
    STOP_FOREGROUND_SECONDS = 2
    CLEANUP_SECONDS = 15

    def __init__(self, preview, workflow, *, checkpoint, clock=time.monotonic):
        if not callable(checkpoint) or not callable(clock):
            raise RecordingTaskError('录制协调器必须配置有效持久化接口和时钟。')
        self.preview = preview
        self.workflow = workflow
        self.checkpoint = checkpoint
        self.clock = clock
        self.task_id = workflow.session.task_id
        self.session_id = preview.session.name
        self.scope = workflow.session.scope
        self.expected_identity = workflow.session.expected_identity
        self.hotkey = workflow.session.hotkey
        self.output_directory = workflow.output_directory
        self.state = 'idle'
        self.error = ''
        self.cleanup_requested = False
        self.terminal = False
        self.durable = False
        self._persistence_failed = False
        self._input_deadline = self._cleanup_deadline = None
        self._polling = False
        self.automatic_input_ready = None

    @property
    def confirmation_token(self):
        return self.workflow.confirmation_token

    def snapshot(self):
        recorder = self.workflow.session
        if self.state in ('awaiting_start_foreground', 'awaiting_stop_foreground'):
            deadline = self._input_deadline
        elif self.state == 'closing':
            deadline = self._cleanup_deadline
        elif self.state in ('awaiting_end_console', 'checking_binding'):
            deadline = getattr(self.preview, 'deadline', None)
        else:
            deadline = recorder.deadline
        restored = ('complete' if self.preview.state == 'complete'
                    else 'blocked' if self.preview.state == 'recovery_blocked' else 'unknown')
        return dict(schema=1, task_id=self.task_id, session_id=self.session_id,
            dispatch_mode=getattr(self, 'dispatch_mode', 'confirmed'),
            nvidia_state_verified=getattr(self, 'dispatch_mode', 'confirmed') != 'direct_hotkey',
            state=self.state, error=self.error, cleanup_requested=self.cleanup_requested,
            terminal=self.terminal, durable=self.durable,
            recording_state=recorder.state, triggered=recorder.triggered,
            started_at=recorder.started_at, stopped_at=recorder.stopped_at,
            start_attempted_at=recorder.start_attempted_at,
            stop_attempted_at=recorder.stop_attempted_at,
            start_not_sent=getattr(recorder, 'start_not_sent', False),
            confirmation_token=self.confirmation_token, deadline=deadline,
            preview_state=self.preview.state, restore_status=restored,
            scope=asdict(self.scope), expected_identity=asdict(self.expected_identity),
            hotkey=self.hotkey, output_directory=str(self.output_directory))

    def _save(self):
        value = self.snapshot()
        # The callback persists this candidate. Any observed failure is sticky
        # even if a later cleanup checkpoint can be written successfully.
        value['durable'] = not self._persistence_failed
        try:
            self.checkpoint(value)
        except Exception as error:
            self._persistence_failed = True
            self.durable = False
            raise RecordingTaskError('录制任务检查点保存失败：' + str(error)) from error
        self.durable = not self._persistence_failed

    def _add_error(self, reason):
        if reason and reason not in self.error:
            self.error += ('\n' if self.error else '') + str(reason)

    def _save_cleanup(self):
        try:
            self._save()
        except Exception as error:
            self._add_error(str(error))

    def _same_task(self, task_id):
        if task_id != self.task_id or self.terminal or self.cleanup_requested:
            raise RecordingTaskError('确认属于其他任务或已结束任务，不能授权当前操作。')

    def _confirmation(self, expected, task_id, step_token):
        self._same_task(task_id)
        if (self.state != expected or not isinstance(step_token, str) or not step_token
                or step_token != self.confirmation_token):
            raise RecordingTaskError('确认不属于当前录制步骤，或已经重复/过期。')

    def _contract(self):
        recorder = self.workflow.session
        preview = self.preview
        ensure_playback_hotkey_compatible(self.hotkey)
        if (preview.session.name != self.session_id or recorder.task_id != self.task_id
                or recorder.scope != self.scope or RecordingScope.freeze(preview.draft) != self.scope
                or recorder.expected_identity != self.expected_identity
                or recorder.game is not preview.game or preview.controller.game is not preview.game
                or recorder.hotkey != self.hotkey or preview.settings.hotkey != self.hotkey
                or self.workflow.output_directory != self.output_directory
                or recorder.output_directory.resolve() != self.output_directory.resolve()
                or Path(preview.settings.video_directory).resolve() != self.output_directory.resolve()
                or recorder.nvidia_path_confirmed is not True
                or preview.settings.nvidia_path_confirmed is not True
                or preview.game.verify() != self.expected_identity
                or '-insecure' not in (preview.game.argv or [])):
            raise RecordingTaskError('录制范围、设置或受管理游戏与冻结的预览任务不一致。')

    def _ready_start(self):
        playback = self.preview.playback
        from cs2pov.adapters.pipe_console import PipeConsole
        expected_binding = ('unbind F8; demo_resume' if type(self.preview.console) is PipeConsole
                            else f'unbind F8; demo_resume; demo_pauseatservertick {self.scope.server_end_tick}')
        if (self.preview.state != 'play_ready' or playback is None
                or playback.state != 'ready' or playback.binding_may_exist is not True
                or playback.triggered is not False
                or playback.binding != expected_binding
                or self.preview.console.console_open_confirmed):
            raise RecordingTaskError('必须先核验起点、准确播放绑定并收起控制台，才能准备录制。')

    def _endpoint(self, *, at_end=False):
        recorder = self.workflow.session
        proof = self.preview.controller.verify_result(self.preview.draft,
            since=recorder.started_at if at_end else self.clock()-5,
            at_end=at_end, require_foreground=False)
        # Reuse the same strict boolean, identity, dual-tick and timestamp
        # checks as recorder input. Readback alone never opens the console.
        return recorder._proof(proof, at_end=at_end)

    def _foreground(self):
        if self.preview.console.foreground_pid() != self.expected_identity.pid:
            return False
        input_ready = getattr(self.preview.console, 'input_ready', None)
        return not callable(input_ready) or input_ready() is True

    @contextmanager
    def _bounded_reader(self, name):
        recorder = self.workflow.session
        original = getattr(recorder, name)
        deadline = self._input_deadline

        def read():
            if deadline is None or self.clock() >= deadline:
                raise RecordingTaskError('输入前台等待期限已过；不发送或重试热键。')
            result = original()
            if self.clock() >= deadline:
                raise RecordingTaskError('端点回读耗时超出输入期限；不发送或重试热键。')
            return result

        setattr(recorder, name, read)
        try:
            yield
        finally:
            setattr(recorder, name, original)

    def _settle_cleanup(self):
        if self.preview.state == 'complete':
            self.state, self.terminal = 'complete', True
        elif self.preview.state == 'recovery_blocked':
            self.state, self.terminal = 'recovery_blocked', True
            self._add_error(self.preview.error or '游戏文件恢复未全部通过，保留备份。')
        elif self.preview.state != 'closing':
            self.state, self.terminal = 'recovery_blocked', True
            self._add_error('游戏退出或恢复无法确认，保留会话并阻断下一次准备。')
        elif self.clock() >= self._cleanup_deadline:
            self.state, self.terminal = 'recovery_blocked', True
            self._add_error('游戏退出及文件恢复超过收尾期限，保留备份。')
        if self.terminal:
            self._save_cleanup()

    def _cleanup(self, reason=''):
        self._add_error(reason)
        if self.cleanup_requested:
            return
        self.cleanup_requested = True
        self.state = 'closing'
        self._input_deadline = None
        self._cleanup_deadline = self.clock()+self.CLEANUP_SECONDS
        self._save_cleanup()
        try:
            if self.workflow.session.state not in ('stopped', 'unknown', 'cancelled'):
                self.workflow.cancel()
        except Exception as error:
            self._add_error('录制收尾状态保存失败：' + str(error))
        if self.workflow.session.state == 'unknown':
            self._add_error(self.workflow.session.reason)
            self._add_error('NVIDIA 录制状态未知，请手动检查并停止录制；应用不会重发热键。')
        # Checkpoint and NVIDIA failures must never prevent attempting the
        # held PreviewSession's owned-process close and file restoration.
        try:
            self.preview.stop()
        except Exception as error:
            self._add_error('游戏及文件收尾失败：' + str(error))
        self._settle_cleanup()

    def _operation_error(self, error):
        self._cleanup(str(error))
        return RecordingTaskError(self.error)

    def arm(self):
        if self.state != 'idle':
            raise RecordingTaskError('录制任务已准备，不能重复准备。')
        try:
            self._contract()
            self._ready_start()
            proof = self._endpoint()
            self.state = 'awaiting_not_recording'
            self._save()
            self.workflow.arm(proof)
            self._save()
        except Exception as error:
            raise self._operation_error(error) from error
        return self.snapshot()

    def confirm_not_recording(self, *, task_id, step_token):
        self._confirmation('awaiting_not_recording', task_id, step_token)
        try:
            self.workflow.confirm_not_recording(step_token=step_token)
            self._contract()
            self._ready_start()
            self._endpoint()
            self.state = 'awaiting_start_foreground'
            self._input_deadline = self.clock()+self.START_FOREGROUND_SECONDS
            self._save()
        except Exception as error:
            raise self._operation_error(error) from error
        return self.snapshot()

    def confirm_started(self, *, task_id, step_token):
        self._confirmation('awaiting_started', task_id, step_token)
        try:
            self.workflow.confirm_started(step_token=step_token)
            self._contract()
            self._ready_start()
            self._endpoint()
            self.state = 'awaiting_play_foreground'
            self._save()
            self.preview.play_clip()
            self._save()
        except Exception as error:
            raise self._operation_error(error) from error
        return self.snapshot()

    def confirm_stopped(self, *, task_id, step_token):
        self._confirmation('awaiting_stopped', task_id, step_token)
        try:
            self.workflow.confirm_stopped(step_token=step_token)
            if self.workflow.session.state != 'stopped':
                raise RecordingTaskError('NVIDIA 尚未明确确认停止，不能检查控制台。')
            self.state = 'awaiting_end_console'
            self._save()
            self.preview.check_playback_binding()
            self._save()
        except Exception as error:
            raise self._operation_error(error) from error
        return self.snapshot()

    def confirm_console(self, *, task_id, session_id, step_token):
        self._same_task(task_id)
        if (self.state != 'awaiting_end_console' or self.workflow.session.state != 'stopped'
                or session_id != self.session_id or self.preview.session.name != self.session_id
                or self.preview.state != 'awaiting_end_console' or self.preview.console_confirmed
                or not isinstance(step_token, str) or not step_token
                or step_token != self.preview.console_step_token):
            raise RecordingTaskError('控制台确认不属于已停录任务的当前收尾步骤。')
        try:
            self.preview.confirm_console(step_token=step_token)
            self._save()
        except Exception as error:
            raise self._operation_error(error) from error
        return self.snapshot()

    def _send_stop(self):
        if self.clock() >= self._input_deadline:
            raise RecordingTaskError('终点停录前台等待超过 2 秒；NVIDIA 状态未知，请手动停止。')
        self._endpoint(at_end=True)
        if not self._foreground():
            return
        if self.automatic_input_ready is not None and self.automatic_input_ready('recording') is not True:
            return
        with self._bounded_reader('end_reader'):
            self.workflow.send_stop()
        self.state = 'awaiting_stopped'
        self._input_deadline = None
        self._save()

    def _poll_active(self):
        self.workflow.poll()
        if self.workflow.session.state in ('unknown', 'blocked', 'cancelled'):
            raise RecordingTaskError(self.workflow.session.reason or '录制任务已失去输入授权。')
        self._contract()
        self.preview.poll()
        if self.preview.state in ('complete', 'recovery_blocked', 'closing'):
            raise RecordingTaskError(self.preview.error or '预览会话意外结束，录制状态需检查。')
        if self.state in ('awaiting_not_recording', 'awaiting_start_foreground', 'awaiting_started'):
            self._ready_start()
            self._endpoint()
            if self.state == 'awaiting_start_foreground':
                if self.clock() >= self._input_deadline:
                    raise RecordingTaskError('开始录制前台等待超过 30 秒，未发送热键。')
                if self._foreground():
                    if self.automatic_input_ready is not None and self.automatic_input_ready('idle') is not True:
                        return
                    with self._bounded_reader('preview_reader'):
                        self.workflow.send_start()
                    self.state = 'awaiting_started'
                    self._input_deadline = None
                    self._save()
        elif self.state == 'awaiting_play_foreground':
            if self.preview.state == 'playing':
                self.state = 'recording'
                self._save()
            elif self.preview.state != 'awaiting_play_foreground':
                raise RecordingTaskError('播放步骤与已确认录制任务不一致。')
        elif self.state == 'recording':
            if self.preview.state == 'play_ended':
                proof = self._endpoint(at_end=True)
                self.workflow.mark_end(proof)
                self.state = 'awaiting_stop_foreground'
                self._input_deadline = proof.observed_at+self.STOP_FOREGROUND_SECONDS
                self._save()
                self._send_stop()
            elif self.preview.state != 'playing':
                raise RecordingTaskError('播放尚未证明精确终点，不能发送停止热键。')
        elif self.state == 'awaiting_stop_foreground':
            if self.preview.state != 'play_ended':
                raise RecordingTaskError('停录前的精确暂停终点已改变。')
            self._send_stop()
        elif self.state == 'awaiting_stopped':
            if self.preview.state != 'play_ended':
                raise RecordingTaskError('等待 NVIDIA 停止确认时游戏端点已改变。')
            self._endpoint(at_end=True)
        elif self.state in ('awaiting_end_console', 'checking_binding'):
            if self.workflow.session.state != 'stopped':
                raise RecordingTaskError('尚未确认 NVIDIA 停止，禁止最终控制台查询。')
            if self.preview.state == 'checking_binding':
                if self.state != 'checking_binding':
                    self.state = 'checking_binding'
                    self._save()
            elif self.preview.state == 'play_verified':
                playback = self.preview.playback
                if playback.state != 'verified' or playback.binding_may_exist:
                    raise RecordingTaskError('最终播放绑定尚未获得空绑定核验证据。')
                self._cleanup()
            elif self.preview.state != 'awaiting_end_console':
                raise RecordingTaskError('最终控制台收尾步骤与录制任务不一致。')

    def poll(self):
        if self.terminal or self.state == 'idle' or self._polling:
            return self.snapshot()
        self._polling = True
        try:
            if self.cleanup_requested:
                try:
                    self.preview.poll()
                except Exception as error:
                    self._add_error('收尾回读失败：' + str(error))
                self._settle_cleanup()
            else:
                try:
                    self._poll_active()
                except Exception as error:
                    self._cleanup(str(error))
        finally:
            self._polling = False
        return self.snapshot()

    def cancel(self):
        if not self.terminal:
            self._cleanup('用户取消录制任务；录制状态与游戏恢复分别核验。')
        return self.snapshot()
