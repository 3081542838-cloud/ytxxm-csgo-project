"""Deterministic event-based highlights; never infer missing team identities."""
from bisect import bisect_left, bisect_right
import math
from cs2pov.adapters.demo import DemoError


def event_team(event, prefix):
    values = [event.get(prefix + name) for name in ("_team_num", "_CCSPlayerController.m_iTeamNum")]
    valid = {int(v) for v in values if type(v) in (int, float) and v in (2, 3)}
    # Both are captured at the event tick. A disagreement is not safe to guess.
    return valid.pop() if len(valid) == 1 else None


def normalize_kills(events, identities):
    kills, issues, seen = [], [], set()
    for event in events:
        attacker, victim = str(event.get("attacker_steamid")), str(event.get("user_steamid"))
        tick = event["tick"]
        if event.get("is_warmup_period") is True or attacker == victim:
            continue
        if attacker in ("0", "None", ""):
            continue  # World/environment death has no player attacker.
        teams = (event_team(event, "attacker"), event_team(event, "user"))
        if (attacker not in identities or victim not in identities
                or event.get("is_warmup_period") is not False
                or any(type(t) not in (int, float) or t not in (2, 3) for t in teams)):
            issues.append({"tick": int(tick), "player": attacker,
                           "reason": "身份、队伍或热身信息缺失，此次击杀未计入"})
            continue
        if teams[0] == teams[1]:
            continue
        key = (attacker, victim, int(tick))
        if key not in seen:
            seen.add(key)
            kills.append({"player": attacker, "victim": victim, "tick": int(tick)})
    return sorted(kills, key=lambda k: k["tick"]), issues


def candidates(analysis, player_id):
    player = next((p for p in analysis["players"] if p["id"] == player_id), None)
    if player is None:
        return []
    ticks = [p[0] for p in analysis["timeline"]]
    rate = analysis["tick_rate"]
    result = []
    for round in analysis["rounds"]:
        kills = [k["tick"] for k in analysis.get("kills", []) if k["player"] == player_id
                 and round["start"] <= k["tick"] <= round["end"]]
        if len(kills) < 2:
            continue
        kills.sort()
        item = {"id": f"{player_id}:{round['id']}", "round_id": round["id"],
                "number": round["number"], "kill_ticks": kills, "available": False,
                "reason": "", "reasons": []}
        result.append(item)
        if not round["complete"]:
            item["reason"] = "回合边界不完整"
            continue
        span = next((s for s in player["alive"] if s[0] <= kills[0] <= kills[-1] <= s[1]), None)
        if span is None:
            item["reason"] = "全部击杀无法落在同一连续存活范围"
            continue
        low = max(span[0], ticks[0])
        high = min(span[1], ticks[-1])
        deaths = [d["tick"] for d in analysis["deaths"] if d["player"] == player_id
                  and low <= d["tick"] <= high]
        if deaths:
            high = min(high, min(deaths) - 1)
        left, right = bisect_left(ticks, low), bisect_right(ticks, high) - 1
        if left >= len(ticks) or right < 0 or ticks[left] >= ticks[right] or ticks[left] > kills[0] or ticks[right] < kills[-1]:
            item["reason"] = "死亡或有效时间边界使片段无法保留全部击杀"
            continue
        low, high = ticks[left], ticks[right]
        wanted_start, wanted_end = kills[0] - 10 * rate, kills[-1] + 10 * rate
        start = ticks[max(left, bisect_right(ticks, wanted_start) - 1)]
        end = ticks[min(right, bisect_left(ticks, wanted_end))]
        if low > wanted_start:
            item["reasons"].append("前置不足 10 秒：已到玩家连续可观战或 Demo 起点")
        if high < wanted_end:
            item["reasons"].append("后置不足 10 秒：已到死亡、连续可观战或 Demo 终点")
        item.update(available=True, minimum=low, maximum=high, start_tick=start, end_tick=end)
    return result


def select_highlight(analysis, player_id, candidate_id, start, end):
    from cs2pov.services.clips import selection
    item = next((c for c in candidates(analysis, player_id) if c["id"] == candidate_id), None)
    if item is None or not item["available"]:
        raise DemoError("多杀候选不存在或不可录，请重新选择。")
    ticks = [p[0] for p in analysis["timeline"]]
    if (type(start) is not int or type(end) is not int or start not in ticks or end not in ticks
            or not item["minimum"] <= start < end <= item["maximum"]
            or start > item["kill_ticks"][0] or end < item["kill_ticks"][-1]):
        raise DemoError("范围必须保留全部击杀，且位于连续可录区间。")
    rate, origin = analysis["tick_rate"], ticks[0]
    # Reuse the existing independent death and alive checks, at exact tick inputs.
    # Outward rounding prevents floating division from losing a boundary tick.
    clip = selection(analysis, player_id, "time",
                     start_seconds=max(0, math.nextafter((start-origin)/rate, -math.inf)),
                     end_seconds=min((ticks[-1]-origin)/rate,
                                     math.nextafter((end-origin)/rate, math.inf)))
    if (clip["start_tick"], clip["end_tick"]) != (start, end):
        raise DemoError("时间转换无法精确保持选定 tick。")
    clip.update(mode="highlight", round_id=item["round_id"], candidate_id=item["id"],
                kill_ticks=item["kill_ticks"], reasons=item["reasons"])
    return clip
