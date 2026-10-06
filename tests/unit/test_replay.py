from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.demo import fingerprint
from cs2pov.services.replay import (ReplayController, ReplayError, ReplayEvidence, quoted_name)
from cs2pov.storage.settings import HudPreset


def setup(tmp_path):
    path = tmp_path / "test.dem"; path.write_bytes(b"reference")
    clip = dict(player_id="101", start_tick=10, end_tick=20, server_start_tick=100,
                server_end_tick=110, hud=asdict(HudPreset()), content_sha256=fingerprint(path)["sha256"])
    draft = {"demo": str(path), "fingerprint": fingerprint(path), "selection": clip}
    analysis = {"players": [{"id": "101", "name": "小虾米", "aliases": []}]}
    identity = ProcessIdentity(123, 456, "C:/game/cs2.exe")
    proof = ReplayEvidence(identity, True, path, 10, 100, True, "101", True, 10)
    sent = []
    game = SimpleNamespace(argv=["cs2.exe", "-insecure"], verify=lambda: identity)
    console = SimpleNamespace(foreground_pid=lambda: 123, command=sent.append, readback=lambda: proof)
    controller = ReplayController(game, console, clock=lambda: 10)
    return controller, analysis, draft, proof, sent


def test_input_is_not_success_and_only_complete_fresh_evidence_allows_preview(tmp_path):
    controller, analysis, draft, proof, sent = setup(tmp_path)
    assert controller.prepare(analysis, draft) == proof
    assert controller.state == "preview_verified" and "demo_gototick 10; demo_pause" in sent
    assert "demo_gototick 10" not in sent
    assert 'spec_player "小虾米"' in sent
    assert "spec_mode 2" in sent and "spec_mode 1" not in sent
    controller.console.readback = lambda: None
    with pytest.raises(ReplayError): controller.prepare(analysis, draft)
    assert controller.state == "unverified"


def test_prepare_hides_trueview_status_without_hiding_pov_hud(tmp_path):
    controller, analysis, draft, proof, sent = setup(tmp_path)
    assert controller.prepare(analysis, draft) == proof
    assert sent.count("cl_trueview_show_status 0") == 1
    assert "cl_drawhud 0" not in sent
    assert "cl_demo_predict 0" not in sent and "cl_demo_predict 1" not in sent
    assert "cl_drawhud_force_radar -1" in sent
    assert "spec_mode 2" in sent
    assert any(command.startswith("hud_scaling ") for command in sent)
    assert any(command.startswith("viewmodel_fov ") for command in sent)


def test_trueview_status_input_failure_keeps_preview_unverified(tmp_path):
    controller, analysis, draft, _, sent = setup(tmp_path)

    def fail_status_command(command):
        sent.append(command)
        if command == "cl_trueview_show_status 0":
            raise ReplayError("status input outcome unknown")

    controller.console.command = fail_status_command
    with pytest.raises(ReplayError, match="status input outcome unknown"):
        controller.prepare(analysis, draft)
    assert controller.state == "unverified"
    assert sent[-1] == "cl_trueview_show_status 0"
    assert sent.count("cl_trueview_show_status 0") == 1
    assert not any(command.startswith("hud_scaling ") for command in sent)
    assert not any(command.startswith("demo_resume") for command in sent)


@pytest.mark.parametrize("changes", [{"local_demo": False}, {"tick": 11}, {"server_tick": 101},
    {"paused": False}, {"player_id": "202"}, {"first_person": False}, {"observed_at": 9},
    {"observed_at": 11}, {"path": Path("C:/other.dem")},
    {"process_identity": ProcessIdentity(123, 457, "C:/game/cs2.exe")}])
def test_wrong_or_stale_evidence_never_confirms_preview(tmp_path, changes):
    controller, analysis, draft, proof, _ = setup(tmp_path)
    controller.console.readback = lambda: replace(proof, **changes)
    with pytest.raises(ReplayError): controller.prepare(analysis, draft)
    assert controller.state == "unverified"


def test_focus_loss_halfway_stops_further_commands(tmp_path):
    controller, analysis, draft, _, sent = setup(tmp_path)
    controller.console.foreground_pid = lambda: 123 if len(sent) < 2 else 999
    with pytest.raises(ReplayError, match="前台"):
        controller.prepare(analysis, draft)
    assert len(sent) == 2 and controller.state == "focus_lost"


def test_changed_demo_blocks_before_any_input(tmp_path):
    controller, analysis, draft, _, sent = setup(tmp_path)
    Path(draft["demo"]).write_bytes(b"changed")
    with pytest.raises(ReplayError, match="已变化"):
        controller.prepare(analysis, draft)
    assert sent == []


@pytest.mark.parametrize("other", ["小虾米", "另一位小虾米", " 小虾米 "])
def test_ambiguous_names_block_without_guessing_slots(other):
    players = [{"id": "101", "name": "小虾米"}, {"id": "202", "name": other}]
    with pytest.raises(ReplayError, match="歧义"):
        quoted_name({"players": players}, "101")


def test_console_injection_and_missing_insecure_evidence_block(tmp_path):
    controller, analysis, draft, _, sent = setup(tmp_path)
    analysis["players"][0]["name"] = 'name";quit'
    with pytest.raises(ReplayError): controller.prepare(analysis, draft)
    assert sent == []
    analysis["players"][0]["name"] = "正常"
    controller.game.argv = ["cs2.exe"]
    with pytest.raises(ReplayError, match="insecure"):
        controller.prepare(analysis, draft)
    assert sent == []


def test_player_name_keeps_significant_leading_space():
    analysis = {"players": [{"id": "101", "name": " 一条小虾米OVO", "aliases": []}]}
    assert quoted_name(analysis, "101") == '" 一条小虾米OVO"'


def test_playback_requires_verified_preview_and_end_readback(tmp_path):
    controller, analysis, draft, proof, sent = setup(tmp_path)
    with pytest.raises(ReplayError): controller.resume()
    with pytest.raises(ReplayError): controller.arm_end(draft)
    controller.prepare(analysis, draft)
    before = list(sent)
    controller.arm_end(draft)
    assert controller.state == "end_armed" and sent == before
    controller.resume()
    assert sent[-1] == "demo_resume; demo_pauseatservertick 110"
    assert "demo_resume" not in sent and "demo_pauseatservertick 110" not in sent
    assert controller.state == "playing_unverified"
    with pytest.raises(ReplayError): controller.verify_result(draft, since=10, at_end=True)
    controller.console.readback = lambda: replace(proof, tick=20, server_tick=110)
    controller.verify_result(draft, since=10, at_end=True)
    assert controller.state == "end_verified"


def test_readonly_end_verification_allows_app_focus_but_still_checks_every_field(tmp_path):
    controller, _, draft, proof, sent = setup(tmp_path)
    controller.console.foreground_pid = lambda: 999
    end = replace(proof, tick=20, server_tick=110)
    controller.console.readback = lambda: end
    assert controller.verify_result(draft, since=10, at_end=True, require_foreground=False)==end
    assert sent==[]
    with pytest.raises(ReplayError,match='前台'):
        controller.verify_result(draft,since=10,at_end=True)
    for changes in ({'tick':21},{'server_tick':111},{'player_id':'202'},
                    {'paused':False},{'observed_at':4},{'local_demo':False},
                    {'process_identity':ProcessIdentity(123,457,'C:/game/cs2.exe')}):
        controller.console.readback=lambda:replace(end,**changes)
        with pytest.raises(ReplayError):
            controller.verify_result(draft,since=10,at_end=True,require_foreground=False)
    controller.game.argv=['cs2.exe']
    with pytest.raises(ReplayError,match='insecure'):
        controller.verify_result(draft,since=10,at_end=True,require_foreground=False)
    assert sent==[]
