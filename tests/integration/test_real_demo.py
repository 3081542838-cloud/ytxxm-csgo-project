"""Mandatory local sample integration; never silently skipped when unavailable."""
from pathlib import Path
import json
from cs2pov.adapters.demo import fingerprint
from cs2pov.services.demo_worker import analyse
from cs2pov.services.clips import selection

DEMO = Path.home() / "AppData/Roaming/Wmpvp/demo/9210181067658518284_0.dem"


def test_original_demo_identity_timing_and_readonly_parse(tmp_path):
    before = fingerprint(DEMO)
    assert before["sha256"] == "c2a52a4b2d536053c64f369bc804fa19a3607f0d8484f9444606cce3485867b4"
    result = analyse(DEMO, tmp_path / "cache")
    data = json.loads(Path(result["cache"]).read_text(encoding="utf-8"))["analysis"]
    assert data["map"] == "de_mirage" and len(data["players"]) == 10 and len(data["rounds"]) == 19
    assert all(round["complete"] for round in data["rounds"])
    assert data["tick_rate"] == 64 and data["timeline"][-1][0] == 121590
    own = next(p for p in data["players"] if p["id"] == "76561199198478034")
    assert own["name"].strip() == "一条小虾米OVO"
    clip = selection(data, own["id"], "time", start_seconds=12602 / 64, end_seconds=13400 / 64)
    assert (clip["start_tick"], clip["end_tick"]) == (12602, 13400)
    assert (clip["server_start_tick"], clip["server_end_tick"]) == (15934, 16732)
    assert clip["duration"] == 12.46875
    round = selection(data, own["id"], "round", round_id=3)
    assert round["start_tick"] < 12602 and round["end_tick"] >= 13400
    assert fingerprint(DEMO) == before
    assert not list(tmp_path.rglob("*.dem"))
    from cs2pov.services.highlights import candidates, select_highlight
    highlights = candidates(data, own["id"])
    assert data["schema"] == 2 and len(data["kills"]) == 137 and len(data["kill_issues"]) == 1
    assert [c["round_id"] for c in highlights] == [4, 7, 8, 10, 11, 15, 16, 17, 18, 19]
    assert next(c for c in highlights if c["round_id"] == 11)["kill_ticks"] == [68200, 68492, 68558, 69168, 72535]
    third = next(c for c in highlights if c["round_id"] == 8)
    assert third["kill_ticks"] == [48303, 49010, 50469]
    assert (third["start_tick"], third["end_tick"]) == (47663, 51109)
    for candidate in highlights:
        selected = select_highlight(data, own["id"], candidate["id"], candidate["start_tick"], candidate["end_tick"])
        assert selected["start_tick"] <= min(selected["kill_ticks"]) <= max(selected["kill_ticks"]) <= selected["end_tick"]
