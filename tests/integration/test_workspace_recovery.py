from dataclasses import asdict, replace
import json
import pytest
from cs2pov.services.workspace import Workspace
from cs2pov.services.hud_trial import prepare_trial
from cs2pov.storage.settings import DataError
from test_hud_trial import candidate, check_fixture


def test_desktop_restart_recovers_trusted_targets_and_database(tmp_path, monkeypatch, qapp):
    from cs2pov.services import hud_trial
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: True)
    check, original = check_fixture(tmp_path)
    root = tmp_path / "data"
    model = Workspace(root)
    model.save_settings(replace(model.settings, installation=str(check.installation),
                                cfg=str(check.protected_configs[0].parent)))
    session = root / "sessions" / "trial"
    transaction = prepare_trial(check, session, candidate())
    context = asdict(check)
    context["protected_configs"] = list(map(str, check.protected_configs))
    (session / "trial-context.json").write_text(json.dumps(context, default=str))
    transaction.deploy()
    with model.library.db:
        model.library.db.execute("INSERT INTO sessions VALUES ('trial', 'running')")
    model.library.close()
    fresh = Workspace(root)
    assert fresh.recovery == [str(session)]
    assert "applied" in fresh.recovery_details(session)
    deployed = {path: path.read_bytes() for path in transaction.targets.values()}
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: False)
    with pytest.raises(DataError, match="CS2"):
        fresh.restore_session(session)
    assert all(path.read_bytes() == content for path, content in deployed.items())
    monkeypatch.setattr(hud_trial, "cs2_closed", lambda: True)
    context["protected_configs"] = [str(tmp_path / "untrusted.vcfg")]
    (session / "trial-context.json").write_text(json.dumps(context, default=str))
    with pytest.raises(DataError):
        fresh.restore_session(session)
    context["protected_configs"] = list(map(str, check.protected_configs))
    (session / "trial-context.json").write_text(json.dumps(context, default=str))
    assert all(state == "restored" for state in fresh.restore_session(session).values())
    assert fresh.recovery == [] and fresh.library.unfinished() == []
    assert (check.installation / "game/csgo/gameinfo.gi").read_bytes() == original
    assert check.protected_configs[0].read_bytes() == b"user settings"
    assert not (check.installation / "game/csgo/pov.vpk").exists()
    fresh.library.close()
