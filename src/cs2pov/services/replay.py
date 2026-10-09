"""Fail-closed replay command boundary; real readback adapter is a phase 6 gate."""
from dataclasses import dataclass
from pathlib import Path
import math
import time
from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.demo_path import playdemo_command
from cs2pov.storage.settings import HudPreset, DataError


class ReplayError(DataError):
    pass


def position_tolerance(draft):
    value = draft.get('position_tolerance_ticks', 0)
    if type(value) is not int or not 0 <= value <= 1024:
        raise ReplayError('片段秒级定位范围无效。')
    return value


def position_matches(draft, actual, expected):
    return type(actual) is int and abs(actual-expected) <= position_tolerance(draft)


def quoted_name(analysis, player_id):
    target = next((p for p in analysis["players"] if p["id"] == player_id), None)
    if target is None:
        raise ReplayError("玩家稳定身份不在此 Demo 中。")
    name = target["name"]
    if not name.strip() or any(c in name for c in ('"', ';', '\\', '\r', '\n')) or any(ord(c) < 32 for c in name):
        raise ReplayError("此玩家昵称不适合安全控制台输入；需要已验证的身份选人接口。")
    needle = name.strip().casefold()
    for player in analysis["players"]:
        if player["id"] != player_id and any(needle in other.strip().casefold()
                                              for other in [player["name"], *player.get("aliases", [])]):
            raise ReplayError("昵称重复或存在匹配歧义，禁止按昵称自动选人。")
    return '"' + name + '"'


def preview_commands(analysis, draft):
    clip = draft["selection"]
    if clip["content_sha256"] != draft["fingerprint"]["sha256"]:
        raise ReplayError("片段与当前 Demo 内容标识不一致。")
    preset = HudPreset.decode(clip["hud"])
    name = quoted_name(analysis, clip["player_id"])
    if type(clip["start_tick"]) is not int or type(clip["server_end_tick"]) is not int or clip["start_tick"] < 0:
        raise ReplayError("片段 tick 无效。")
    # CS2 resumes while seeking. A separate pause input lets playback run
    # ahead before the next guarded input arrives; the engine accepts both
    # commands in one console submission and pauses at the requested tick.
    result = ["demo_pause", f"demo_gototick {clip['start_tick']}; demo_pause",
              "demo_timescale 1", "spec_autodirector 0", "spec_mode 2", f"spec_player {name}",
              *(["cl_radar_square_when_spectating 1", "cl_drawhud_force_radar 1"] if preset.show_radar
                else ["cl_drawhud_force_radar -1"]), "spec_show_xray 0", "demo_ui_mode 0",
              "cl_trueview_show_status 0",
              f"hud_scaling {preset.hud_scale:g}", f"viewmodel_fov {preset.viewmodel_fov:g}",
              f"viewmodel_offset_x {preset.viewmodel_x:g}", f"viewmodel_offset_y {preset.viewmodel_y:g}",
              f"viewmodel_offset_z {preset.viewmodel_z:g}"]
    if preset.crosshair:
        result.append("apply_crosshair_code " + preset.crosshair)
    return result


@dataclass(frozen=True)
class ReplayEvidence:
    process_identity: object
    local_demo: bool
    path: Path
    tick: int
    server_tick: int
    paused: bool
    player_id: str
    first_person: bool
    observed_at: float


class ReplayController:
    """Uses an injected console/readback adapter, with no success inferred from input."""
    def __init__(self, game, console, *, clock=time.monotonic):
        self.game, self.console, self.clock = game, console, clock
        self.state = "unverified"
        self._pending_end = None

    def guard(self):
        identity = self.game.verify()
        if "-insecure" not in (self.game.argv or []):
            raise ReplayError("没有受管理的 -insecure 启动证据。")
        from cs2pov.adapters.pipe_console import PipeConsole
        if type(self.console) is PipeConsole:
            # A verified command channel does not type into whichever window
            # has focus. Hardware recording/playback still checks real focus.
            if self.console.game is not self.game or self.console.command_target() != identity:
                raise ReplayError('自动命令通路不属于本次游戏。')
            return identity
        if self.console.foreground_pid() != identity.pid:
            self.state = "focus_lost"
            raise ReplayError("CS2 不在前台，暂停操作；请回到受管理游戏再核验。")
        return identity

    def send(self, command, *, deadline=None, input_guard=None):
        if deadline is not None and (type(deadline) not in (int,float)
                or not math.isfinite(deadline) or self.clock() >= deadline):
            raise ReplayError('回放输入缺少有效的原期限，禁止发送。')
        self.guard()
        self.state = "unverified"
        from cs2pov.adapters.pipe_console import PipeConsole
        if type(self.console) is PipeConsole:
            try:
                self.console.command(command,deadline=deadline,input_guard=input_guard)
            except Exception as error:
                label = '回放输入超时：' if deadline is not None and self.clock() >= deadline else '回放输入失败：'
                raise ReplayError(label+str(error)) from error
        else:
            if input_guard is not None: input_guard()
            if deadline is not None and self.clock() >= deadline:
                raise ReplayError('回放输入检查超过原期限，禁止发送。')
            self.console.command(command)

    def verify_result(self, draft, *, since, at_end=False, require_foreground=True):
        identity = self.guard() if require_foreground else self.game.verify()
        if '-insecure' not in (self.game.argv or []):
            raise ReplayError('没有受管理的 -insecure 启动证据。')
        proof = self.console.readback()
        clip = draft["selection"]
        tick = clip["end_tick"] if at_end else clip["start_tick"]
        server = clip["server_end_tick"] if at_end else clip["server_start_tick"]
        if (proof is None or proof.process_identity != identity or not proof.local_demo
                or proof.path != Path(draft["demo"]) or not proof.paused
                or not position_matches(draft, proof.tick, tick)
                or not position_matches(draft, proof.server_tick, server)
                or proof.player_id != clip["player_id"] or not proof.first_person
                or not since <= proof.observed_at <= self.clock() or self.clock() - proof.observed_at > 5):
            self.state = "unverified"
            raise ReplayError("没有新鲜、完整的 Demo/位置/玩家第一人称回读；禁止进入录制。")
        self.state = "end_verified" if at_end else "preview_verified"
        return proof

    def prepare(self, analysis, draft):
        commands = preview_commands(analysis, draft)
        if fingerprint(Path(draft["demo"])) != draft["fingerprint"]:
            raise ReplayError("原 Demo 已变化，先重新解析，不发送控制指令。")
        since = self.clock()
        for command in commands:
            self.send(command)
        return self.verify_result(draft, since=since)

    def arm_end(self, draft):
        # Caller must already have verified the same paused preview. NVIDIA
        # authorization/state is enforced separately by the phase 7 machine.
        if self.state != "preview_verified":
            raise ReplayError("预览未核验，不能设置播放结束。")
        self.verify_result(draft, since=self.clock() - 5)
        end = draft["selection"]["server_end_tick"]
        if type(end) is not int or end < 0:
            raise ReplayError("结束服务器 tick 无效。")
        # On the tested CS2 build, sending pauseatservertick before resume
        # does not stop at the requested tick. Keep the validated target and
        # submit resume followed by pauseatservertick atomically in resume().
        self._pending_end = end
        self.state = "end_armed"

    def resume(self):
        if self.state != "end_armed" or self._pending_end is None:
            raise ReplayError("没有经过核验的终点安排，不能自动播放。")
        self.send(f"demo_resume; demo_pauseatservertick {self._pending_end}")
        self._pending_end = None
        self.state = "playing_unverified"

    def load(self, draft):
        def unchanged():
            if fingerprint(Path(draft['demo'])) != draft['fingerprint']:
                raise ReplayError('原 Demo 在等待期间已变化，禁止加载旧片段。')
        unchanged()
        self.send(playdemo_command(Path(draft["demo"])),input_guard=unchanged)
