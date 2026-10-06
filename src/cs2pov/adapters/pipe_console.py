"""Session command channel, enabled only by fresh startup and execution receipts.

It never types into a window or claims that console visibility was observed.
Every command attempt is durably consumed before a single pipe write.
"""
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid

from cs2pov.adapters.binding_log import BindingLogReader, binding_query_command
from cs2pov.adapters.command_pipe import CommandPipes
from cs2pov.adapters.native_console import Win32Keyboard
from cs2pov.storage.settings import DataError
from cs2pov.storage.transaction import atomic_write, no_redirection


class PipeConsole:
    def __init__(self, game, pipes, session, *, clock=time.monotonic,
                 keyboard=None, reader=None, binding_reader_factory=BindingLogReader,
                 persist=atomic_write):
        if type(pipes) is not CommandPipes:
            raise DataError('自动命令通路必须来自本次受限管道。')
        self.game, self.pipes, self.session, self.clock = game, pipes, Path(session), clock
        self.keyboard = keyboard if keyboard is not None else Win32Keyboard()
        self.reader, self.binding_reader_factory, self.persist = reader, binding_reader_factory, persist
        self.state = 'new'
        self.identity = self.request_nonce = self.binding_reader = self.deadline = None
        self.sequence = 0
        self.ledger = self.session/'pipe-input-state.json'
        no_redirection(self.ledger)
        if self.ledger.exists():
            raise DataError('已有管道输入记录，不能恢复或重发旧会话。')

    @property
    def console_open_confirmed(self):
        return False  # Never grants keyboard console authority.

    @console_open_confirmed.setter
    def console_open_confirmed(self, value):
        if value is not False:
            raise DataError('管道执行证据不能转换为人工控制台确认。')

    def confirm_open_and_empty(self):
        raise DataError('自动管道模式不接受人工控制台确认。')

    def _target(self):
        identity = self.game.verify()
        if (self.identity is not None and identity != self.identity
                or '-insecure' not in (self.game.argv or [])
                or self.pipes.state != 'connected'
                or self.pipes.game is not self.game or self.pipes.identity != identity):
            self.state = 'failed'
            raise DataError('自动通路的受管理游戏或连接身份变化。')
        return identity

    def command_target(self):
        if self.state != 'ready':
            raise DataError('尚无本次自动通路的真实执行回执。')
        return self._target()

    def _send(self, command, *, deadline=None, input_guard=None):
        dispatch_deadline = self.clock()+1
        if deadline is not None:
            dispatch_deadline = min(dispatch_deadline,deadline)
        identity = self._target()
        self.sequence += 1
        # Avoid persisting the whole nickname/path/command; the upper layer owns
        # exact scope and replay proof. This ledger only proves consumed input.
        record = dict(sequence=self.sequence, consumed=True, attempt=uuid.uuid4().hex,
                      identity=asdict(identity), channel='owned_local_command_pipe',
                      observed_at=self.clock())
        try:
            no_redirection(self.ledger)
            self.persist(self.ledger, json.dumps(record).encode('utf-8'))
            if self.clock() >= dispatch_deadline:
                raise DataError('管道输入持久化超过原期限，未发送或重发。')
            if input_guard is not None:
                input_guard()
            if self.clock() >= dispatch_deadline:
                raise DataError('管道输入回读超过原期限，未发送或重发。')
            self._target()
            self.pipes.send_command(command, self.game,
                timeout=min(1,dispatch_deadline-self.clock()),deadline=dispatch_deadline)
            if self.clock() >= dispatch_deadline:
                raise DataError('管道输入超过原期限，结果未知，禁止重发。')
            if self._target() != identity:
                raise DataError('管道写入后的游戏身份变化，禁止重发。')
        except Exception:
            self.state = 'failed'
            raise

    def begin(self, startup_proof):
        from cs2pov.adapters.game_startup import GameStartupEvidence
        if self.state != 'new':
            raise DataError('自动执行回执不能重复请求。')
        identity = self._target()
        if (type(startup_proof) is not GameStartupEvidence
                or startup_proof.process_identity != identity
                or not 0 <= self.clock()-startup_proof.observed_at <= 5):
            self.state = 'failed'
            raise DataError('没有本次游戏的新鲜初始化证据。')
        self.identity = identity
        self.request_nonce = uuid.uuid4().hex
        self.deadline = self.clock()+5
        self.state = 'querying'
        try:
            self.binding_reader = self.binding_reader_factory(self.session, identity,
                                                             self.request_nonce, clock=self.clock)
            if (self.clock() >= self.deadline
                    or not 0 <= self.clock()-startup_proof.observed_at <= 5):
                raise DataError('初始化证据在查询准备期间已过期，未发送命令。')
            self._send('log_flags Console -ConsoleOnly; '+binding_query_command('F8', self.request_nonce),
                       deadline=min(self.deadline,startup_proof.observed_at+5))
        except Exception:
            self.state = 'failed'
            raise

    def poll_ready(self):
        if self.state == 'ready':
            self.command_target()
            return True
        if self.state != 'querying':
            raise DataError('自动执行回执通路未开始或已失效。')
        try:
            identity = self._target()
            if self.clock() >= self.deadline:
                raise DataError('等待真实执行回执超时，不重发查询。')
            proof = self.binding_reader.readback()
            if self.clock() >= self.deadline:
                raise DataError('执行回执检查不能延长原期限。')
            if proof is None:
                if self.binding_reader.decoder.state == 'invalid':
                    raise DataError('初始化查询无效或 F8 已被占用。')
                return False
            if (proof.process_identity != identity or proof.key != 'F8' or proof.value != ''
                    or proof.request_nonce != self.request_nonce
                    or not 0 <= self.clock()-proof.observed_at <= 5):
                raise DataError('初始化执行回执的身份、请求或内容不符。')
            self.state = 'ready'
            return True
        except Exception:
            self.state = 'failed'
            raise

    def command(self, value, *, deadline=None, input_guard=None):
        self.command_target()
        if input_guard is not None and not callable(input_guard):
            raise DataError('管道输入缺少有效的状态回读接口。')
        self._send(value,deadline=deadline,input_guard=input_guard)

    def input_ready(self, *, playback=False):
        if type(playback) is not bool:
            return False
        try:
            self.command_target()
            return True  # No keyboard input, modifiers and input focus do not apply.
        except Exception:
            return False

    def foreground_pid(self):
        return self.keyboard.foreground_pid()

    def hide_console(self):
        self.command('hideconsole')  # Delivery never asserts observed invisibility.

    def trigger_playback(self, server_end_tick, *, deadline=None, input_guard=None):
        if type(server_end_tick) is not int or not 1 <= server_end_tick <= 2_147_483_647:
            raise DataError('自动播放终点无效。')
        # pauseat must be scheduled in a later engine frame, after a fresh
        # unpaused replay observation. The caller owns that separate phase.
        self.command('unbind F8; demo_resume',
                     deadline=deadline, input_guard=input_guard)

    def schedule_playback_end(self, server_end_tick, *, deadline, input_guard):
        if type(server_end_tick) is not int or not 1 <= server_end_tick <= 2_147_483_647:
            raise DataError('自动播放终点无效。')
        if not callable(input_guard):
            raise DataError('自动播放终点缺少未暂停状态回读。')
        self.command(f'demo_pauseatservertick {server_end_tick}',
                     deadline=deadline, input_guard=input_guard)

    def readback(self):
        return None if self.reader is None else self.reader.readback()

    def snapshot(self):
        return None if self.reader is None else self.reader.snapshot()

    def close(self):
        self.state = 'closed'
        self.pipes.close()
