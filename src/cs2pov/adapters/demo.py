"""Read-only Source 2 framing and demoparser2 adapter. No game interaction."""
import hashlib
import math
from pathlib import Path
import struct
from cs2pov.storage.settings import DataError, local_path


class DemoError(DataError):
    pass


def varint(stream):
    value = 0
    for index in range(5):
        raw = stream.read(1)
        if not raw:
            raise DemoError("Demo 在帧头处截断。")
        value |= (raw[0] & 127) << (7 * index)
        if not raw[0] & 128:
            if value > 0xffffffff:
                break
            return value
    raise DemoError("Demo 帧头整数无效。")


def file_info(raw):
    import io
    stream = io.BytesIO(raw)
    values = {}
    while stream.tell() < len(raw):
        tag = varint(stream)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value = varint(stream)
        elif wire in (1, 5):
            count = 8 if wire == 1 else 4
            part = stream.read(count)
            if len(part) != count:
                raise DemoError("Demo 时间信息截断。")
            value = struct.unpack("<f", part)[0] if wire == 5 else None
        elif wire == 2:
            size = varint(stream)
            if size > len(raw) - stream.tell():
                raise DemoError("Demo 时间信息长度无效。")
            stream.seek(size, 1)
            value = None
        else:
            raise DemoError("Demo 时间信息编码不支持。")
        if (field, wire) in ((1, 5), (2, 0)):
            values[field] = value
    duration, ticks = values.get(1), values.get(2)
    if not isinstance(duration, float) or not math.isfinite(duration) or duration <= 0 or not ticks:
        raise DemoError("Demo 缺少有效总时长和 tick，不能猜测时间换算。")
    rate = ticks / duration
    if not 1 <= rate <= 1024:
        raise DemoError("Demo 时间比例异常。")
    return {"duration": duration, "playback_ticks": ticks, "tick_rate": rate}


def validate_frames(path: Path):
    """Streaming framing validation rejects truncated files even if parser tolerates EOF."""
    local_path(str(path))
    size = path.stat().st_size
    if path.suffix.casefold() != ".dem" or size < 19:
        raise DemoError("请选择完整 Source 2 .dem 文件。")
    stop, info, first = False, None, True
    with path.open("rb") as stream:
        if stream.read(8) != b"PBDEMS2\x00" or len(stream.read(8)) != 8:
            raise DemoError("文件头不属于 Source 2 Demo。")
        while stream.tell() < size:
            command, tick, length = varint(stream), varint(stream), varint(stream)
            kind = command & ~64
            if kind > 18 or first and kind != 1:
                raise DemoError("Demo 帧类型无效。")
            first = False
            if length > size - stream.tell():
                raise DemoError("Demo 数据帧截断，不能用于录制。")
            if kind == 0:
                stop = True
            if kind == 2:
                if command & 64 or length > 1_048_576:
                    raise DemoError("不支持此 Demo 的时间信息格式。")
                info = file_info(stream.read(length))
            else:
                stream.seek(length, 1)
    if not stop:
        raise DemoError("Demo 缺少正常结束标记，可能仍在下载或已截断。")
    if info is None:
        raise DemoError("Demo 缺少时间信息，不能猜测帧率。")
    return info


def fingerprint(path: Path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise DemoError("Demo 正在变化，请等待下载完成后重试。")
    return {"sha256": digest.hexdigest(), "bytes": after.st_size, "mtime_ns": after.st_mtime_ns}


def parse_native(path):
    from demoparser2 import DemoParser
    from cs2pov.services.clips import build_analysis
    info = validate_frames(path)
    parser = DemoParser(str(path))
    header = parser.parse_header()
    players = parser.parse_player_info().to_dict("records")
    events = {name: frame.to_dict("records") for name, frame in parser.parse_events(
        ["round_start", "round_end", "player_death"],
        player=["team_num", "CCSPlayerController.m_iTeamNum"],
        other=["game_time", "total_rounds_played", "is_warmup_period"])}
    ticks = parser.parse_ticks(["game_time", "is_alive", "is_warmup_period"])
    return build_analysis(header, players, events, ticks, info)
