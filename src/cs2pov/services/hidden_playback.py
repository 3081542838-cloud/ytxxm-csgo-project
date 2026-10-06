"""A single verified clip, played with the console hidden and no recorder input.

F8 must be unused. A fixed self-unbinding command is queried before input,
then submitted once through the owned pipe or legacy physical F8. Engine
state, not input delivery, proves the end.
"""
from copy import deepcopy
from pathlib import Path
import math
import time
import uuid

from cs2pov.adapters.binding_log import BindingLogReader, binding_query_command
from cs2pov.adapters.pipe_console import PipeConsole
from cs2pov.services.replay import ReplayError


class HiddenPlayback:
    def __init__(self, controller, session, draft, *, clock=time.monotonic,
                 reader_factory=BindingLogReader, checkpoint=lambda: None):
        self.controller, self.console = controller, controller.console
        self.session, self.draft = Path(session), deepcopy(draft)
        self.clock, self.reader_factory = clock, reader_factory
        self.checkpoint = checkpoint
        self.state = 'idle'
        self.reader = None
        self.request_nonce = None
        self.binding_may_exist = False
        self.triggered = False
        self.started_at = self.deadline = None
        self.moving_seen = False
        self.end_proof = None
        self.pipe_end_attempted = False
        self._pipe_input_deadline = self._pipe_identity = self._pipe_scope = None
        self._pipe_started_at = self._pipe_playback_deadline = None
        end = self.draft['selection']['server_end_tick']
        if type(end) is not int or not 1 <= end <= 2_147_483_647:
            raise ReplayError('播放结束服务器 tick 无效。')
        self.binding = ('unbind F8; demo_resume' if type(self.console) is PipeConsole
                        else f'unbind F8; demo_resume; demo_pauseatservertick {end}')
        duration = self.draft['selection'].get('duration')
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or duration <= 0):
            raise ReplayError('片段播放时长无效。')

    def _start_proof(self, *, require_foreground=True):
        return self.controller.verify_result(self.draft, since=self.clock()-5,
                                             require_foreground=require_foreground)

    def _play_start_proof(self):
        # A pipe may prepare a background game, but actual playback/capture
        # still needs the real owned game in front after each persistence step.
        identity = self.controller.game.verify()
        if self.console.foreground_pid() != identity.pid:
            raise ReplayError('播放前 CS2 已失去前台，未发送播放指令。')
        proof = self._start_proof()
        if (self.controller.game.verify() != identity
                or self.console.foreground_pid() != identity.pid):
            raise ReplayError('播放起点核验期间前台或进程身份已变化。')
        return proof

    def _input_ready(self, *, playback=False):
        check = getattr(self.console, 'input_ready', None)
        if callable(check) and check(playback=playback) is not True:
            return False
        if self.clock() >= self.deadline:
            raise ReplayError('隐藏控制台播放核验超时；输入检查不能延长原期限。')
        self._start_proof()
        if self.clock() >= self.deadline:
            raise ReplayError('隐藏控制台播放核验超时；起点回读不能延长原期限。')
        return True

    def _query(self, *, expected=None, prefix='', authorization_deadline=None):
        # Reader construction, replay checks and durable submission all spend
        # the same fixed budget. A new response nonce cannot renew permission
        # granted by the preceding empty-binding receipt.
        self.deadline = self.clock()+5
        input_deadline = (self.deadline if authorization_deadline is None
                          else min(self.deadline, authorization_deadline))
        try:
            identity = self.controller.guard()
            self.request_nonce = uuid.uuid4().hex
            self.reader = self.reader_factory(self.session, identity, self.request_nonce,
                                             expected=expected, clock=self.clock)
            if self.clock() >= input_deadline:
                raise ReplayError('绑定查询准备超过原输入期限，未发送指令。')
            command = prefix+binding_query_command('F8', self.request_nonce)
            if type(self.console) is PipeConsole:
                guard = (lambda: self.controller.verify_result(self.draft,since=self.started_at,at_end=True)) if self.state=='querying_unbound' else self._start_proof
                self.console.command(command,deadline=input_deadline,input_guard=guard)
            else:
                self.console.command(command)
        except Exception:
            self.console.console_open_confirmed = False
            self.state = 'failed'
            raise

    def begin(self):
        if self.state != 'idle':
            raise ReplayError('不能重复准备隐藏控制台播放。')
        self._start_proof()
        if type(self.console) is not PipeConsole and not self.console.console_open_confirmed:
            raise ReplayError('请先确认控制台打开且空输入框有光标。')
        self.state = 'querying_empty'
        self._query(prefix='log_flags Console -ConsoleOnly; ')

    def request_play(self):
        if self.state != 'ready' or self.triggered:
            raise ReplayError('画面或播放绑定未核验，不能重复播放。')
        self.state = 'awaiting_foreground'
        self.deadline = self.clock()+30

    def confirm_end_console(self):
        if self.state != 'ended':
            raise ReplayError('还没有核验片段终点，不能检查解绑。')
        self.controller.verify_result(self.draft, since=self.started_at, at_end=True)
        if type(self.console) is not PipeConsole and not self.console.console_open_confirmed:
            raise ReplayError('请先确认控制台打开且空输入框有光标。')
        self.state = 'querying_unbound'
        self._query()

    def _binding_proof(self, value):
        identity = self.controller.guard()
        proof = self.reader.readback()
        if proof is None:
            return None
        if (proof.process_identity != identity or proof.key != 'F8'
                or proof.request_nonce != self.request_nonce
                or not 0 <= self.clock()-proof.observed_at <= 5):
            raise ReplayError('绑定回读身份、请求或时间不匹配。')
        if proof.value != value:
            raise ReplayError('F8 已被占用或绑定结果不匹配，禁止覆盖或播放。')
        return proof

    def _snapshot(self):
        identity = self.controller.game.verify()
        if '-insecure' not in (self.controller.game.argv or []):
            raise ReplayError('播放进程缺少 -insecure。')
        proof = self.console.snapshot()
        if proof is None:
            return None
        clip = self.draft['selection']
        if (proof.process_identity != identity or not proof.local_demo
                or proof.path != Path(self.draft['demo']) or not proof.first_person
                or proof.player_id != clip['player_id']
                or not 0 <= self.clock()-proof.observed_at <= 5
                or not clip['start_tick']-self.draft.get('position_tolerance_ticks',0)
                    <= proof.tick <= clip['end_tick']+self.draft.get('position_tolerance_ticks',0)):
            raise ReplayError('播放中的 Demo、玩家、第一人称或位置证据失效。')
        return proof

    def _pipe_phase(self):
        if (self.state != 'playing' or self.triggered is not True
                or self.started_at != self._pipe_started_at
                or self.deadline != self._pipe_playback_deadline
                or self.draft != self._pipe_scope):
            raise ReplayError('自动播放的原阶段、范围或时间锚已变化，禁止输入。')

    def _pipe_start_proof(self):
        self._pipe_phase()
        if self.pipe_end_attempted or self.clock() >= self._pipe_input_deadline:
            raise ReplayError('自动播放开始权限已消费或超过原期限。')
        proof = self._play_start_proof()
        self._pipe_phase()
        if (proof.process_identity != self._pipe_identity
                or self.clock() >= self._pipe_input_deadline):
            raise ReplayError('自动播放开始回读身份变化或超过原期限。')
        return proof

    def _pipe_running_proof(self, identity, since, deadline, input_deadline, scope):
        """After persistence, recheck this one consumed endpoint scheduling step."""
        self._pipe_phase()
        if (since != self._pipe_started_at or deadline != self._pipe_playback_deadline
                or self.state != 'playing' or self.triggered is not True or self.pipe_end_attempted is not True
                or self.started_at != since or self.deadline != deadline
                or self._pipe_input_deadline != input_deadline or self.draft != scope
                or self._pipe_scope != scope or self._pipe_identity != identity
                or self.controller.game.verify() != identity
                or self.console.foreground_pid() != identity.pid):
            raise ReplayError('自动播放阶段、范围、身份或前台变化，禁止设置终点。')
        if self.clock() >= min(deadline, input_deadline):
            raise ReplayError('自动播放设置终点超过原期限；不重发。')
        proof = self._snapshot()
        self._pipe_phase()
        clip = scope['selection']
        if (proof is None or proof.paused is not False or type(proof.tick) is not int
                or not clip['start_tick'] <= proof.tick < clip['end_tick']
                or not since <= proof.observed_at <= self.clock()):
            raise ReplayError('没有本次未到终点的新鲜未暂停回读，禁止设置终点。')
        if (self.state != 'playing' or self.triggered is not True or self.pipe_end_attempted is not True
                or self.started_at != since or self.deadline != deadline
                or self._pipe_input_deadline != input_deadline or self.draft != scope
                or self._pipe_scope != scope or self._pipe_identity != identity
                or self.controller.game.verify() != identity
                or self.console.foreground_pid() != identity.pid):
            raise ReplayError('自动播放回读期间阶段、范围、身份或前台变化。')
        if self.clock() >= min(deadline, input_deadline):
            raise ReplayError('自动播放终点回读超过原期限；不重发。')
        return proof

    def poll(self):
        if self.state in ('idle', 'ended', 'verified', 'failed'):
            return self.end_proof
        try:
            if self.state != 'ready' and self.clock() >= self.deadline:
                raise ReplayError('隐藏控制台播放核验超时；不重发按键。')
            # A binding reply may take several polls. Keep draining the live
            # replay log so expired queued telemetry cannot erase its pause
            # anchor; readback never grants console or physical-key authority.
            if self.state in ('querying_empty', 'querying_installed'):
                self._start_proof(require_foreground=False)
            elif self.state == 'querying_unbound':
                self.controller.verify_result(self.draft, since=self.started_at,
                    at_end=True, require_foreground=False)
            if self.state == 'querying_empty':
                binding_proof = self._binding_proof('')
                if binding_proof is None: return None
                authorization_deadline = min(self.deadline, binding_proof.observed_at+5)
                self._start_proof()
                if not self._input_ready(): return None
                # Persisted by the caller at the next poll boundary. Even a
                # failed submission may have installed the runtime binding.
                self.binding_may_exist = True
                self.state = 'querying_installed'
                self.checkpoint()
                self._start_proof()
                if self.clock() >= self.deadline:
                    raise ReplayError('绑定安装持久化超过原期限，未发送指令。')
                self._query(expected=self.binding, prefix=f'bind "F8" "{self.binding}"; ',
                            authorization_deadline=authorization_deadline)
            elif self.state == 'querying_installed':
                if self._binding_proof(self.binding) is None: return None
                self._start_proof()
                if not self._input_ready(): return None
                self.console.hide_console()
                self.state = 'ready'
            elif self.state == 'ready':
                proof = self._snapshot()
                clip = self.draft['selection']
                from cs2pov.services.replay import position_matches
                if (proof is None or not proof.paused or not position_matches(self.draft,proof.tick,clip['start_tick'])):
                    raise ReplayError('准备后的暂停起点已改变，禁止播放。')
            elif self.state == 'awaiting_foreground':
                # Continue reading the paused start while waiting for the user
                # to switch windows; only the foreground branch may send F8.
                self._start_proof(require_foreground=False)
                identity = self.controller.game.verify()
                if self.console.foreground_pid() != identity.pid: return None
                self._start_proof()
                if self.console.console_open_confirmed:
                    raise ReplayError('控制台输入权限仍有效，不能发送播放按键。')
                if not self._input_ready(playback=True): return None
                self.started_at = self.clock()
                input_deadline = self.deadline
                clip = self.draft['selection']
                self.deadline = self.started_at+clip['duration']+10
                # Consume authorization before attempting input. Partial or
                # uncertain delivery can never retry this one-shot action.
                self.triggered = True
                self.state = 'playing'
                if type(self.console) is PipeConsole:
                    self._pipe_input_deadline = min(input_deadline, self.deadline)
                    self._pipe_identity, self._pipe_scope = identity, deepcopy(self.draft)
                    self._pipe_started_at, self._pipe_playback_deadline = self.started_at, self.deadline
                self.checkpoint()
                (self._pipe_start_proof if type(self.console) is PipeConsole else self._play_start_proof)()
                if self.clock() >= min(input_deadline,self.deadline):
                    raise ReplayError('播放持久化超过原期限，未发送或重发播放。')
                if type(self.console) is PipeConsole:
                    # Resume once; the next fresh unpaused poll authorizes a
                    # separately consumed pauseat command in an engine frame.
                    self.console.trigger_playback(clip['server_end_tick'],
                        deadline=self._pipe_input_deadline,input_guard=self._pipe_start_proof)
                else:
                    self.console.press_play_key()
                self.controller.state = 'playing_unverified'
            elif self.state == 'playing':
                if type(self.console) is PipeConsole and self.triggered:
                    self._pipe_phase()
                proof = self._snapshot()
                if proof is None or proof.observed_at < self.started_at: return None
                clip = self.draft['selection']
                if not proof.paused and proof.tick > clip['start_tick']:
                    self.moving_seen = True
                if type(self.console) is PipeConsole and not self.pipe_end_attempted:
                    if proof.paused:
                        if proof.tick == clip['end_tick']:
                            raise ReplayError('没有实际播放与独立终点调度证据，不能把终点当作完成。')
                        return None
                    # The initial resume authorization is never renewed by a
                    # new report or checkpoint. Endpoint dispatch retains it.
                    self.pipe_end_attempted = True
                    identity, since, deadline = self._pipe_identity, self._pipe_started_at, self._pipe_playback_deadline
                    input_deadline, scope = self._pipe_input_deadline, self._pipe_scope
                    self.checkpoint()
                    self._pipe_running_proof(identity, since, deadline, input_deadline, scope)
                    self.console.schedule_playback_end(scope['selection']['server_end_tick'],
                        deadline=min(deadline, input_deadline), input_guard=lambda:
                            self._pipe_running_proof(identity, since, deadline, input_deadline, scope))
                    return None
                from cs2pov.services.replay import position_matches
                if proof.paused and position_matches(self.draft,proof.tick,clip['end_tick']):
                    if not self.moving_seen:
                        raise ReplayError('没有实际播放中的证据，不能把终点当作完成。')
                    self.end_proof = self.controller.verify_result(
                        self.draft, since=self.started_at, at_end=True, require_foreground=False)
                    self.state = 'ended'
                    return self.end_proof
            elif self.state == 'querying_unbound':
                if self._binding_proof('') is None: return None
                self.controller.verify_result(self.draft, since=self.started_at, at_end=True)
                self.binding_may_exist = False
                self.console.console_open_confirmed = False
                self.state = 'verified'
                return self.end_proof
            return None
        except Exception:
            self.console.console_open_confirmed = False
            self.state = 'failed'
            raise
