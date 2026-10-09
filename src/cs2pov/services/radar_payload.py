"""Export only the selected POV's known information for a bounded session.

Hidden enemy positions never enter the runtime script. The original track is
still required to recompute eligibility on seek; each frame is independent.
"""
import json
import math
import re
from pathlib import Path

from cs2pov.services.radar import RadarTrack, identity
from cs2pov.storage.settings import DataError, local_path

KINDS = ('self', 'ally', 'enemy', 'last_known')
MAX_PAYLOAD_BYTES = 7 * 1024 * 1024


def decode_payload(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise DataError('雷达数据过大或格式无效。')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise DataError('雷达数据字段重复。')
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict) or set(data) != {'schema', 'player', 'map', 'overview', 'demo', 'stride', 'start', 'end', 'frames'}:
            raise DataError('雷达数据结构无效。')
        if type(data['schema']) is not int or data['schema'] != 1:
            raise DataError('雷达数据版本无效。')
        identity(data['player'])
        local_path(data['demo'])
        if not data['demo'] or Path(data['demo']).suffix.casefold() != '.dem':
            raise DataError('雷达数据缺少本次 Demo 身份。')
        if (not isinstance(data['map'], str) or not re.fullmatch(r'de_[a-z0-9_]{1,48}', data['map'])
                or not isinstance(data['overview'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['overview'])):
            raise DataError('雷达地图来源无效。')
        if (any(type(data[key]) is not int for key in ('stride', 'start', 'end'))
                or not 1 <= data['stride'] <= 1024 or not 0 <= data['start'] < data['end'] <= 2**31-1):
            raise DataError('雷达数据时间无效。')
        frames = data['frames']
        if not isinstance(frames, list) or not frames or len(frames) > 200000:
            raise DataError('雷达数据帧数无效。')
        previous = -1
        for frame in frames:
            if (not isinstance(frame, list) or len(frame) != 2 or type(frame[0]) is not int
                    or not previous < frame[0] <= data['end'] or not isinstance(frame[1], list) or len(frame[1]) > 64):
                raise DataError('雷达数据帧无效。')
            previous = frame[0]
            ids = set()
            own = 0
            for marker in frame[1]:
                if not isinstance(marker, list) or len(marker) != 6:
                    raise DataError('雷达标记结构无效。')
                player = identity(marker[0])
                if player in ids or type(marker[1]) is not int or not 0 <= marker[1] < len(KINDS):
                    raise DataError('雷达标记身份或类型无效。')
                ids.add(player)
                if marker[1] == 0:
                    own += 1
                    if player != data['player']:
                        raise DataError('雷达标记 POV 身份不一致。')
                if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 100000 for v in marker[2:]):
                    raise DataError('雷达标记坐标无效。')
            if frame[1] and own != 1:
                raise DataError('雷达数据缺少唯一 POV 标记。')
        if (not data['start'] - data['stride'] < frames[0][0] <= data['start']
                or data['end'] - frames[-1][0] >= data['stride']):
            raise DataError('雷达数据未覆盖片段。')
        return data
    except (ValueError, TypeError, RecursionError) as error:
        raise DataError('雷达数据验证失败。') from error


def payload(track: RadarTrack, overview, player, start, end, *, demo):
    player = identity(player)
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise DataError('雷达片段范围无效。')
    frames = []
    for tick in track.ticks:
        if tick < start - track.stride + 1 or tick > end:
            continue
        try:
            state = track.state(tick, player)
        except DataError:
            # A dead/absent POV cannot display radar. Explicit empty frames
            # invalidate older markers; they are not omitted from the timeline.
            frames.append([tick, []])
            continue
        markers = []
        for marker in state['markers']:
            x, y = overview.percent(marker['x'], marker['y'])
            markers.append([marker['id'], KINDS.index(marker['kind']), x, y,
                            marker['z'], marker['yaw']])
        frames.append([tick, markers])
    if not frames or frames[0][0] > start or end - frames[-1][0] >= track.stride:
        raise DataError('雷达轨迹未覆盖所选片段。')
    value = dict(schema=1, player=player, map=overview.map, overview=overview.sha256,demo=str(Path(demo).absolute()),
                 stride=track.stride, start=start, end=end, frames=frames)
    raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode('ascii')) > MAX_PAYLOAD_BYTES:
        raise DataError('雷达片段数据超过资源限制，请缩短片段。')
    decode_payload(raw)
    return raw
