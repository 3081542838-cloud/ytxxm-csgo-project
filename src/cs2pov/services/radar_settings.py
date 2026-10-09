"""Frozen, read-only snapshots of the radar settings actually reported by CS2.

These are validation bounds for incoming telemetry, not new game defaults or
claims about the engine's supported slider limits. We never emit set commands.
"""
from dataclasses import dataclass
import math

from cs2pov.storage.settings import DataError


SETTING_NAMES = (
    'cl_radar_scale', 'cl_radar_rotate', 'cl_radar_always_centered',
    'cl_hud_radar_scale', 'cl_radar_icon_scale_min',
    'cl_radar_square_always', 'cl_radar_square_when_spectating',
)
BOOLEAN_INDICES = frozenset((1, 2, 5, 6))


@dataclass(frozen=True)
class RadarSettings:
    values: tuple[float, ...]

    @classmethod
    def decode(cls, raw):
        if (not isinstance(raw, dict) or set(raw) != {'schema', 'values'}
                or type(raw['schema']) is not int or raw['schema'] != 1
                or not isinstance(raw['values'], list) or len(raw['values']) != len(SETTING_NAMES)):
            raise DataError('游戏雷达设置回读结构不完整。')
        values = raw['values']
        for index, value in enumerate(values):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise DataError('游戏雷达设置回读值无效。')
            if index in BOOLEAN_INDICES:
                if value not in (0, 1):
                    raise DataError('游戏雷达开关设置无效。')
            elif not 0 < value <= 10:
                raise DataError('游戏雷达比例设置无效。')
        return cls(tuple(float(v) for v in values))

    def snapshot(self):
        return {'schema': 1, 'values': list(self.values)}

    def as_settings(self):
        return dict(zip(SETTING_NAMES, self.values, strict=True))
