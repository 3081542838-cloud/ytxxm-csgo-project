"""One durable NVIDIA recording attempt bound to a frozen local clip.

Successful keyboard insertion is never evidence of NVIDIA's state. Every
toggle is consumed and checkpointed before input; recovery never repeats it.
"""
from dataclasses import dataclass
from pathlib import Path
import math
import re
import time
import uuid

from cs2pov.adapters.disk import MIN_VIDEO_BYTES, check_directory
from cs2pov.adapters.nvidia import parse_hotkey, NvidiaNotSentError
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.storage.settings import DataError, local_path


class RecordingError(DataError):
    pass


@dataclass(frozen=True)
class RecordingScope:
    demo: str
    player_id: str
    start_tick: int
    end_tick: int
    server_start_tick: int
    server_end_tick: int
    content_sha256: str

    @classmethod
    def freeze(cls, draft):
        try:
            clip = draft['selection']
            demo = local_path(draft['demo'])
            ticks = tuple(clip[key] for key in
                          ('start_tick', 'end_tick', 'server_start_tick', 'server_end_tick'))
            player = clip['player_id']
            content = clip['content_sha256']
            if (not demo or any(type(value) is not int or not 0 <= value <= 2_147_483_647 for value in ticks)
                    or ticks[0] >= ticks[1] or ticks[2] >= ticks[3]
                    or not isinstance(player, str) or not player.isascii() or not player.isdigit()
                    or not isinstance(content, str) or re.fullmatch(r'[0-9a-f]{64}', content) is None
                    or draft['fingerprint']['sha256'] != content):
                raise ValueError('invalid clip')
            return cls(demo, player, *ticks, content)
        except (KeyError, TypeError, ValueError, DataError) as error:
            raise RecordingError('录制任务的 Demo、玩家、范围或内容标识无效。') from error


@dataclass(frozen=True)
class RecordingCheckpoint:
    state: str
    reason: str
    triggered: str
    started_at: float | None
    stopped_at: float | None
    task_id: str
    scope: RecordingScope
    expected_identity: ProcessIdentity
    confirmation_token: str | None
    deadline: float | None
    start_attempted_at: float | None
    stop_attempted_at: float | None
    start_not_sent: bool = False


class RecordingSession:
    WAITING = ('awaiting_not_recording', 'awaiting_started', 'awaiting_stopped')
    ACTIVE = (*WAITING, 'start_ready', 'start_pending', 'recording', 'stop_ready', 'stop_pending')

    def __init__(self, output_directory, hotkey, *, nvidia_path_confirmed,
                 draft, expected_identity, game, preview_reader, end_reader, checkpoint,
                 disk_checker=check_directory, hotkey_adapter=None,
                 clock=time.monotonic, confirmation_timeout=60, input_guard=None):
        if not str(output_directory):
            raise RecordingError('录制任务缺少视频保存目录。')
        local_path(str(output_directory))
        if (not isinstance(expected_identity, ProcessIdentity)
                or type(expected_identity.pid) is not int or expected_identity.pid <= 0
                or type(expected_identity.created) is not int or expected_identity.created <= 0
                or not isinstance(expected_identity.executable, str) or not expected_identity.executable):
            raise RecordingError('录制任务缺少完整的受管理游戏身份。')
        if (game is None or not callable(getattr(game, 'verify', None))
                or not callable(preview_reader) or not callable(end_reader) or not callable(checkpoint)):
            raise RecordingError('录制任务必须配置游戏核验、新鲜回读和持久化检查点。')
        if (type(confirmation_timeout) not in (int, float)
                or not math.isfinite(confirmation_timeout) or confirmation_timeout <= 0):
            raise RecordingError('录制确认超时设置无效。')
        if input_guard is not None and not callable(input_guard):
            raise RecordingError('NVIDIA 自动输入必须配置有效的只读状态守卫。')
        parse_hotkey(hotkey)
        self._scope = RecordingScope.freeze(draft)
        from cs2pov.services.replay import position_tolerance
        self.position_tolerance_ticks = position_tolerance(draft)
        self._expected_identity = expected_identity
        self.task_id = uuid.uuid4().hex
        self._output_directory = Path(output_directory)
        self._hotkey = hotkey
        self.nvidia_path_confirmed = nvidia_path_confirmed
        self.disk_checker = disk_checker
        self.hotkey_adapter = hotkey_adapter
        self._input_guard = input_guard
        self.game = game
        self.preview_reader, self.end_reader = preview_reader, end_reader
        self.clock, self.checkpoint = clock, checkpoint
        self.confirmation_timeout = confirmation_timeout
        self.state = 'idle'
        self.reason = ''
        self.triggered = 'none'
        self.started_at = self.stopped_at = None
        self.start_attempted_at = self.stop_attempted_at = None
        self.start_not_sent = False
        self.deadline = self.confirmation_token = None
        self.start_proof = self.end_proof = None

    @property
    def scope(self):
        return self._scope

    @property
    def expected_identity(self):
        return self._expected_identity

    @property
    def output_directory(self):
        return self._output_directory

    @property
    def hotkey(self):
        return self._hotkey

    @property
    def input_guard(self):
        return self._input_guard

    def _check_input_state(self, expected):
        if self._input_guard is None:
            return  # Existing explicitly confirmed manual flow.
        try:
            if self._input_guard(expected) is not True:
                raise RecordingError('只读观察未证明当前 NVIDIA 状态为 ' + expected + '。')
        except Exception as error:
            raise RecordingError('NVIDIA 输入前状态核验失败，不发送热键：' + str(error)) from error

    def _save(self):
        value = RecordingCheckpoint(self.state, self.reason, self.triggered,
            self.started_at, self.stopped_at, self.task_id, self.scope,
            self.expected_identity, self.confirmation_token, self.deadline,
            self.start_attempted_at, self.stop_attempted_at, self.start_not_sent)
        try:
            self.checkpoint(value)
        except Exception as error:
            # A callback can fail after writing. Keep the consumed attempt in
            # memory and never permit further input after a persistence error.
            self.state = 'unknown' if self.triggered != 'none' else 'blocked'
            self.confirmation_token = self.deadline = None
            self.reason = (self.reason + '\n' if self.reason else '') + '录制检查点保存失败：' + str(error)
            raise RecordingError(self.reason) from error

    def _fail_unknown(self, reason):
        self.reason, self.state = reason, 'unknown'
        self.confirmation_token = self.deadline = None
        self._save()

    def _guard_game(self):
        try:
            identity = self.game.verify()
            if identity != self.expected_identity or '-insecure' not in (self.game.argv or []):
                raise RecordingError('受管理游戏身份已变化或缺少 -insecure。')
            return identity
        except Exception as error:
            if self.triggered != 'none':
                self._fail_unknown('游戏状态无法核验；NVIDIA 状态未知：' + str(error))
            raise RecordingError('受管理游戏状态无法核验：' + str(error)) from error

    def _proof(self, proof, *, at_end=False):
        scope = self.scope
        now = self.clock()
        tick = scope.end_tick if at_end else scope.start_tick
        server = scope.server_end_tick if at_end else scope.server_start_tick
        observed = getattr(proof, 'observed_at', None)
        if (proof is None or getattr(proof, 'process_identity', None) != self.expected_identity
                or getattr(proof, 'local_demo', None) is not True
                or getattr(proof, 'paused', None) is not True
                or getattr(proof, 'first_person', None) is not True
                or getattr(proof, 'path', None) != Path(scope.demo)
                or getattr(proof, 'player_id', None) != scope.player_id
                or type(getattr(proof, 'tick', None)) is not int or abs(proof.tick-tick) > self.position_tolerance_ticks
                or type(getattr(proof, 'server_tick', None)) is not int or abs(proof.server_tick-server) > self.position_tolerance_ticks
                or type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= now-observed <= 5
                or (at_end and (self.started_at is None or observed <= self.started_at))):
            raise RecordingError('预览位置或视角已改变，或缺少新鲜、完整的精确端点证据。')
        return proof

    def _check_disk(self):
        try:
            return self.disk_checker(self.output_directory, MIN_VIDEO_BYTES)
        except Exception as error:
            raise RecordingError(str(error)) from error

    def _await(self, state):
        self.state = state
        self.confirmation_token = uuid.uuid4().hex
        self.deadline = self.clock() + self.confirmation_timeout

    def _expire(self):
        if self.state not in (*self.WAITING, 'start_ready') or self.deadline is None:
            return False
        if self.clock() < self.deadline:
            return False
        if self.triggered != 'none':
            self._fail_unknown('NVIDIA 录制状态确认超时；请手动检查并停止录制。')
        else:
            self.state, self.reason = 'blocked', 'NVIDIA 未录制授权已超时；未发送热键，请重新准备。'
            self.confirmation_token = self.deadline = None
            self._save()
        return True

    def _confirm(self, state, step_token):
        if (self.state != state or not isinstance(step_token, str)
                or step_token != self.confirmation_token):
            raise RecordingError('NVIDIA 确认不属于当前任务步骤，或已经重复/过期。')
        if self._expire():
            raise RecordingError(self.reason)

    def arm(self, preview_proof):
        if self.state != 'idle':
            raise RecordingError('录制任务已经开始，不能重复准备。')
        if self.nvidia_path_confirmed is not True:
            raise RecordingError('尚未确认 NVIDIA 实际保存目录，不能开始录制。')
        self._guard_game()
        self.start_proof = self._proof(preview_proof)
        self._check_disk()
        self._await('awaiting_not_recording')
        self._save()

    def confirm_not_recording(self, *, step_token):
        self._confirm('awaiting_not_recording', step_token)
        self._guard_game()
        self._check_disk()
        self.state, self.confirmation_token = 'start_ready', None
        self.deadline = self.clock() + self.confirmation_timeout
        self._save()

    def send_start(self):
        if self.state != 'start_ready':
            raise RecordingError('必须先确认 NVIDIA 当前未录制。')
        if self._expire():
            raise RecordingError(self.reason)
        self._check_disk()
        self._guard_game()
        try:
            self.start_proof = self._proof(self.preview_reader())
        except RecordingError:
            raise
        except Exception as error:
            raise RecordingError('起点新鲜回读失败：' + str(error)) from error
        if self.hotkey_adapter is None:
            raise RecordingError('未配置 NVIDIA 热键适配器，不能发送。')
        self._check_input_state('idle')
        self.state, self.triggered = 'start_pending', 'start'
        self.confirmation_token = self.deadline = None
        self.start_attempted_at = self.clock()
        self._save()  # Durable consumed authorization precedes any input.
        try:
            # A slow checkpoint cannot authorize input from an aged proof.
            self.start_proof = self._proof(self.preview_reader())
            self._check_input_state('idle')
            self._proof(self.start_proof)  # State-provider latency cannot age the replay authorization.
            self.hotkey_adapter.send_once(self.hotkey, self.game,
                                          expected_identity=self.expected_identity)
        except NvidiaNotSentError as error:
            # All batches inserted zero events. Persist a consumed, cancelled
            # attempt without claiming uncertain NVIDIA state or restoring input.
            self.state, self.triggered = 'cancelled', 'none'
            self.start_not_sent = True
            self.reason = '本次录制未启动：' + str(error)
            self.confirmation_token = self.deadline = None
            self._save()
            raise RecordingError(self.reason) from error
        except Exception as error:
            self._fail_unknown('开始热键结果未知：' + str(error))
            raise RecordingError(self.reason) from error
        self._await('awaiting_started')
        self._save()

    def confirm_started(self, *, step_token):
        self._confirm('awaiting_started', step_token)
        self._guard_game()
        self.state, self.started_at = 'recording', self.clock()
        self.confirmation_token = self.deadline = None
        self._save()

    def mark_end(self, end_proof):
        if self.state != 'recording':
            raise RecordingError('只有已确认录制中才能到达片段终点。')
        self._guard_game()
        self.end_proof = self._proof(end_proof, at_end=True)
        self.state = 'stop_ready'
        self._save()

    def send_stop(self):
        if self.state != 'stop_ready':
            raise RecordingError('必须先到达已核验片段终点。')
        self._guard_game()
        try:
            self.end_proof = self._proof(self.end_reader(), at_end=True)
        except RecordingError:
            raise
        except Exception as error:
            raise RecordingError('终点新鲜回读失败：' + str(error)) from error
        if self.hotkey_adapter is None:
            raise RecordingError('未配置 NVIDIA 热键适配器，不能发送。')
        self._check_input_state('recording')
        self.state, self.triggered = 'stop_pending', 'stop'
        self.confirmation_token = self.deadline = None
        self.stop_attempted_at = self.clock()
        self._save()
        try:
            self.end_proof = self._proof(self.end_reader(), at_end=True)
            self._check_input_state('recording')
            self._proof(self.end_proof, at_end=True)
            self.hotkey_adapter.send_once(self.hotkey, self.game,
                                          expected_identity=self.expected_identity)
        except Exception as error:
            self._fail_unknown('停止热键结果未知：' + str(error))
            raise RecordingError(self.reason) from error
        self._await('awaiting_stopped')
        self._save()

    def confirm_stopped(self, *, step_token):
        self._confirm('awaiting_stopped', step_token)
        self.state, self.stopped_at = 'stopped', self.clock()
        self.confirmation_token = self.deadline = None
        self._save()

    def poll(self):
        if self._expire():
            return self.state
        if self.state in self.ACTIVE:
            self._guard_game()
        return self.state

    def cancel(self):
        if self.state in ('idle', 'awaiting_not_recording', 'start_ready', 'blocked'):
            self.state, self.reason = 'cancelled', '用户取消，未发送 NVIDIA 热键。'
            self.confirmation_token = self.deadline = None
            self._save()
        elif self.state in ('start_pending', 'awaiting_started', 'recording', 'stop_ready', 'stop_pending', 'awaiting_stopped'):
            self._fail_unknown('取消时 NVIDIA 状态未知，请手动检查并停止录制。')
        elif self.state == 'unknown':
            raise RecordingError('NVIDIA 状态未知，先由使用者确认后再处理。')
        else:
            raise RecordingError('当前录制任务不能取消。')

    def restore_checkpoint(self, value):
        """Restore status for cleanup; never restore input authorization."""
        if (self.state != 'idle' or not isinstance(value, RecordingCheckpoint)
                or value.scope != self.scope or value.expected_identity != self.expected_identity
                or value.state not in ('idle', *self.ACTIVE, 'blocked', 'unknown', 'cancelled', 'stopped')
                or value.triggered not in ('none', 'start', 'stop')
                or type(value.start_not_sent) is not bool
                or not isinstance(value.task_id, str) or re.fullmatch(r'[0-9a-f]{32}', value.task_id) is None):
            raise RecordingError('录制恢复检查点与任务不一致或结构无效。')
        self.task_id = value.task_id
        self.triggered = value.triggered
        self.started_at, self.stopped_at = value.started_at, value.stopped_at
        self.start_attempted_at, self.stop_attempted_at = value.start_attempted_at, value.stop_attempted_at
        self.start_not_sent = value.start_not_sent
        self.confirmation_token = self.deadline = None
        times = (value.start_attempted_at, value.started_at, value.stop_attempted_at, value.stopped_at)
        stopped_is_complete = (value.triggered == 'stop'
            and all(type(item) in (int, float) and math.isfinite(item) and item >= 0 for item in times)
            and tuple(sorted(times)) == times)
        if value.state == 'stopped' and stopped_is_complete:
            self.state, self.reason = 'stopped', value.reason
        elif (value.triggered != 'none'
                or value.state in ('unknown', 'start_pending', 'awaiting_started', 'recording',
                                   'stop_ready', 'stop_pending', 'awaiting_stopped', 'stopped')):
            self.state, self.reason = 'unknown', '应用上次退出时录制状态未确认；请手动检查并停止 NVIDIA。'
        else:
            self.state, self.reason = 'cancelled', '遗留录制授权已失效，未恢复任何热键。'
        self._save()
