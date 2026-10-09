"""Deterministic demo radar data; observer information is never a POV reveal."""
from bisect import bisect_right
from dataclasses import dataclass
import math
from numbers import Integral

from cs2pov.storage.settings import DataError


FIELDS = ['X', 'Y', 'Z', 'yaw', 'team_num', 'CCSPlayerController.m_iTeamNum',
          'is_alive', 'spotted', 'approximate_spotted_by']


def identity(value):
    if isinstance(value, Integral) and not isinstance(value, bool):
        value = str(int(value))
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**64:
        raise DataError('雷达玩家稳定身份无效。')
    return value


@dataclass(frozen=True)
class Sample:
    id: str
    team: int
    alive: bool
    x: float | None
    y: float | None
    z: float | None
    yaw: float | None
    spotted_by: frozenset[str]


def normalize(records):
    frames = {}
    for row in records:
        tick = row.get('tick')
        if not isinstance(tick, Integral) or isinstance(tick, bool) or not 0 <= tick <= 2**31 - 1:
            raise DataError('雷达时间无效。')
        tick = int(tick)
        player = identity(row.get('steamid'))
        alive = row.get('is_alive')
        if type(alive) is not bool:
            raise DataError('雷达存活字段缺失。')
        teams = {int(v) for key in ('team_num', 'CCSPlayerController.m_iTeamNum')
                 if type(v := row.get(key)) in (int, float) and v in (2, 3)}
        if len(teams) != 1:
            raise DataError('雷达阵营缺失或冲突。')
        team = teams.pop()
        if alive:
            values = [row.get(key) for key in ('X', 'Y', 'Z', 'yaw')]
            if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 100000 for v in values):
                raise DataError('雷达位置或朝向无效。')
            if type(row.get('spotted')) is not bool or not isinstance(row.get('approximate_spotted_by'), list):
                raise DataError('雷达发现状态缺失。')
            spotted = frozenset(identity(v) for v in row['approximate_spotted_by'])
            if spotted and row['spotted'] is not True:
                raise DataError('雷达发现状态矛盾。')
        else:
            values, spotted = [None] * 4, frozenset()
        frame = frames.setdefault(tick, {})
        if player in frame:
            raise DataError('同一时间出现重复雷达玩家。')
        frame[player] = Sample(player, team, alive, *values, spotted)
    if not frames:
        raise DataError('没有雷达轨迹。')
    for frame in frames.values():
        for sample in frame.values():
            if not sample.spotted_by <= frame.keys():
                raise DataError('雷达发现者无法对应当时的稳定身份。')
    return dict(sorted(frames.items()))


class RadarTrack:
    def __init__(self, frames, *, stride=8, rate=64, memory_seconds=1.5):
        if type(stride) is not int or stride <= 0 or type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
            raise DataError('雷达采样比例无效。')
        if type(memory_seconds) not in (int, float) or not math.isfinite(memory_seconds) or not 0 <= memory_seconds <= 10:
            raise DataError('雷达最后已知位置期限无效。')
        self.frames, self.ticks = frames, sorted(frames)
        self.stride, self.rate, self.memory = stride, rate, int(memory_seconds * rate)

    def state(self, tick, player):
        if type(tick) is not int:
            raise DataError('雷达查询时间无效。')
        index = bisect_right(self.ticks, tick) - 1
        if index < 0 or tick - self.ticks[index] >= self.stride:
            raise DataError('当前时间没有新鲜的雷达样本。')
        now = self.frames[self.ticks[index]]
        owner = now.get(identity(player))
        if owner is None or not owner.alive:
            raise DataError('当前雷达 POV 玩家不可用。')
        visible = []
        for target in now.values():
            if target.team == owner.team:
                if target.alive:
                    visible.append(dict(id=target.id, kind='self' if target.id == owner.id else 'ally',
                                        x=target.x, y=target.y, z=target.z, yaw=target.yaw))
                continue
            # Reconstruct only past observations. Switching player/seeking cannot
            # reuse remembered intel from a prior render, and hidden movement is
            # not used even to interpolate last-known positions.
            # Scan a bounded history without copying the whole preceding demo
            # on every marker query (a full match can contain thousands of ticks).
            for history_index in range(index, -1, -1):
                at = self.ticks[history_index]
                if tick - at > self.memory:
                    break
                history = self.frames[at]
                previous_owner, enemy = history.get(owner.id), history.get(target.id)
                if (previous_owner is None or not previous_owner.alive or previous_owner.team != owner.team
                        or enemy is None or enemy.team != target.team):
                    break
                seen = enemy.alive and any(history[who].team == owner.team for who in enemy.spotted_by)
                if seen:
                    visible.append(dict(id=enemy.id, kind='enemy' if at == self.ticks[index] else 'last_known',
                                        x=enemy.x, y=enemy.y, z=enemy.z, yaw=enemy.yaw))
                    break
        return dict(tick=tick, player=owner.id, team=owner.team, markers=visible)
