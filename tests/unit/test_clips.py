import copy
import pandas as pd
import pytest
from cs2pov.adapters.demo import DemoError
from cs2pov.services.clips import build_analysis, selection


def analysis_fixture():
    players = [{"steamid": 101, "name": "同名"}, {"steamid": 202, "name": "同名"}]
    rows = [{"tick": tick, "game_time": (tick + 3332 + (100 if tick >= 105 else 0)) / 64,
             "steamid": id, "is_alive": id == 202 or 101 <= tick <= 107,
             "is_warmup_period": False} for tick in range(100, 121) for id in (101, 202)]
    events = {"round_start": [{"tick": 100, "round": 1, "is_warmup_period": False},
                               {"tick": 111, "round": 2, "is_warmup_period": False}],
              "round_end": [{"tick": 110, "is_warmup_period": False}],
              "player_death": [{"tick": 108, "user_steamid": "101"}]}
    return build_analysis({"map_name": "de_test"}, players, events, pd.DataFrame(rows),
                          {"duration": 120 / 64, "tick_rate": 64, "playback_ticks": 120})


def test_same_names_keep_stable_ids_and_round_shows_truncation():
    data = analysis_fixture()
    first = selection(data, "101", "round", round_id=1)
    second = selection(data, "202", "round", round_id=1)
    assert first["start_tick"] == 101 and first["end_tick"] == 107
    assert second["start_tick"] == 100 and second["end_tick"] == 110
    assert len(first["reasons"]) == 2 and not first["viewpoint_verified"]


def test_mapping_uses_actual_time_points_not_a_fixed_offset():
    clip = selection(analysis_fixture(), "202", "time", start_seconds=4 / 64, end_seconds=6 / 64)
    assert clip["start_tick"] == 104 and clip["server_start_tick"] == 3436
    assert clip["end_tick"] == 106 and clip["server_end_tick"] == 3538
    assert clip["duration"] == 2 / 64


@pytest.mark.parametrize("options", [
    {"start_seconds": -1, "end_seconds": .1}, {"start_seconds": .1, "end_seconds": .1},
    {"start_seconds": .2, "end_seconds": .1}, {"start_seconds": 0, "end_seconds": 3},
    {"start_seconds": float("nan"), "end_seconds": .1},
    {"start_seconds": True, "end_seconds": .1},
])
def test_invalid_time_ranges_block(options):
    with pytest.raises(DemoError):
        selection(analysis_fixture(), "202", "time", **options)


def test_custom_range_cannot_cross_death_or_start_before_observable():
    for start, end in ((0, 5 / 64), (5 / 64, 10 / 64)):
        with pytest.raises(DemoError, match="死亡"):
            selection(analysis_fixture(), "101", "time", start_seconds=start, end_seconds=end)
    clip = selection(analysis_fixture(), "101", "time", start_seconds=1 / 64, end_seconds=7 / 64)
    assert clip["end_tick"] == 107


def test_death_event_independently_caps_late_alive_state():
    data = analysis_fixture()
    data["players"][0]["alive"] = [[100, 120]]
    assert selection(data, "101", "round", round_id=1)["end_tick"] == 107
    with pytest.raises(DemoError, match="死亡时点"):
        selection(data, "101", "time", start_seconds=0, end_seconds=10 / 64)


def test_incomplete_round_unknown_player_and_zero_observable_range_fail():
    data = analysis_fixture()
    with pytest.raises(DemoError, match="不完整"):
        selection(data, "202", "round", round_id=2)
    with pytest.raises(DemoError, match="稳定身份"):
        selection(data, "同名", "round", round_id=1)
    data["players"][0]["alive"] = [[105, 105]]
    with pytest.raises(DemoError, match="足够"):
        selection(data, "101", "round", round_id=1)


def test_missing_alive_tick_splits_recordable_span():
    data = analysis_fixture()
    data["players"][1]["alive"] = [[100, 104], [106, 120]]
    with pytest.raises(DemoError, match="中断"):
        selection(data, "202", "time", start_seconds=0, end_seconds=7 / 64)
