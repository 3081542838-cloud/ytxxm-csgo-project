"""One local preview session: deploy, owned game, verified preparation, restore.

No NVIDIA operations. The UI supplies the explicit console confirmation and
polls without blocking. Every failure retains a recoverable file transaction.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import time
import uuid

from cs2pov.adapters.native_console import NativeConsole
from cs2pov.adapters.command_pipe import CommandPipes
from cs2pov.adapters.pipe_console import PipeConsole
from cs2pov.adapters.nvidia import ensure_playback_hotkey_compatible
from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.owned_process import OwnedGame
from cs2pov.adapters.replay_log import ReplayLogReader
from cs2pov.services.hud_trial import check_trial, prepare_trial, trial_transaction
from cs2pov.services.replay import ReplayController, ReplayError, preview_commands
from cs2pov.services.replay_preparation import ReplayPreparation
from cs2pov.services.hidden_playback import HiddenPlayback
from cs2pov.services.telemetry_resource import build_session_resource
from cs2pov.storage.settings import DataError, HudPreset
from cs2pov.storage.transaction import atomic_write, no_redirection


class PreviewSession:
    def __init__(self, directory, settings, analysis, draft, base, *, clock=time.monotonic,
                 game=None, console_factory=NativeConsole, reader_factory=ReplayLogReader,
                 checker=check_trial, preparer=prepare_trial, builder=build_session_resource,
                 playback_factory=HiddenPlayback, automatic=False,
                 pipes_factory=CommandPipes, startup_factory=None,
                 pipe_console_factory=PipeConsole, second_precision=False):
        if type(automatic) is not bool:
            raise DataError('自动准备选项无效。')
        self.session = Path(directory)/('preview-'+uuid.uuid4().hex)
        self.settings, self.analysis, self.draft = settings,deepcopy(analysis),deepcopy(draft)
        self.second_precision = second_precision
        if second_precision:
            import math
            self.draft['position_tolerance_ticks'] = math.ceil(self.analysis['tick_rate'])
        self.base, self.clock = Path(base),clock
        self.game = game if game is not None else OwnedGame()
        self.console_factory,self.reader_factory = console_factory,reader_factory
        self.checker,self.preparer,self.builder = checker,preparer,builder
        self.playback_factory, self.playback = playback_factory, None
        self.automatic = automatic
        self.pipes_factory, self.startup_factory = pipes_factory, startup_factory
        self.pipe_console_factory = pipe_console_factory
        self.pipes = self.startup_reader = None
        self.pipe_cleanup_pending = False
        self.state = 'idle'
        self.error = ''
        self.transaction = self.console = self.controller = self.preparation = None
        self.resource = None
        self.deadline = self.since = None
        self.console_confirmed = False
        self.console_step_token = None
        self.restoration = None
        self.created = False
        self.check = None
        # FileTransaction owns creation of sessions/*. Building a resource in
        # that directory beforehand would look like an orphaned transaction.
        self.resource_directory = self.session.parent.parent/'preview-resources'/self.session.name
        self.staging_session = self.resource_directory/'transactions'/self.session.name

    def _persist(self):
        atomic_write(self.session/'preview-state.json',json.dumps(dict(
            state=self.state,error=self.error,restoration=self.restoration,
            console_step_token=self.console_step_token,
            playback=None if self.playback is None else dict(state=self.playback.state,
                triggered=self.playback.triggered,binding_may_exist=self.playback.binding_may_exist)),
            ensure_ascii=False).encode('utf-8'))

    def _new_preparation(self):
        if self.automatic and self.check is not None and self.check.patch_version == '1.41.8.9':
            return ReplayPreparation(self.controller, self.analysis, self.draft,
                                     deferred_seek_pause=True)
        return ReplayPreparation(self.controller, self.analysis, self.draft)

    def start(self):
        if self.state!='idle': raise DataError('预览会话已开始，不能重复启动。')
        try:
            if not str(self.session.absolute()).isascii():
                raise DataError('CS2 无法可靠写入中文路径下的控制日志。请把整个便携文件夹放到纯英文路径，例如 D:\\XiamiPOV；未修改游戏。')
            ensure_playback_hotkey_compatible(self.settings.hotkey)
            # Validate selection and lead-in before creating any game modification.
            preview_commands(self.analysis,self.draft)
            ReplayPreparation(ReplayController(self.game,None,clock=self.clock),self.analysis,self.draft)._lead_in()
            if fingerprint(Path(self.draft['demo']))!=self.draft['fingerprint']:
                raise ReplayError('原 Demo 已变化，先重新解析，不修改游戏。')
            self.resource_directory.mkdir(parents=True,exist_ok=False)
            resources = self.resource_directory/'resources'
            resources.mkdir()
            hud = HudPreset.decode(self.draft['selection']['hud'])
            self.resource = (self.builder(self.base,resources/'session-pov.vpk',native_radar=True)
                             if hud.show_radar else self.builder(self.base,resources/'session-pov.vpk'))
            check = self.checker(Path(self.settings.installation),Path(self.draft['demo']),
                Path(self.settings.video_directory),self.resource.path,Path(self.settings.cfg),telemetry=self.resource)
            self.check = check
            self.transaction = self.preparer(check,self.staging_session,self.resource.path)
            if (self.transaction.data.get('prepared') is not True
                    or not (self.staging_session/'journal.json').is_file()
                    or not (self.staging_session/'trial-context.json').is_file()):
                raise DataError('事务备份或恢复上下文未完整持久化，禁止部署。')
            # Move only this small prepared backup directory, on the same local
            # volume. The active scan never observes half-created metadata.
            self.session.parent.mkdir(parents=True,exist_ok=True)
            no_redirection(self.session)
            no_redirection(self.staging_session)
            if self.session.exists(): raise DataError('会话目录已经存在，禁止覆盖。')
            self.staging_session.rename(self.session)
            self.transaction.session = self.session.absolute()
            self.created = True
            if self.automatic:
                self.pipes = self.pipes_factory(clock=self.clock).create()
            self.transaction.deploy()
            if self.automatic:
                self.game.launch(check.installation,self.session,telemetry=True,command_pipes=self.pipes)
                self.console = self.pipe_console_factory(self.game,self.pipes,self.session,clock=self.clock)
            else:
                self.game.launch(check.installation,self.session,telemetry=True)
                self.console = self.console_factory(self.game)
            self.controller = ReplayController(self.game,self.console,clock=self.clock)
            if self.automatic:
                self.state = 'connecting_game'
                self.deadline = self.clock()+120
            elif (self.session/'engine.log').is_file():
                self._await_console('awaiting_console')
            else:
                self.state = 'awaiting_game'
                self.deadline = self.clock()+120
            self._persist()
        except Exception as error:
            self.error = str(error)
            if self.transaction is None and self.created:
                try:
                    self.transaction = trial_transaction(self.check,self.session)
                    self.transaction.load()
                except Exception as recovery_error:
                    self.state = 'recovery_blocked'
                    self.error += '\n事务加载失败，保留备份：'+str(recovery_error)
                    self._persist()
                    raise
            self.stop()
            raise

    def _await_console(self, state):
        # A confirmation belongs to one waiting step of this session. Map
        # loading and later playback preparation cannot reuse its authority.
        self.state = state
        self.console_confirmed = False
        self.console_step_token = uuid.uuid4().hex
        self.deadline = self.clock()+120

    def _input_ready(self):
        check = getattr(self.console, 'input_ready', None)
        if callable(check) and check() is not True:
            return False
        if self.clock() >= self.deadline:
            raise ReplayError('预览准备超时；输入检查不能延长当前步骤期限。')
        return True

    def confirm_console(self, *, step_token=None):
        if self.automatic:
            raise DataError('自动管道准备无需人工控制台确认。')
        if self.state not in ('awaiting_console','awaiting_demo_console','awaiting_play_console','awaiting_end_console') or self.console_confirmed:
            raise DataError('当前步骤不能重复确认控制台。')
        if step_token is not None and step_token != self.console_step_token:
            raise DataError('控制台确认属于旧步骤或其他会话，不能授权当前操作。')
        # Check the existing deadline here, before replacing it. A delayed
        # click must not depend on the UI timer having polled first.
        if self.deadline is None or not self.clock() < self.deadline:
            self.error = '控制台确认超时，请重新准备；未发送控制指令。'
            self.console_confirmed = False
            self.console_step_token = None
            self.stop()
            raise ReplayError(self.error)
        # The UI remains foreground during this click. Input waits for the game
        # to become foreground; confirmation alone never counts as readback.
        self.console_confirmed = True
        self.deadline = self.clock()+30

    def prepare_playback(self):
        if self.state != 'ready': raise ReplayError('预览起点未核验，不能准备播放。')
        try:
            self.controller.verify_result(self.draft,since=self.clock()-5,require_foreground=False)
        except Exception as error:
            self.state = 'preview_changed'
            self.controller.state = 'unverified'
            self._persist()
            raise
        if self.automatic:
            try:
                self.playback = self.playback_factory(self.controller,self.session,self.draft,
                    clock=self.clock,checkpoint=self._persist)
                self.playback.begin()
                self.state = 'arming_playback'
            except Exception as error:
                self.error = '自动播放准备失败：'+str(error)
                self.stop()
                raise
        else:
            self._await_console('awaiting_play_console')
        self._persist()

    def play_clip(self):
        if self.state != 'play_ready': raise ReplayError('请先收起控制台并核验播放准备。')
        try:
            self.playback.request_play()
        except Exception as error:
            # If the temporary binding may already exist, close the owned game
            # and restore rather than leaving a hidden F8 action behind.
            if self.playback.binding_may_exist:
                self.error = '播放授权失败：' + str(error)
                self.stop()
            raise
        self.state = 'awaiting_play_foreground'
        self._persist()

    def check_playback_binding(self):
        if self.state != 'play_ended': raise ReplayError('片段尚未核验停止。')
        if self.automatic:
            try:
                self.playback.confirm_end_console()
                self.state = 'checking_binding'
            except Exception as error:
                self.error = '自动播放收尾查询失败：'+str(error)
                self.stop()
                raise
        else:
            self._await_console('awaiting_end_console')
        self._persist()

    def poll(self):
        if self.state=='closing': return self._finish_close()
        if self.state in ('idle','complete','recovery_blocked'): return None
        try:
            identity = self.game.verify()
            if self.state in ('arming_playback','play_ready','awaiting_play_foreground','playing','checking_binding'):
                proof = self.playback.poll()
                states = {'querying_empty':'arming_playback','querying_installed':'arming_playback',
                    'ready':'play_ready','awaiting_foreground':'awaiting_play_foreground',
                    'playing':'playing','ended':'play_ended','querying_unbound':'checking_binding',
                    'verified':'play_verified'}
                next_state = states[self.playback.state]
                if self.state != next_state:
                    self.state = next_state
                    self._persist()
                return proof
            if self.state in ('play_ended','play_verified'):
                return self.controller.verify_result(self.draft,since=self.playback.started_at,
                    at_end=True,require_foreground=False)
            if self.state in ('ready','preview_changed'):
                # Reading local state does not require foreground. Watching the
                # app is normal; loss of ownership or exit still needs cleanup.
                proof = self.console.readback()
                clip = self.draft['selection']
                now = self.clock()
                from cs2pov.services.replay import position_matches
                valid = (proof is not None and proof.process_identity==identity and proof.local_demo
                    and proof.path==Path(self.draft['demo']) and proof.paused
                    and position_matches(self.draft,proof.tick,clip['start_tick'])
                    and position_matches(self.draft,proof.server_tick,clip['server_start_tick'])
                    and proof.player_id==clip['player_id'] and proof.first_person
                    and 0<=now-proof.observed_at<=5)
                if not valid and self.state=='ready':
                    self.state='preview_changed'
                    self.controller.state='unverified'
                    self._persist()
                return proof if self.state=='ready' and valid else None
            if self.clock()>=self.deadline: raise ReplayError('预览准备超时，请检查游戏和控制台。')
            if self.state == 'connecting_game':
                if not self.pipes.poll_connection(self.game): return None
                if not (self.session/'engine.log').is_file(): return None
                if self.startup_reader is None:
                    from cs2pov.adapters.game_startup import StartupLogReader
                    factory = self.startup_factory or StartupLogReader
                    self.startup_reader = factory(self.session,self.game,
                        patch_version=self.check.patch_version,clock=self.clock)
                startup_proof = self.startup_reader.poll()
                if self.clock() >= self.deadline:
                    raise ReplayError('初始化检查不能延长原准备期限。')
                if startup_proof is None: return None
                self.console.begin(startup_proof)
                self.state = 'checking_command_pipe'
                self.deadline = self.console.deadline
                self._persist()
                return None
            if self.state == 'checking_command_pipe':
                if not self.console.poll_ready(): return None
                self.console.reader = self.reader_factory(self.session,identity,self.resource.nonce,clock=self.clock)
                if self.second_precision:
                    self.console.reader.decoder.allow_tick_boundary = True
                self.since = self.clock()
                self.controller.load(self.draft)
                self.state = 'loading'
                self.deadline = self.clock()+60
                self._persist()
                return None
            if self.state in ('awaiting_play_console','awaiting_end_console'):
                # Keep consuming live state while the user is in the app. A
                # later batch containing expired telemetry must never be used
                # to reconstruct an old pause anchor. This is read-only and
                # does not grant console or keyboard authority.
                self.controller.verify_result(self.draft,
                    since=self.clock()-5 if self.state=='awaiting_play_console' else self.playback.started_at,
                    at_end=self.state=='awaiting_end_console',require_foreground=False)
                if not self.console_confirmed or self.console.foreground_pid()!=identity.pid: return None
                if not self._input_ready(): return None
                self.console.confirm_open_and_empty()
                if self.state == 'awaiting_play_console':
                    self.playback = self.playback_factory(self.controller,self.session,self.draft,
                        clock=self.clock,checkpoint=self._persist)
                    self.playback.begin()
                    self.state = 'arming_playback'
                else:
                    self.playback.confirm_end_console()
                    self.state = 'checking_binding'
                self.console_confirmed = False
                self.console_step_token = None
                self._persist()
                return None
            if self.state=='awaiting_game':
                # The region launcher and engine initialization precede a usable
                # console. Give the explicit input step its own bounded window.
                if (self.session/'engine.log').is_file():
                    self._await_console('awaiting_console')
                    self._persist()
                return None
            if self.state=='awaiting_console':
                if not self.console_confirmed or self.console.foreground_pid()!=identity.pid: return None
                if not (self.session/'engine.log').is_file(): return None
                if not self._input_ready(): return None
                self.console.reader = self.reader_factory(self.session,identity,self.resource.nonce,clock=self.clock)
                self.console.confirm_open_and_empty()
                self.since = self.clock()
                self.controller.load(self.draft)
                # Loading a map can close or defocus its console input.
                # A pre-load confirmation cannot authorize later keyboard input.
                self.console.console_open_confirmed = False
                self.console_confirmed = False
                self.console_step_token = None
                self.state = 'loading'
                self.deadline = self.clock()+60
                self._persist()
                return None
            if self.state=='loading':
                snapshot = self.console.snapshot()
                if (snapshot is None or not snapshot.local_demo or snapshot.path!=Path(self.draft['demo'])
                        or snapshot.process_identity!=identity or snapshot.observed_at<self.since
                        or not 0 <= self.clock()-snapshot.observed_at <= 5): return None
                if self.automatic:
                    self.preparation = self._new_preparation()
                    self.preparation.begin(loading_since=self.since,loading_deadline=self.deadline)
                    self.state = 'preparing'
                    self.deadline = self.clock()+60
                else:
                    self._await_console('awaiting_demo_console')
                self._persist()
                return None
            if self.state=='awaiting_demo_console':
                if not self.console_confirmed or self.console.foreground_pid()!=identity.pid: return None
                snapshot = self.console.snapshot()
                if (snapshot is None or not snapshot.local_demo or snapshot.path!=Path(self.draft['demo'])
                        or snapshot.process_identity!=identity or not 0<=self.clock()-snapshot.observed_at<=5):
                    return None
                if not self._input_ready(): return None
                if not 0 <= self.clock()-snapshot.observed_at <= 5: return None
                self.console.confirm_open_and_empty()
                self.preparation = self._new_preparation()
                self.preparation.begin()
                self.state = 'preparing'
                self.console_confirmed = False
                self.console_step_token = None
                self.deadline = self.clock()+60
                self._persist()
                return None
            self.controller.guard()
            proof = self.preparation.poll()
            if proof is not None:
                if self.automatic: self.console.hide_console()
                self.state = 'ready'
                self.console.console_open_confirmed = False
                self._persist()
            return proof
        except Exception as error:
            self.error = str(error)
            self.stop()
            raise

    def stop(self):
        if self.state in ('complete','closing'): return
        if self.state == 'recovery_blocked':
            if not self.pipe_cleanup_pending: return
            self.state = 'closing'
            self.deadline = self.clock()+15
            return self._finish_close()
        self.console_confirmed = False
        self.console_step_token = None
        if self.preparation is not None: self.preparation.cancel()
        self.state = 'closing'
        self.deadline = self.clock()+15
        if self.pipes is not None:
            try:
                if self.console is not None: self.console.close()
                else: self.pipes.close()
            except Exception as error:
                self.pipe_cleanup_pending = True
                self.error += '\n命令通路清理失败：'+str(error)
        if self.game.process is not None and self.game.process.poll() is None:
            try:
                self.game.force_close()  # Only owned birth/path, on the same handle.
            except Exception as error:
                # The held process object can confirm an exit racing the close.
                # This never follows an unknown/reused PID to another process.
                if self.game.process.poll() is None:
                    self.error += '\n游戏关闭未确认：'+str(error)
                    self.state = 'recovery_blocked'
        if self.created: self._persist()
        if self.state=='closing': self._finish_close()

    def _finish_close(self):
        if self.game.process is not None and self.game.process.poll() is None:
            if self.clock()>=self.deadline:
                self.state = 'recovery_blocked'
                self.error += '\n游戏仍在运行，保留备份，请退出后恢复。'
                self._persist()
            return None
        try:
            if self.transaction is not None and self.created:
                self.restoration = self.transaction.restore()
                if not self.transaction.data['complete']:
                    raise DataError('文件恢复没有全部通过，保留备份。')
            if self.pipes is not None:
                self.pipe_cleanup_pending = True
                self.pipes.close()
                if self.pipes.handles or self.pipes.cleanup_errors:
                    raise DataError('命令通路清理仍未完成，保持任务阻断。')
                self.pipe_cleanup_pending = False
            self.state = 'complete'
        except Exception as error:
            self.error += '\n'+str(error)
            self.state = 'recovery_blocked'
        if self.created: self._persist()
        return self.restoration
