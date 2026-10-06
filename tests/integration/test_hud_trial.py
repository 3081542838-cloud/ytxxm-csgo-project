from pathlib import Path
import pytest
from cs2pov.adapters.vpk import validate_resource
from cs2pov.services.hud_trial import (TrialCheck, TRIAL_SHA256, TRIAL_PATHS,
                                      prepare_trial, trial_transaction)
from cs2pov.storage.transaction import RecoveryError, attributes


def candidate():
    return Path(__file__).resolve().parents[2] / "resources/private/minimal-pov/pov.vpk"


def test_real_reviewed_candidate_has_only_approved_entries():
    archive = validate_resource(candidate(), TRIAL_SHA256, set(TRIAL_PATHS))
    assert {e.path for e in archive.entries} == TRIAL_PATHS
    assert all("sound" not in e.path for e in archive.entries)


def check_fixture(tmp_path, patch_version='1.41.8.8'):
    import hashlib
    installation = tmp_path / "steam/common/cs2"
    csgo = installation / "game/csgo"
    csgo.mkdir(parents=True)
    gameinfo = csgo / "gameinfo.gi"
    raw = b'GameInfo\n{\nFileSystem\n{\nSearchPaths\n{\n\tGame csgo\n}\n}\n}\n'
    gameinfo.write_bytes(raw)
    executable=installation/'game/bin/win64/cs2.exe'
    executable.parent.mkdir(parents=True); executable.write_bytes(b'test executable, never run')
    (csgo/'steam.inf').write_text(f'appID=730\nPatchVersion={patch_version}\n')
    cfg = tmp_path / "steam/userdata/1/730/local/cfg"
    cfg.mkdir(parents=True)
    config = cfg / "cs2_machine_convars.vcfg"
    config.write_bytes(b"user settings")
    output = tmp_path / "output"
    output.mkdir()
    return TrialCheck(installation, hashlib.sha256(raw).hexdigest(), tmp_path / "demo.dem", output, 20_000_000_000, (config,)), raw


def test_real_candidate_trial_transaction_restores_config_and_game(tmp_path, monkeypatch):
    from cs2pov.services import hud_trial
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: True)
    check, original = check_fixture(tmp_path)
    session = tmp_path / "sessions/one"
    before_attrs = attributes(check.protected_configs[0])
    t = prepare_trial(check, session, candidate())
    t.deploy()
    assert (check.installation / "game/csgo/pov.vpk").read_bytes() == candidate().read_bytes()
    assert b"csgo/pov.vpk" in (check.installation / "game/csgo/gameinfo.gi").read_bytes()
    recovered = trial_transaction(check, session)
    recovered.load()
    assert all(result == "restored" for result in recovered.restore().values())
    assert (check.installation / "game/csgo/gameinfo.gi").read_bytes() == original
    assert not (check.installation / "game/csgo/pov.vpk").exists()
    assert check.protected_configs[0].read_bytes() == b"user settings"
    assert attributes(check.protected_configs[0]) == before_attrs


def test_changed_gameinfo_and_corrupt_candidate_block_before_backup(tmp_path):
    check, _ = check_fixture(tmp_path)
    session = tmp_path / "sessions/one"
    (check.installation / "game/csgo/gameinfo.gi").write_bytes(b"external update")
    with pytest.raises(RecoveryError):
        prepare_trial(check, session, candidate())
    assert not session.exists()


def test_space_lost_since_check_blocks_preparation(tmp_path, monkeypatch):
    from cs2pov.services import hud_trial
    from cs2pov.adapters.disk import DiskError
    check, _ = check_fixture(tmp_path)
    def full(_):
        raise DiskError("space lost")
    monkeypatch.setattr(hud_trial, "check_directory", full)
    session = tmp_path / "sessions/one"
    with pytest.raises(DiskError):
        prepare_trial(check, session, candidate())
    assert not session.exists()


@pytest.mark.parametrize('patch_version', ['1.41.8.8', '1.41.8.9'])
def test_session_telemetry_archive_uses_same_protected_transaction_and_restores(tmp_path,monkeypatch,patch_version):
    from cs2pov.services import hud_trial
    from cs2pov.services.telemetry_resource import build_session_resource
    monkeypatch.setattr(hud_trial,'cs2_closed',lambda:True)
    fixture,original=check_fixture(tmp_path,patch_version)
    fixture.demo.write_bytes(b'PBDEMS2\x00test demo header, never launched')
    resource=build_session_resource(candidate(),tmp_path/'session-pov.vpk')
    check=hud_trial.check_trial(fixture.installation,fixture.demo,fixture.output,
                               resource.path,fixture.protected_configs[0].parent,
                               telemetry=resource)
    assert check.patch_version == patch_version and check.telemetry == resource
    assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
    assert not (check.installation/'game/csgo/pov.vpk').exists()
    session=tmp_path/'sessions/telemetry'
    before_attrs=attributes(check.protected_configs[0])
    transaction=prepare_trial(check,session,resource.path)
    assert transaction.data['prepared']
    assert (session/'gameinfo.backup').read_bytes()==original
    assert (session/'config_0.backup').read_bytes()==b'user settings'
    assert not (check.installation/'game/csgo/pov.vpk').exists()
    assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
    transaction.deploy()
    assert (check.installation/'game/csgo/pov.vpk').read_bytes()==resource.path.read_bytes()
    recovered=trial_transaction(check,session); recovered.load()
    assert all(result=='restored' for result in recovered.restore().values())
    assert recovered.data['complete']
    assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
    assert not (check.installation/'game/csgo/pov.vpk').exists()
    assert check.protected_configs[0].read_bytes()==b'user settings'
    assert attributes(check.protected_configs[0])==before_attrs


def test_unknown_patch_diagnostic_names_detected_and_supported_versions_before_any_backup(tmp_path,monkeypatch):
    from dataclasses import replace
    from cs2pov.services import hud_trial
    from cs2pov.services.telemetry_resource import build_session_resource
    monkeypatch.setattr(hud_trial,'cs2_closed',lambda:True)
    check,original=check_fixture(tmp_path,'1.41.8.10')
    check.demo.write_bytes(b'PBDEMS2\x00test demo header, never launched')
    resource=build_session_resource(candidate(),tmp_path/'session-pov.vpk')
    session=tmp_path/'sessions/unknown-patch'
    for operation in (
        lambda: hud_trial.check_trial(check.installation,check.demo,check.output,resource.path,
                                     check.protected_configs[0].parent,telemetry=resource),
        lambda: prepare_trial(replace(check,patch_version='1.41.8.10',telemetry=resource),
                              session,resource.path),
    ):
        with pytest.raises(RecoveryError) as failure:
            operation()
        assert all(version in str(failure.value) for version in ('1.41.8.10','1.41.8.8','1.41.8.9'))
        assert not session.exists()
        assert (check.installation/'game/csgo/gameinfo.gi').read_bytes()==original
        assert not (check.installation/'game/csgo/pov.vpk').exists()
        assert check.protected_configs[0].read_bytes()==b'user settings'


def test_session_resource_changed_or_unverified_build_cannot_create_backup(tmp_path):
    from dataclasses import replace
    from cs2pov.services.telemetry_resource import build_session_resource
    check,_=check_fixture(tmp_path)
    resource=build_session_resource(candidate(),tmp_path/'session-pov.vpk')
    session=tmp_path/'sessions/telemetry'
    with pytest.raises(RecoveryError,match='版本'):
        prepare_trial(replace(check,patch_version='future',telemetry=resource),session,resource.path)
    assert not session.exists()
    resource.path.write_bytes(resource.path.read_bytes()+b'altered')
    with pytest.raises(ValueError,match='不一致'):
        prepare_trial(replace(check,patch_version='1.41.8.8',telemetry=resource),session,resource.path)
    assert not session.exists()


def test_game_update_between_check_and_preparation_blocks_telemetry(tmp_path):
    from dataclasses import replace
    from cs2pov.services.telemetry_resource import build_session_resource
    check,_=check_fixture(tmp_path)
    resource=build_session_resource(candidate(),tmp_path/'session-pov.vpk')
    check=replace(check,patch_version='1.41.8.8',telemetry=resource)
    (check.installation/'game/csgo/steam.inf').write_text('appID=730\nPatchVersion=1.41.8.9\n')
    session=tmp_path/'sessions/telemetry'
    with pytest.raises(RecoveryError,match='发生变化'): prepare_trial(check,session,resource.path)
    assert not session.exists()
