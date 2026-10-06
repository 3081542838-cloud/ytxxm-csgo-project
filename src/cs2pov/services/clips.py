"""Stable player identity and explicit, validated Demo ranges."""
from bisect import bisect_left, bisect_right
import math
from cs2pov.adapters.demo import DemoError


def build_analysis(header, players, events, ticks, info):
    import numpy as np
    if not header.get("map_name") or ticks.empty:
        raise DemoError("Demo 缺少地图或有效 tick。")
    if ticks.groupby("tick")["game_time"].nunique().max() > 1:
        raise DemoError("同一回放 tick 的服务器时间不一致。")
    timeline = ticks[["tick", "game_time"]].drop_duplicates("tick").sort_values("tick")
    if timeline.isna().any().any():
        raise DemoError("Demo 时间字段缺失。")
    demo_ticks = timeline["tick"].to_numpy(dtype=np.int64)
    seconds = timeline["game_time"].to_numpy(dtype=float)
    # Pinned demoparser2 game_time is net_tick / 64; this is its API encoding,
    # not an assumption that all demos have the same playback tick rate/offset.
    server_ticks = np.rint(seconds * 64).astype(np.int64)
    if (not np.isfinite(seconds).all() or np.any(np.diff(demo_ticks) <= 0)
            or np.any(np.diff(server_ticks) <= 0) or np.max(np.abs(seconds * 64 - server_ticks)) > .02):
        raise DemoError("Demo 时间映射不单调或无法精确还原；不能自动选片。")
    if demo_ticks[-1] > info["playback_ticks"]:
        raise DemoError("实际 tick 超出文件声明长度。")
    identities = {}
    for item in players:
        value = item.get("steamid")
        if value is None or not str(value).isdigit() or int(value) <= 0:
            continue
        id = str(value)
        name = str(item.get("name") or "未知玩家")
        if id in identities and identities[id]["name"] != name:
            identities[id]["aliases"].append(name)
            continue
        identities[id] = {"id": id, "name": name, "aliases": [], "alive": []}
    for id, player in identities.items():
        rows = ticks[(ticks["steamid"].astype(str) == id) & (ticks["is_alive"] == True)
                     & (ticks["is_warmup_period"] == False)].sort_values("tick")
        alive_ticks = rows["tick"].drop_duplicates().to_numpy(dtype=np.int64)
        indexes = np.searchsorted(demo_ticks, alive_ticks)
        if len(alive_ticks):
            splits = np.flatnonzero(np.diff(indexes) != 1) + 1
            for part in np.split(alive_ticks, splits):
                player["alive"].append([int(part[0]), int(part[-1])])
    if not identities or not any(p["alive"] for p in identities.values()):
        raise DemoError("Demo 没有带稳定身份的可录玩家。")
    starts = sorted((r for r in events.get("round_start", []) if r.get("is_warmup_period") is False), key=lambda r: r["tick"])
    ends = sorted((r for r in events.get("round_end", []) if r.get("is_warmup_period") is False), key=lambda r: r["tick"])
    rounds = []
    for index, start in enumerate(starts):
        begin = int(start["tick"])
        limit = int(starts[index + 1]["tick"]) if index + 1 < len(starts) else int(demo_ticks[-1]) + 1
        found = next((r for r in ends if begin <= int(r["tick"]) < limit), None)
        rounds.append({"id": index + 1, "number": int(start.get("round") or index + 1),
                       "start": begin, "end": int(found["tick"]) if found else min(limit - 1, int(demo_ticks[-1])),
                       "complete": found is not None})
    deaths = [{"player": str(r.get("user_steamid")), "tick": int(r["tick"])}
              for r in events.get("player_death", []) if r.get("user_steamid") is not None]
    from cs2pov.services.highlights import normalize_kills
    kills, issues = normalize_kills(events.get("player_death", []), identities)
    return {"schema": 2, "parser": "demoparser2-0.42.0", "map": str(header["map_name"]),
            "tick_rate": info["tick_rate"], "duration": info["duration"],
            "timeline": np.column_stack([demo_ticks, server_ticks]).tolist(),
            "players": list(identities.values()), "rounds": rounds, "deaths": deaths, "kills": kills, "kill_issues": issues}


def selection(analysis, player_id, mode, *, round_id=None, start_seconds=None, end_seconds=None, candidate_id=None, start_tick=None, end_tick=None):
    if mode == "highlight":
        from cs2pov.services.highlights import select_highlight
        return select_highlight(analysis, player_id, candidate_id, start_tick, end_tick)
    player = next((p for p in analysis["players"] if p["id"] == player_id), None)
    if player is None:
        raise DemoError("玩家稳定身份不存在，请重新选择。")
    timeline = analysis["timeline"]
    ticks = [pair[0] for pair in timeline]
    rate = analysis["tick_rate"]
    reasons = []
    if mode == "round":
        round = next((r for r in analysis["rounds"] if r["id"] == round_id), None)
        if not round or not round["complete"]:
            raise DemoError("此回合边界不完整，请改选完整回合或有效时间段。")
        start, end = round["start"], round["end"]
        interval = next((span for span in player["alive"] if span[1] >= start and span[0] <= end), None)
        if interval is None:
            raise DemoError("该玩家在此回合没有可录的存活范围。")
        if interval[0] > start:
            reasons.append("回合起点尚不可观战，已从玩家可观战时点开始")
            start = interval[0]
        if interval[1] < end:
            reasons.append("玩家死亡、离开或存活信息中断，已在视角切换前截短")
            end = interval[1]
    elif mode == "time":
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start_seconds, end_seconds)):
            raise DemoError("请输入有效的起止秒数。")
        max_time = (ticks[-1] - ticks[0]) / rate
        if not 0 <= start_seconds < end_seconds <= max_time:
            raise DemoError("时间范围为空、倒置或超出 Demo。")
        left = bisect_left(ticks, ticks[0] + start_seconds * rate)
        right = bisect_right(ticks, ticks[0] + end_seconds * rate) - 1
        start, end = ticks[left], ticks[right]
        if not any(a <= start < end <= b for a, b in player["alive"]):
            raise DemoError("该区间含死亡、不可观战或存活信息中断，请调整到一个连续可录范围。")
    else:
        raise DemoError("未知范围模式。")
    # Death events are an independent safety cap, even if alive data is late.
    death = next((d["tick"] for d in sorted(analysis["deaths"], key=lambda d: d["tick"])
                  if d["player"] == player_id and start <= d["tick"] <= end), None)
    if death is not None:
        if mode == "time":
            raise DemoError("自定义区间跨过死亡时点，请把结束位置移到死亡之前。")
        end_index = bisect_left(ticks, death) - 1
        end = ticks[end_index] if end_index >= 0 else start
        reasons.append("死亡事件前停止，避免自动切到其他玩家")
    if start >= end or start < ticks[0] or end > ticks[-1]:
        raise DemoError("此玩家没有足够的连续可录范围。")
    start_index, end_index = bisect_left(ticks, start), bisect_right(ticks, end) - 1
    start, end = ticks[start_index], ticks[end_index]
    return {"player_id": player_id, "player": player["name"], "map": analysis["map"],
            "mode": mode, "round_id": round_id if mode == "round" else None,
            "start_tick": start, "end_tick": end,
            "server_start_tick": timeline[start_index][1], "server_end_tick": timeline[end_index][1],
            "start_seconds": (start - ticks[0]) / rate, "end_seconds": (end - ticks[0]) / rate,
            "duration": (end - start) / rate, "reasons": reasons,
            "viewpoint_verified": False}
