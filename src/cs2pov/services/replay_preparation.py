"""Poll-driven precision preparation, using measured engine pause behavior.

The caller polls from a timer. No sleeping, retrying input, or inferred success.
The short lead-in is played before recording; exact start proof is mandatory.
"""
from bisect import bisect_left
from copy import deepcopy
import math
from pathlib import Path

from cs2pov.adapters.demo import fingerprint
from cs2pov.services.replay import ReplayError, preview_commands, position_matches


class ReplayPreparation:
    def __init__(self, controller, analysis, draft, *, timeout=20, deferred_seek_pause=False):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ReplayError("回放准备超时设置无效。")
        if type(deferred_seek_pause) is not bool:
            raise ReplayError('延后跳转暂停设置必须是布尔值。')
        self.controller = controller
        self.analysis, self.draft = deepcopy(analysis), deepcopy(draft)
        self.timeout = timeout
        self.deferred_seek_pause = deferred_seek_pause
        self.state = "idle"
        self.deadline = self.since = None
        self.preroll_tick = None
        self.commands = None
        self._resuming_scope = self._resuming_identity = None
        self._resuming_since = self._resuming_deadline = None
        self._seek_identity = self._seek_reader = self._seek_nonce = None
        self._seek_scope = self._seek_since = self._seek_deadline = self._seek_requested = None

    def _lead_in(self):
        clip = self.draft["selection"]
        rate = self.analysis.get("tick_rate")
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 1024:
            raise ReplayError("缺少有效回放速率，不能准备准确起点。")
        pairs = self.analysis.get("timeline", [])
        if (not pairs or any(not isinstance(p, (list, tuple)) or len(p) != 2
                             or any(type(v) is not int or v < 0 for v in p) for p in pairs)
                or any(a[0] >= b[0] or a[1] >= b[1] for a, b in zip(pairs, pairs[1:]))):
            raise ReplayError("缺少单调的回放与服务器时间映射。")
        ticks = [p[0] for p in pairs]
        for name in ("start", "end"):
            value, server = clip[f"{name}_tick"], clip[f"server_{name}_tick"]
            if type(value) is not int or type(server) is not int:
                raise ReplayError("片段边界 tick 无效。")
            i = bisect_left(ticks, value)
            if i == len(pairs) or tuple(pairs[i]) != (value, server):
                raise ReplayError("片段边界不匹配解析的时间映射。")
        if clip["start_tick"] >= clip["end_tick"]:
            raise ReplayError("片段范围倒置或为空。")
        start_index = bisect_left(ticks, clip["start_tick"])
        from cs2pov.adapters.pipe_console import PipeConsole
        # A background game may process pipe inputs across slow render frames.
        # Give that path three seconds before the cut, or five while an
        # asynchronous seek settles. The final dual-tick cut is unchanged.
        lead_seconds = (5 if self.deferred_seek_pause else 3) if type(self.controller.console) is PipeConsole else 1
        before = bisect_left(ticks, clip["start_tick"] - math.ceil(rate * lead_seconds))
        before = min(before, start_index - 1)
        if before < 0:
            raise ReplayError("起点之前没有可验证的预播放范围，请选择稍后的起点。")
        lead = pairs[before]
        if not 0 < clip["server_start_tick"] - lead[1] <= math.ceil(rate * 5):
            raise ReplayError("起点之前时间映射中断，不能自动预播放。")
        return lead[0]

    def _stage(self, name):
        self.state = name
        self.since = self.controller.clock()
        self.deadline = self.since + self.timeout

    def begin(self, *, loading_since=None, loading_deadline=None):
        if self.state != "idle":
            raise ReplayError("此回放准备已经开始，不能重复发送跳转。")
        try:
            from cs2pov.adapters.pipe_console import PipeConsole
            automatic = type(self.controller.console) is PipeConsole
            if self.deferred_seek_pause and not automatic:
                raise ReplayError('延后跳转暂停只支持本次游戏的自动命令通路。')
            identity = self.controller.guard() if automatic else None
            paused_tick = [None]
            if automatic:
                now = self.controller.clock()
                if (any(type(value) not in (int, float) or not math.isfinite(value)
                        for value in (loading_since, loading_deadline))
                        or not 0 <= loading_since <= now < loading_deadline
                        or loading_deadline > loading_since+120):
                    raise ReplayError('自动准备缺少本次载入的原时间范围，禁止输入。')
            def loaded_input(expected_state):
                if self.state != expected_state:
                    raise ReplayError('载入准备阶段已改变，禁止再次输入。')
                self._check_deadline(loading_deadline)
                if self.controller.guard() != identity:
                    raise ReplayError('载入准备的游戏身份变化，禁止输入。')
                proof = self.controller.console.snapshot()
                now = self.controller.clock()
                self._check_deadline(loading_deadline)
                if (proof is None or proof.process_identity != identity or not proof.local_demo
                        or proof.path != Path(self.draft['demo'])
                        or type(proof.tick) is not int or proof.tick < 0
                        or type(proof.paused) is not bool
                        or not loading_since <= proof.observed_at <= now
                        or now-proof.observed_at > 5):
                    raise ReplayError('载入后的 Demo 回读已失效，禁止暂停或跳转。')
                if paused_tick[0] is not None and (not proof.paused or proof.tick != paused_tick[0]):
                    raise ReplayError('载入后已核验的暂停位置变化，禁止跳转。')
                if proof.paused:
                    paused_tick[0] = proof.tick
                if self.controller.guard() != identity:
                    raise ReplayError('载入回读期间游戏身份变化，禁止输入。')
                self._check_deadline(loading_deadline)
            commands = preview_commands(self.analysis, self.draft)
            self.preroll_tick = self._lead_in()
            if fingerprint(Path(self.draft["demo"])) != self.draft["fingerprint"]:
                raise ReplayError("原 Demo 已变化，先重新解析，不发送控制指令。")
            # Seek/pause commands are replaced by the independently checked lead-in.
            self.commands = commands[2:]
            self.controller.send("demo_pause", deadline=loading_deadline if automatic else None,
                input_guard=(lambda: loaded_input('idle')) if automatic else None)
            if automatic: self._check_deadline(loading_deadline)
            stage = 'settling_seek' if self.deferred_seek_pause else 'seeking'
            self._stage(stage)
            if self.deferred_seek_pause:
                self._seek_identity, self._seek_reader = identity, self.controller.console.reader
                self._seek_nonce = self._reader_nonce()
                self._seek_requested = self.preroll_tick
                self._seek_scope = self._seek_selection()
                self._seek_since, self._seek_deadline = self.since, self.deadline
            command = f"demo_gototick {self.preroll_tick}"
            if not self.deferred_seek_pause:
                command += '; demo_pause'
            self.controller.send(command,
                deadline=loading_deadline if automatic else None,
                input_guard=(lambda: loaded_input(stage)) if automatic else None)
            if automatic: self._check_deadline(loading_deadline)
        except Exception:
            self._fail()
            raise

    def _fail(self):
        self.state = "failed"
        self.controller.state = "unverified"

    def _check_deadline(self, deadline):
        if self.controller.clock() >= deadline:
            raise ReplayError('回放准备超时；没有再次发送指令，请检查游戏。')

    def _base(self, proof, identity, *, since=None):
        now = self.controller.clock()
        since = self.since if since is None else since
        return (proof is not None and proof.process_identity == identity and proof.local_demo
                and proof.path == Path(self.draft["demo"]) and proof.paused
                and since <= proof.observed_at <= now and now - proof.observed_at <= 5)

    def _phase_input(self, identity, since, deadline, *, expected_state, preroll=False):
        """Re-read scope after every ledger, using the phase that granted input."""
        if self.state != expected_state:
            raise ReplayError('回放准备阶段已改变，禁止再次输入。')
        self._check_deadline(deadline)
        if self.controller.guard() != identity:
            raise ReplayError('回放准备的游戏身份变化，禁止输入。')
        proof = self.controller.console.readback()
        checked = proof
        if preroll and proof is None:
            read_snapshot = getattr(self.controller.console, 'snapshot', None)
            if callable(read_snapshot):
                checked = read_snapshot()
                proof = self.controller.console.readback()
                if proof is not None: checked = proof
        self._check_deadline(deadline)
        clip = self.draft['selection']
        if not self._base(checked, identity, since=since):
            raise ReplayError('没有本阶段新鲜的同 Demo 暂停回读，禁止后续输入。')
        if preroll:
            if (checked.tick != self.preroll_tick
                    or proof is not None and proof.server_tick >= clip['server_start_tick']):
                raise ReplayError('预播放暂停位置已变化或不在起点之前，禁止输入。')
        elif (not position_matches(self.draft, checked.tick, clip['start_tick'])
              or not position_matches(self.draft, checked.server_tick, clip['server_start_tick'])):
            raise ReplayError('精确起点回读已变化，禁止选人和 HUD 输入。')
        # Selection may still show another player until spec_player executes.
        # Only the final ready gate requires target identity and first person.
        if self.controller.guard() != identity:
            raise ReplayError('准备回读期间游戏身份变化，禁止输入。')
        self._check_deadline(deadline)

    def _input_ready(self, proof, identity):
        input_ready = getattr(self.controller.console, "input_ready", None)
        if not callable(input_ready):
            return True
        if not input_ready():
            return False
        # A readiness query must not authorize input after the original
        # deadline or after the evidence used by this poll became stale.
        if self.controller.clock() >= self.deadline:
            raise ReplayError("回放准备超时；没有再次发送指令，请检查游戏。")
        return self._base(proof, identity)

    def _resume_scope(self):
        clip = self.draft['selection']
        return (self.draft['demo'], self.preroll_tick, clip['start_tick'],
                clip['server_start_tick'], clip['player_id'])

    def _running(self, proof, identity, since):
        now = self.controller.clock()
        return (proof is not None and proof.process_identity == identity and proof.local_demo
                and proof.path == Path(self.draft['demo']) and proof.paused is False
                and type(proof.tick) is int
                and self.preroll_tick <= proof.tick < self.draft['selection']['start_tick']
                and since <= proof.observed_at <= now and now-proof.observed_at <= 5)

    def _reader_nonce(self):
        return getattr(getattr(self.controller.console.reader, 'decoder', None), 'nonce', None)

    def _seek_selection(self):
        clip = self.draft['selection']
        return (self.draft['demo'], self.preroll_tick, clip['start_tick'], clip['end_tick'],
                clip['server_start_tick'], clip['server_end_tick'], clip['player_id'],
                self.analysis.get('tick_rate'))

    def _seek_anchor(self, expected_state):
        if (self.state != expected_state or self.since != self._seek_since
                or self.deadline != self._seek_deadline
                or self._seek_selection() != self._seek_scope
                or self.controller.console.reader is not self._seek_reader
                or self._reader_nonce() != self._seek_nonce):
            raise ReplayError('跳转暂停的原阶段、会话或范围已变化，禁止输入。')
        self._check_deadline(self._seek_deadline)

    def _settled_seek(self, proof, identity):
        now = self.controller.clock()
        return (proof is not None and proof.process_identity == identity
                and proof.local_demo and proof.path == Path(self.draft['demo'])
                and type(proof.paused) is bool and type(proof.tick) is int
                # The first requested frame precedes "Skipping finished" on
                # this engine. Observe actual advancement before pausing.
                and self._seek_requested < proof.tick <= self._seek_requested+math.ceil(self.analysis['tick_rate'])
                and proof.tick < self.draft['selection']['start_tick']
                and self._seek_since <= proof.observed_at <= now
                and now-proof.observed_at <= 5)

    def _seek_pause_input(self, identity):
        self._seek_anchor('pausing_seek')
        if self.controller.guard() != identity or identity != self._seek_identity:
            raise ReplayError('跳转暂停的游戏身份变化，禁止输入。')
        proof = self.controller.console.snapshot()
        self._seek_anchor('pausing_seek')
        if not self._settled_seek(proof, identity):
            raise ReplayError('跳转后没有新鲜同 Demo 的起点前推进回读，禁止暂停。')
        if self.controller.guard() != identity:
            raise ReplayError('跳转回读期间游戏身份变化，禁止输入。')
        self._seek_anchor('pausing_seek')
        if not self._settled_seek(proof, identity):
            raise ReplayError('跳转回读在身份检查期间过期，禁止暂停。')

    def _resume_anchor(self, expected_state):
        if (self.state != expected_state or self.since != self._resuming_since
                or self.deadline != self._resuming_deadline
                or self._resume_scope() != self._resuming_scope):
            raise ReplayError('恢复播放的原阶段、时间范围或片段已变化，禁止后续输入。')
        self._check_deadline(self._resuming_deadline)

    def _resume_dispatch(self, identity, since, deadline):
        self._resume_anchor('resuming')
        self._phase_input(identity, since, deadline, expected_state='resuming', preroll=True)
        self._resume_anchor('resuming')

    def _resume_input(self, identity, since, deadline, scope):
        """A separate pause command requires post-resume telemetry after its ledger."""
        self._resume_anchor('starting')
        if (identity != self._resuming_identity or since != self._resuming_since
                or deadline != self._resuming_deadline
                or self.state != 'starting' or self.since != since or self.deadline != deadline
                or self._resume_scope() != scope):
            raise ReplayError('预播放阶段或范围已变化，禁止设置起点暂停。')
        self._check_deadline(deadline)
        if self.controller.guard() != identity:
            raise ReplayError('预播放游戏身份变化，禁止设置起点暂停。')
        proof = self.controller.console.snapshot()
        self._check_deadline(deadline)
        if not self._running(proof, identity, since):
            raise ReplayError('没有新鲜同 Demo 的起点前未暂停回读，禁止设置起点暂停。')
        if (self.controller.guard() != identity or self._resume_scope() != scope
                or self.state != 'starting' or self.since != since or self.deadline != deadline):
            raise ReplayError('预播放回读期间阶段、范围或身份变化。')
        self._resume_anchor('starting')
        self._check_deadline(deadline)

    def poll(self):
        if self.state == "ready":
            # A ready label is never a substitute for currently valid evidence.
            try:
                return self.controller.verify_result(self.draft, since=self.since)
            except Exception:
                self._fail()
                raise
        if self.state not in ('settling_seek', 'pausing_seek', "seeking", "resuming", "starting", "selecting"):
            raise ReplayError("回放准备未开始或已停止。")
        try:
            entry_state = self.state
            if entry_state == 'resuming': self._resume_anchor('resuming')
            if entry_state in ('settling_seek', 'pausing_seek'): self._seek_anchor(entry_state)
            identity = self.controller.guard()
            if self.controller.clock() >= self.deadline:
                raise ReplayError("回放准备超时；没有再次发送指令，请检查游戏。")
            proof = self.controller.console.readback()
            if self.state != entry_state:
                raise ReplayError('回放回读期间准备阶段已变化，禁止输入。')
            if entry_state == 'resuming': self._resume_anchor('resuming')
            if entry_state in ('settling_seek', 'pausing_seek'):
                self._seek_anchor(entry_state)
                if identity != self._seek_identity:
                    raise ReplayError('跳转后游戏身份变化，禁止暂停或播放。')
            clip = self.draft["selection"]
            if self.state == 'settling_seek':
                settled = self.controller.console.snapshot()
                self._seek_anchor('settling_seek')
                if not self._settled_seek(settled, identity):
                    return None
                ready = self.controller.console.input_ready()
                self._seek_anchor('settling_seek')
                if not ready or not self._settled_seek(settled, identity):
                    return None
                # Consume before the independent pipe ledger. This deadline
                # also covers the wait for real server pause evidence.
                self.state = 'pausing_seek'
                self.controller.send('demo_pause', deadline=self._seek_deadline,
                    input_guard=lambda: self._seek_pause_input(identity))
                self._seek_anchor('pausing_seek')
                return None
            if self.state == 'pausing_seek':
                from cs2pov.services.replay import ReplayEvidence
                if (type(proof) is not ReplayEvidence or not self._base(proof, identity)
                        or not self._settled_seek(proof, identity)):
                    return None
                if type(proof.server_tick) is not int or proof.server_tick >= clip['server_start_tick']:
                    raise ReplayError('预播放暂停位置不在起点之前，禁止播放。')
                # Only engine-backed pause evidence may supply this anchor;
                # the snapshot which authorized pause has no server clock.
                self.preroll_tick = proof.tick
                self.state = 'seeking'
                return None
            if self.state == "seeking":
                preroll = proof
                if preroll is None:
                    # A seek while paused may emit no new server pause line.
                    # The snapshot can authorize only the checked lead-in;
                    # it never supplies a server clock or exact start proof.
                    read_snapshot = getattr(self.controller.console, "snapshot", None)
                    if callable(read_snapshot):
                        preroll = read_snapshot()
                        # snapshot() may consume more log lines and establish
                        # a real pause proof; its server check has priority.
                        proof = self.controller.console.readback()
                        if proof is not None:
                            preroll = proof
                if not self._base(preroll, identity) or preroll.tick != self.preroll_tick:
                    return None
                if proof is not None and proof.server_tick >= clip["server_start_tick"]:
                    raise ReplayError("预播放位置不在起点之前，禁止播放。")
                if not self._input_ready(preroll, identity):
                    return None
                since, deadline = self.since, self.deadline
                self.controller.send("demo_timescale 1", deadline=deadline,input_guard=lambda:
                    self._phase_input(identity,since,deadline,expected_state='seeking',preroll=True))
                self._check_deadline(deadline)
                from cs2pov.adapters.pipe_console import PipeConsole
                automatic = type(self.controller.console) is PipeConsole
                self._stage('resuming' if automatic else 'starting')
                if automatic:
                    self._resuming_scope, self._resuming_identity = self._resume_scope(), identity
                    self._resuming_since, self._resuming_deadline = self.since, self.deadline
                self.controller.send('demo_resume' if automatic else
                    f"demo_resume; demo_pauseatservertick {clip['server_start_tick']}",
                    deadline=deadline,
                    input_guard=(lambda: self._resume_dispatch(identity,since,deadline))
                        if automatic else lambda: self._phase_input(identity,since,deadline,
                            expected_state='starting',preroll=True))
                if automatic: self._resume_anchor('resuming')
                self._check_deadline(deadline)
                return None
            if self.state == 'resuming':
                # The tested pipe runs resume and pauseat in different engine
                # frames. A fresh unpaused snapshot is the handoff, never a
                # fabricated server clock or an excuse to resend resume.
                if self._resume_scope() != self._resuming_scope or identity != self._resuming_identity:
                    raise ReplayError('恢复播放后的范围或游戏身份变化，禁止设置起点暂停。')
                # The tested engine reinitializes resume over multiple frames.
                # Spend part of the existing preroll, without blocking Qt or
                # renewing its deadline, before scheduling the start pause.
                if self.draft.get('position_tolerance_ticks',0) and self.controller.clock()-self.since < 1.25:
                    return None
                running = self.controller.console.snapshot()
                self._resume_anchor('resuming')
                if not self._running(running, identity, self.since):
                    return None
                if not self.controller.console.input_ready():
                    return None
                self._resume_anchor('resuming')
                since, deadline, scope = self._resuming_since, self._resuming_deadline, self._resuming_scope
                self.state = 'starting'  # Consume before the independent pipe ledger.
                self.controller.send(f"demo_pauseatservertick {clip['server_start_tick']}",
                    deadline=deadline, input_guard=lambda:
                        self._resume_input(identity, since, deadline, scope))
                self._check_deadline(deadline)
                return None
            if not self._base(proof, identity):
                return None
            if (not position_matches(self.draft, proof.tick, clip['start_tick'])
                    or not position_matches(self.draft, proof.server_tick, clip['server_start_tick'])):
                return None
            if self.state == "starting":
                # Selection must happen after the asynchronous seek has finished.
                if not self._input_ready(proof, identity):
                    return None
                since, deadline = self.since, self.deadline
                self._stage("selecting")
                for command in self.commands:
                    self.controller.send(command, deadline=deadline,input_guard=lambda:
                        self._phase_input(identity,since,deadline,expected_state='selecting'))
                    self._check_deadline(deadline)
                return None
            if proof.player_id != clip["player_id"] or not proof.first_person:
                return None
            result = self.controller.verify_result(self.draft, since=self.since)
            self.state = "ready"
            return result
        except Exception:
            self._fail()
            raise

    def cancel(self):
        # Do not issue a blind pause after focus loss or uncertain input.
        self.state = "cancelled"
        self.controller.state = "unverified"
