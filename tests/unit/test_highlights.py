import copy
import pytest
from cs2pov.services.highlights import normalize_kills, candidates, select_highlight
from cs2pov.adapters.demo import DemoError


def sample():
    return {"map": "test", "tick_rate": 10, "timeline": [[i, i+100] for i in range(1001)],
            "players": [{"id": "1", "name": "同名", "alive": [[0, 1000]]},
                        {"id": "2", "name": "同名", "alive": [[0, 1000]]}],
            "rounds": [{"id": 1, "number": 1, "start": 0, "end": 800, "complete": True},
                       {"id": 2, "number": 2, "start": 801, "end": 1000, "complete": True}],
            "deaths": [], "kills": [{"player": "1", "victim": "2", "tick": t} for t in (150, 700)]}


def test_normalize_requires_identity_team_warmup_and_deduplicates():
    base = dict(tick=100, attacker_steamid="1", user_steamid="2", attacker_team_num=2,
                user_team_num=3, is_warmup_period=False)
    events = [base, base, {**base, "user_steamid": "3"},
              {**base, "tick": 101, "user_team_num": 2},
              {**base, "tick": 102, "user_steamid": "1"},
              {**base, "tick": 103, "is_warmup_period": True},
              {**base, "tick": 104, "attacker_steamid": "0"}]
    for key in ("attacker_team_num", "is_warmup_period", "user_steamid"):
        event = dict(base); event.pop(key); events.append(event)
    kills, issues = normalize_kills(events, {"1", "2", "3"})
    assert len(kills) == 2 and [k["victim"] for k in kills] == ["2", "3"]
    assert len(issues) == 3


def test_event_controller_team_fills_missing_pawn_but_conflicts_fail_closed():
    event = {"tick": 100, "attacker_steamid": "1", "user_steamid": "2",
             "attacker_team_num": float("nan"), "user_team_num": 2,
             "attacker_CCSPlayerController.m_iTeamNum": 3,
             "user_CCSPlayerController.m_iTeamNum": 2, "is_warmup_period": False}
    kills, issues = normalize_kills([event], {"1", "2"})
    assert len(kills) == 1 and not issues
    event["attacker_team_num"] = 2
    kills, issues = normalize_kills([event], {"1", "2"})
    assert not kills and len(issues) == 1


@pytest.mark.parametrize("count", [2, 3, 5])
def test_same_round_no_gap_limit_and_stable_identity(count):
    data = sample()
    data["kills"] += [{"player": "1", "tick": 300+i} for i in range(count-2)]
    data["kills"] += [{"player": "1", "tick": 900}, {"player": "2", "tick": 400}]
    found = candidates(data, "1")
    assert len(found) == 1 and len(found[0]["kill_ticks"]) == count
    assert (found[0]["start_tick"], found[0]["end_tick"]) == (50, 800)
    assert candidates(data, "2") == []


def test_boundaries_and_death_are_independent():
    data = sample(); data["deaths"] = [{"player": "1", "tick": 720}]
    data["players"][0]["alive"] = [[100, 1000]]
    c = candidates(data, "1")[0]
    assert (c["start_tick"], c["end_tick"]) == (100, 719)
    assert len(c["reasons"]) == 2
    data["deaths"][0]["tick"] = 700
    assert not candidates(data, "1")[0]["available"]


@pytest.mark.parametrize("change", ["gap", "round", "same_death"])
def test_unavailable_never_drops_a_kill(change):
    data = sample()
    if change == "gap": data["players"][0]["alive"] = [[0, 500], [600, 1000]]
    if change == "round": data["rounds"][0]["complete"] = False
    if change == "same_death": data["deaths"] = [{"player": "1", "tick": 700}]
    c = candidates(data, "1")[0]
    assert not c["available"] and c["kill_ticks"] == [150, 700] and c["reason"]
    with pytest.raises(DemoError): select_highlight(data, "1", c["id"], 0, 800)


@pytest.mark.parametrize("start,end", [(151, 750), (50, 699), (-1, 750), (50, 1001), (True, 750), (50, 50)])
def test_selection_revalidates_core_and_round(start, end):
    with pytest.raises(DemoError): select_highlight(sample(), "1", "1:1", start, end)


def test_same_tick_multikill_and_noninteger_rate():
    data = sample(); data["tick_rate"] = 64.1
    data["kills"][1]["tick"] = 150
    c = candidates(data, "1")[0]
    assert c["available"] and c["kill_ticks"] == [150, 150]
    result = select_highlight(data, "1", c["id"], c["start_tick"], c["end_tick"])
    assert result["kill_ticks"] == [150, 150] and result["mode"] == "highlight"


def test_unknown_candidate_cannot_be_saved():
    with pytest.raises(DemoError): select_highlight(sample(), "1", "2:1", 50, 800)


@pytest.mark.parametrize("rate", [63.9, 64.1, 128.00001])
def test_noninteger_rate_does_not_move_chosen_ticks(rate):
    data = sample(); data["tick_rate"] = rate
    for start in range(1, 150):
        clip = select_highlight(data, "1", "1:1", start, 723)
        assert (clip["start_tick"], clip["end_tick"]) == (start, 723)


def test_round_end_does_not_remove_ten_second_tail():
    data = sample()
    data["rounds"][0]["end"] = 700
    c = candidates(data, "1")[0]
    assert c["end_tick"] == 800 and c["maximum"] == 1000
    assert not c["reasons"]
    clip = select_highlight(data, "1", c["id"], c["start_tick"], 800)
    assert clip["end_tick"] == 800
