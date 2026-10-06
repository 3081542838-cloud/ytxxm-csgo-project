from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogDecoder, ReplayLogReader
from cs2pov.services.replay import ReplayController, ReplayError
from cs2pov.storage.settings import DataError

NONCE = "READBACK_20261003_04"
IDENTITY = ProcessIdentity(28612, 100, "C:/game/cs2.exe")
DEMO = Path("C:/Users/测试员/AppData/Roaming/Wmpvp/demo/9210181067658518284_0.dem")
WALL = datetime(2026, 10, 3, 2, 39, 49).timestamp()


def decoder():
    timer = SimpleNamespace(mono=100.0, wall=WALL)
    value = ReplayLogDecoder(IDENTITY, NONCE, clock=lambda: timer.mono, wall=lambda: timer.wall)
    return value, timer


def report(sequence=1, **changes):
    state = dict(sFileName=str(DEMO), nTick=13400, bIsPaused=True, nObserverMode=2,
                 nSpectatingPlayerId=1288, bIsPlayingDemoFile=True, bIsPlayingBroadcast=False)
    state.update(changes)
    return dict(nonce=NONCE, sequence=sequence, context="HudDemoController", state=state,
                xuid="76561199198478034")


def emit(value, data, prefix="[PanoramaScript] POV_READBACK ", time="02:39:49"):
    value.consume(f"10/03 {time} {prefix}" + json.dumps(data, ensure_ascii=False))


def pause(value):
    value.consume("10/03 02:39:49 CGameRules - paused on tick 16732")


def test_same_event_rules_pause_and_demo_summary_keep_actual_panorama_clock():
    value, _ = decoder()
    value.consume('10/03 02:39:49 CGameRules - paused on tick 15934')
    value.consume('10/03 02:39:49 [Demo] Demo paused at engine time 15934, demo tick 12604')
    assert value.readback() is None  # The two engine messages cannot establish a player.
    emit(value, report(nTick=12602))
    proof = value.readback()
    assert proof is not None
    assert (proof.tick, proof.server_tick) == (12602, 15934)
    assert proof.player_id == '76561199198478034' and proof.first_person
    emit(value, report(sequence=2, nTick=12604))
    assert value.readback() is None  # Moving the established clock still invalidates it.


@pytest.mark.parametrize('prior', [
    '',
    '10/03 02:39:49 [Console] CGameRules - paused on tick 15934',
    '10/03 02:39:48 CGameRules - paused on tick 15934',
    '10/03 02:39:49 CGameRules - paused on tick 15933',
    '10/03 02:39:49 CGameRules - paused on tick 15934\n'
    '10/03 02:39:49 CGameRules - unpaused on tick 15934, pause duration was 0 ticks',
])
def test_unpaired_demo_summary_still_requires_its_exact_clock(prior):
    value, _ = decoder()
    for line in prior.splitlines():
        value.consume(line)
    value.consume('10/03 02:39:49 [Demo] Demo paused at engine time 15934, demo tick 12604')
    emit(value, report(nTick=12602))
    assert value.readback() is None and value.anchor is None


def test_invalid_report_cannot_reuse_paired_rules_pause():
    value, _ = decoder()
    value.consume('10/03 02:39:49 CGameRules - paused on tick 15934')
    value.consume('10/03 02:39:49 [Demo] Demo paused at engine time 15934, demo tick 12604')
    invalid = report(nTick=12602)
    invalid['nonce'] = 'FOREIGN_SESSION_00000000'
    emit(value, invalid)
    emit(value, report(sequence=2, nTick=12602))
    assert value.readback() is None


def test_real_engine_excerpt_tracks_identity_camera_and_exact_tick_difference():
    value, timer = decoder()
    lines = Path("tests/fixtures/replay-readback-20261003.log").read_text(encoding="utf-8").splitlines()
    saw_wrong_player = saw_target = saw_chase = False
    for line in lines:
        stamp = datetime.strptime("2026/" + line[:14], "%Y/%m/%d %H:%M:%S").timestamp()
        timer.wall = stamp + 0.25
        value.consume(line)
        proof = value.readback()
        if '"sequence":99,' in line:
            assert proof.tick == 12602 and proof.server_tick == 15935
            assert proof.player_id == "76561199797409805"
            saw_wrong_player = True
        if '"sequence":168,' in line:
            assert proof.player_id == "76561199198478034" and proof.first_person
            saw_target = True
        if '"sequence":245,' in line:
            assert proof.player_id == "76561199198478034" and not proof.first_person
            saw_chase = True
    assert saw_wrong_player and saw_target and saw_chase
    assert value.readback().tick == 13400 and value.readback().server_tick == 16732
    assert value.readback().first_person and value.readback().paused
    assert value.readback().path == DEMO


def test_new_patch_real_seek_tail_cannot_be_mistaken_for_paused_selected_clip():
    fixture = Path('tests/fixtures/patch-replay-20261006.log')
    manifest = json.loads(fixture.with_suffix('.json').read_text(encoding='utf-8'))
    import hashlib
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == manifest['fixture_sha256']
    assert manifest['patch_version'] == '1.41.8.9' and not manifest['clip_ready']
    wall = [datetime(2026, 10, 6, 14, 56, 29).timestamp()]
    value = ReplayLogDecoder(IDENTITY, manifest['nonce'], clock=lambda:100, wall=lambda:wall[0])
    initial = None
    for line in fixture.read_text(encoding='utf-8').splitlines():
        wall[0] = datetime.strptime('2026/'+line[:14], '%Y/%m/%d %H:%M:%S').timestamp()+.25
        value.consume(line)
        if '"sequence":8,' in line:
            initial = value.readback()
    assert initial is not None and initial.first_person and initial.paused
    assert initial.player_id != manifest['requested_player_xuid']
    snapshot = value.snapshot()
    assert snapshot.local_demo and snapshot.first_person and not snapshot.paused
    assert snapshot.tick > manifest['requested_preroll_demo_tick']
    assert value.readback() is None


def test_fresh_periodic_report_does_not_refresh_old_source_time():
    value, timer = decoder(); pause(value); emit(value, report())
    original = value.readback()
    timer.mono += 6; timer.wall += 6
    assert value.readback() is None
    emit(value, report(sequence=2))
    assert value.readback() is None and value.proof is None
    assert original.observed_at == 100


def test_echo_or_chat_cannot_supply_pause_or_panorama_evidence():
    value, _ = decoder()
    value.consume("10/03 02:39:49 [Console] CGameRules - paused on tick 16732")
    emit(value, report(), prefix="[Console] echo [PanoramaScript] POV_READBACK ")
    assert value.readback() is None
    pause(value)
    emit(value, report(), prefix="[ALL] someone: [PanoramaScript] POV_READBACK ")
    assert value.readback() is None


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(nonce="OTHER_SESSION_00000000"),
    lambda r: r.update(context="MainMenu"),
    lambda r: r.update(xuid=76561199198478034),
    lambda r: r.update(xuid=""),
    lambda r: r.update(sequence=True),
    lambda r: r.update(extra="unknown"),
    lambda r: r["state"].update(bIsPaused="true"),
    lambda r: r["state"].update(nTick=True),
    lambda r: r["state"].pop("nObserverMode"),
    lambda r: r["state"].update(nTick=-1),
    lambda r: r["state"].update(sFileName="//server/share/test.dem"),
    lambda r: r["state"].update(sFileName="C:/video.mp4"),
])
def test_invalid_report_clears_previously_valid_proof(mutation):
    value, _ = decoder(); pause(value); emit(value, report())
    assert value.readback() is not None
    data = report(sequence=2); mutation(data); emit(value, data)
    assert value.readback() is None and value.anchor is None


def test_duplicate_fields_truncation_replayed_sequence_and_errors_invalidate():
    for bad in ('{"nonce":"x","nonce":"y"}', '{"state":',
                json.dumps(report()), 'x' * 2049):
        value, _ = decoder(); pause(value); emit(value, report())
        value.consume("10/03 02:39:49 [PanoramaScript] POV_READBACK " + bad)
        assert value.readback() is None
    value, _ = decoder(); pause(value); emit(value, report())
    value.consume("10/03 02:39:49 [PanoramaScript] POV_READBACK_ERROR invalid panel")
    assert value.readback() is None


def test_menu_null_state_cannot_block_fresh_demo_context_or_reset_valid_replay_protection():
    value,_=decoder()
    menu=report(sequence=500);menu['state']=None
    emit(value,menu)
    assert value.sequence==0 and value.readback() is None
    pause(value);emit(value,report(sequence=1))
    assert value.readback() is not None and value.sequence==1
    emit(value,menu)
    assert value.readback() is None and value.sequence==1
    pause(value);emit(value,report(sequence=1))
    assert value.readback() is None  # An invalid context cannot reset accepted history.


@pytest.mark.parametrize("changes", [{"bIsPaused":False}, {"bIsPlayingDemoFile":False},
                                      {"bIsPlayingBroadcast":True}, {"nTick":13401},
                                      {"sFileName":"C:/other.dem"}])
def test_state_changes_require_new_independent_pause_evidence(changes):
    value, _ = decoder(); pause(value); emit(value, report())
    emit(value, report(sequence=2, **changes))
    assert value.readback() is None
    emit(value, report(sequence=3))
    assert value.readback() is None


def test_engine_end_tick_must_match_report_and_does_not_guess_expected_tick():
    value, _ = decoder()
    value.consume("10/03 02:39:49 [Demo] Demo paused at engine time 16732, demo tick 13400")
    emit(value, report(nTick=13401))
    assert value.readback() is None
    value.consume("10/03 02:39:49 CGameRules - paused on tick 15935")
    emit(value, report(sequence=2, nTick=12602))
    proof = value.readback()
    game = SimpleNamespace(argv=["cs2.exe", "-insecure"], verify=lambda: IDENTITY)
    console = SimpleNamespace(readback=lambda: proof, foreground_pid=lambda: IDENTITY.pid)
    control = ReplayController(game, console, clock=lambda: 100)
    draft = dict(demo=str(DEMO), selection=dict(start_tick=12602, server_start_tick=15934,
                                              player_id="76561199198478034"))
    with pytest.raises(ReplayError, match="回读"):
        control.verify_result(draft, since=99)
    assert control.state == "unverified"  # Never hide the real one-tick difference.


def test_log_reader_ignores_history_and_detects_partial_unicode_and_rotation(tmp_path):
    logfile = tmp_path/"engine.log"
    logfile.write_text("old history\n", encoding="utf-8")
    reader = ReplayLogReader(tmp_path, IDENTITY, NONCE, clock=lambda:100, wall=lambda:WALL)
    assert reader.readback() is None
    text = ("10/03 02:39:49 CGameRules - paused on tick 16732\n"
            + "10/03 02:39:49 [PanoramaScript] POV_READBACK "
            + json.dumps(report(), ensure_ascii=False) + "\n").encode("utf-8")
    split = text.index("测".encode("utf-8")) + 1
    with logfile.open("ab") as stream: stream.write(text[:split])
    assert reader.readback() is None
    with logfile.open("ab") as stream: stream.write(text[split:])
    assert reader.readback().player_id == "76561199198478034"
    logfile.write_text("x", encoding="utf-8")
    with pytest.raises(DataError): reader.readback()
    assert reader.decoder.readback() is None


def test_real_lead_in_and_end_pass_exact_controller_gate_without_tick_adjustment():
    value, timer = decoder()
    identity = IDENTITY
    control = ReplayController(SimpleNamespace(argv=['cs2.exe','-insecure'],verify=lambda:identity),
        SimpleNamespace(foreground_pid=lambda:identity.pid,readback=value.readback),clock=lambda:timer.mono)
    draft = dict(demo=str(DEMO),selection=dict(start_tick=12602,server_start_tick=15934,
        end_tick=13400,server_end_tick=16732,player_id='76561199198478034'))
    checks = []
    for line in Path('tests/fixtures/replay-exact-boundaries-20261003.log').read_text(encoding='utf-8').splitlines():
        timer.wall = datetime.strptime('2026/'+line[:14],'%Y/%m/%d %H:%M:%S').timestamp()+.25
        value.consume(line)
        if '"sequence":241,' in line:
            with pytest.raises(ReplayError): control.verify_result(draft,since=99)
        if '"sequence":799,' in line:
            checks.append(control.verify_result(draft,since=99))
        if '"sequence":844,' in line:
            checks.append(control.verify_result(draft,since=99,at_end=True))
    assert [(p.tick,p.server_tick) for p in checks]==[(12602,15934),(13400,16732)]
    assert control.state=='end_verified'
