"""An already changed resource must revoke proof even when settings fail."""
import pytest

from tests.ui.test_environment_receipt_integration import (
    workspace_environment, issue_current_receipt, result_for,
)


def test_failed_settings_write_after_hud_registration_still_revokes_old_callback(
        workspace_environment, monkeypatch):
    fixture = workspace_environment
    model = fixture.model
    issue_current_receipt(fixture)
    model.check_environment()
    pending_index = len(fixture.coordinator.calls) - 1
    original_result = result_for(fixture, pending_index)
    original_save = model.settings_file.save
    failures = [True]

    def fail_once(raw):
        if failures.pop() if failures else False:
            raise OSError("settings disk write failed")
        return original_save(raw)

    monkeypatch.setattr(model.settings_file, "save", fail_once)
    replacement = fixture.tmp_path / "new-pov.vpk"
    replacement.write_bytes(b"replacement audited resource")
    with pytest.raises(OSError, match="settings disk write failed"):
        model.register_local_resource("hud", replacement)
    assert model.verified_local_resource("hud") == replacement
    assert not model.settings.output_verified
    fixture.coordinator.deliver(pending_index, original_result)
    assert not model.settings.output_verified
    assert model._environment_observation is None
