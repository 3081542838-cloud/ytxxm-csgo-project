"""Real log bytes drive ready-preview validity; no game or native input is used.

Preparation and file transactions have their own tests. These tests begin with
an independently verified paused result, then exercise the production reader,
native-console boundary and ongoing PreviewSession monitoring together.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogDecoder, ReplayLogReader
from cs2pov.services.replay import ReplayController, ReplayError
from tests.unit.test_preview_session import setup as preview_setup


def append_lines(preview, env, *bodies):
    stamp = datetime.fromtimestamp(env.wall).strftime("%m/%d %H:%M:%S")
    with (preview.session / "engine.log").open("a", encoding="utf-8", newline="\n") as stream:
        for body in bodies:
            stream.write(f"{stamp} {body}\n")


def state_report(preview, sequence, *, player=None, **changes):
    clip = preview.draft["selection"]
    state = dict(
        sFileName=preview.draft["demo"], nTick=clip["start_tick"], bIsPaused=True,
        nObserverMode=2, nSpectatingPlayerId=1134,
        bIsPlayingDemoFile=True, bIsPlayingBroadcast=False,
    )
    state.update(changes)
    payload = dict(
        nonce=preview.resource.nonce, sequence=sequence, context="HudDemoController",
        state=state, xuid=player or clip["player_id"],
    )
    return "[PanoramaScript] POV_READBACK " + json.dumps(payload, ensure_ascii=False)


def pause_event(preview, *, tick=None, server=None):
    clip = preview.draft["selection"]
    tick = clip["start_tick"] if tick is None else tick
    server = clip["server_start_tick"] if server is None else server
    return f"[Demo] Demo paused at engine time {server}, demo tick {tick}"


def advance(env, seconds=1):
    env.time += seconds
    env.wall += seconds


@pytest.fixture
def monitored_preview(tmp_path):
    preview, env, _ = preview_setup(tmp_path)
    preview.start()  # All external operations are the existing test fixture's fakes.
    env.pid = preview.game.verify().pid
    env.wall = datetime(2026, 10, 3, 12, 0, 0).timestamp() + 0.25
    preview.console.reader = ReplayLogReader(
        preview.session, preview.game.verify(), preview.resource.nonce,
        clock=lambda: env.time, wall=lambda: env.wall,
    )
    preview.console.confirm_open_and_empty()
    append_lines(preview, env, pause_event(preview), state_report(preview, 10))
    proof = preview.controller.verify_result(preview.draft, since=env.time - 1)
    assert (proof.tick, proof.server_tick, proof.player_id, proof.first_person) == (
        12602, 15934, "76561199198478034", True,
    )
    preview.state = "ready"
    preview._persist()
    assert preview.poll() == proof
    assert preview.state == "ready" and preview.controller.state == "preview_verified"
    before = tuple(env.sent)
    yield preview, env, before
    preview.stop()
    assert preview.state == "complete" and tuple(env.sent) == before
    assert fingerprint(Path(preview.draft["demo"])) == preview.draft["fingerprint"]


@pytest.mark.parametrize("fault", [
    "stale", "replayed_sequence", "position", "server_tick", "camera",
    "paused", "player_identity", "path", "not_local_demo", "broadcast",
])
def test_ready_invalidates_from_real_log_and_requires_explicit_new_preparation(monitored_preview, fault):
    preview, env, before = monitored_preview
    if fault == "stale":
        advance(env, 5.01)  # No newly written telemetry: reading must not refresh old proof.
    else:
        advance(env)
        changes = {}
        player = None
        sequence = 11
        events = []
        if fault == "replayed_sequence":
            sequence = 10
        elif fault == "position":
            changes["nTick"] = 12603
            events.append(pause_event(preview, tick=12603, server=15935))
        elif fault == "server_tick":
            events.append(pause_event(preview, server=15935))
        elif fault == "camera":
            changes["nObserverMode"] = 3
        elif fault == "paused":
            changes["bIsPaused"] = False
        elif fault == "player_identity":
            player = "76561199797409805"
        elif fault == "path":
            changes["sFileName"] = str(preview.session / "other.dem")
            events.append(pause_event(preview))
        elif fault == "not_local_demo":
            changes["bIsPlayingDemoFile"] = False
        elif fault == "broadcast":
            changes["bIsPlayingBroadcast"] = True
        events.append(state_report(preview, sequence, player=player, **changes))
        append_lines(preview, env, *events)

    assert preview.poll() is None
    assert preview.state == "preview_changed" and preview.controller.state == "unverified"
    assert tuple(env.sent) == before and env.closed == env.restored == 0
    assert json.loads((preview.session / "preview-state.json").read_text(encoding="utf-8"))["state"] == "preview_changed"

    # A new, independently anchored valid result is really readable again.
    # Its existence cannot silently authorize playback or resurrect ready.
    advance(env)
    append_lines(preview, env, pause_event(preview), state_report(preview, 12))
    restored = preview.console.readback()
    assert restored is not None and restored.paused and restored.first_person
    assert (restored.tick, restored.server_tick, restored.player_id, str(restored.path)) == (
        12602, 15934, "76561199198478034", preview.draft["demo"],
    )
    assert preview.poll() is None
    assert preview.state == "preview_changed" and preview.controller.state == "unverified"
    assert tuple(env.sent) == before and env.closed == env.restored == 0


def test_fresh_periodic_log_reports_keep_ready_while_app_is_foreground(monitored_preview):
    preview, env, before = monitored_preview
    env.pid = 99  # Reading is allowed without taking focus or issuing keyboard input.
    for sequence in range(11, 17):
        advance(env, 0.5)
        append_lines(preview, env, state_report(preview, sequence))
        proof = preview.poll()
        assert proof is not None and 0 <= env.time - proof.observed_at <= 1
        assert preview.state == "ready" and preview.controller.state == "preview_verified"
        assert tuple(env.sent) == before


def test_new_file_writes_with_old_source_timestamp_do_not_keep_preview_ready(monitored_preview):
    preview, env, before = monitored_preview
    old_wall = env.wall
    advance(env, 6)
    env.wall = old_wall
    append_lines(preview, env, state_report(preview, 11))
    env.wall += 6
    assert preview.poll() is None
    assert preview.state == "preview_changed" and preview.controller.state == "unverified"
    assert tuple(env.sent) == before


@pytest.mark.parametrize("fixture_name,expected_boundaries,middle_tick", [
    ("replay-product-preview-20261003", [(12602, 15934), (13400, 16732)], 13069),
    ("replay-death-boundary-20261003", [(10350, 13682), (10478, 13810)], 10468),
])
def test_actual_product_log_verifies_start_manual_playback_and_end_without_inferred_clock(
    fixture_name, expected_boundaries, middle_tick,
):
    """The excerpt preserves source events; only the Windows account path is anonymized."""
    fixture = Path("tests/fixtures") / (fixture_name + ".log")
    metadata = json.loads(fixture.with_suffix(".json").read_text(encoding="utf-8"))
    raw = fixture.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == metadata["fixture_sha256"]
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == len(metadata["records"]) == 9
    identity = ProcessIdentity(**metadata["process_identity"])
    origin = datetime.strptime("2026/" + lines[0][:14], "%Y/%m/%d %H:%M:%S").timestamp()
    env = SimpleNamespace(wall=origin + 0.25, mono=100.25)
    decoder = ReplayLogDecoder(identity, metadata["nonce"], clock=lambda: env.mono, wall=lambda: env.wall)
    control = ReplayController(
        SimpleNamespace(argv=["cs2.exe", "-insecure"], verify=lambda: identity),
        SimpleNamespace(foreground_pid=lambda: identity.pid, readback=decoder.readback),
        clock=lambda: env.mono,
    )
    (start_tick, start_server_tick), (end_tick, end_server_tick) = expected_boundaries
    draft = dict(
        demo=r"C:\Users\测试员\AppData\Roaming\Wmpvp\demo\9210181067658518284_0.dem",
        selection=dict(start_tick=start_tick, server_start_tick=start_server_tick, end_tick=end_tick,
                       server_end_tick=end_server_tick, player_id="76561199198478034"),
    )
    boundaries = []
    saw_wrong_player = saw_playing_middle = False
    for line, record in zip(lines, metadata["records"]):
        stamp = datetime.strptime("2026/" + line[:14], "%Y/%m/%d %H:%M:%S").timestamp()
        env.wall = stamp + 0.25
        env.mono = 100.25 + stamp - origin
        decoder.consume(line)
        meaning = record["meaning"]
        if meaning == "wrong_player_at_start":
            assert decoder.readback().player_id == "76561199797409805"
            with pytest.raises(ReplayError):
                control.verify_result(draft, since=env.mono - 1)
            saw_wrong_player = True
        elif meaning == "target_first_person_at_start":
            boundaries.append(control.verify_result(draft, since=env.mono - 1))
        elif meaning == "manual_playback_middle":
            snapshot = decoder.snapshot()
            assert snapshot.tick == middle_tick and not snapshot.paused
            assert snapshot.player_id == "76561199198478034" and snapshot.first_person
            assert decoder.readback() is None  # Moving tick is not paused server-tick proof.
            with pytest.raises(ReplayError):
                control.verify_result(draft, since=env.mono - 1, at_end=True)
            saw_playing_middle = True
        elif meaning == "target_first_person_at_end":
            boundaries.append(control.verify_result(draft, since=env.mono - 1, at_end=True))
    assert saw_wrong_player and saw_playing_middle
    assert [(proof.tick, proof.server_tick) for proof in boundaries] == expected_boundaries
    assert control.state == "end_verified"
    if "death_analysis" in metadata:
        # The death row and explicit timeline pairs are copied from the real
        # demoparser cache; the paused log itself never reaches a death event.
        death = metadata["death_analysis"]["death"]
        pairs = metadata["death_analysis"]["timeline_pairs"]
        assert death == {"player": "76561199198478034", "tick": 10479}
        assert pairs == [[10350, 13682], [10478, 13810], [10479, 13811]]
        assert boundaries[-1].tick == death["tick"] - 1
        assert boundaries[-1].server_tick == dict(pairs)[end_tick]
        assert metadata["source_log_finalized_at_extraction"] is False
        assert metadata["source_sha256_at_extraction"] is None  # Live extraction was not hashed.
        assert metadata["source_finalized_after_exit"] is True
        assert metadata["source_sha256"] == "8b4b4dbd3d1f90ba5d2f32e4edee453f2d502d32e68e9920c7ba40b77e8914f3"
