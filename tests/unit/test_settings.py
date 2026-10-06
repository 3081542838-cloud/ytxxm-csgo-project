from dataclasses import asdict, replace
import json
import math
import pytest
from cs2pov.storage.settings import (Settings, JsonFile, HudPreset, Presets,
                                     DataError, DataVersionError)


def test_settings_directory_change_clears_both_confirmation_levels(tmp_path):
    current = Settings(video_directory=str(tmp_path), nvidia_path_confirmed=True, output_verified=True)
    assert current.updated(hotkey="Ctrl+F8").output_verified
    changed = current.updated(video_directory=str(tmp_path / "new"))
    assert not changed.output_verified and not changed.nvidia_path_confirmed
    assert not current.updated(nvidia_path_confirmed=False).output_verified
    assert not current.updated(installation=str(tmp_path / "game")).output_verified


def test_settings_fallback_preserves_corrupt_primary_and_survives_reopen(tmp_path):
    file = JsonFile(tmp_path / "settings.json", Settings.decode)
    file.save(asdict(Settings(hotkey="Ctrl+F8")))
    file.save(asdict(Settings(hotkey="Alt+F9")))
    file.path.write_text("{broken", encoding="utf-8")
    recovered = file.load(Settings())
    assert recovered.hotkey == "Ctrl+F8"
    assert "有效副本" in file.warning
    assert file.path.read_text() == "{broken"
    file.save(asdict(recovered))
    assert list(tmp_path.glob("settings.json.corrupt-*"))[0].read_text() == "{broken"
    assert JsonFile(file.path, Settings.decode).load(Settings()) == recovered


def test_failed_atomic_save_leaves_existing_settings_valid(tmp_path, monkeypatch):
    from cs2pov.storage import settings
    file = JsonFile(tmp_path / "settings.json", Settings.decode)
    file.save(asdict(Settings()))
    before = file.path.read_bytes()
    real = settings.atomic_write
    def fail_primary(path, data):
        if path == file.path:
            raise OSError("simulated disk full")
        real(path, data)
    monkeypatch.setattr(settings, "atomic_write", fail_primary)
    with pytest.raises(OSError):
        file.save(asdict(Settings(hotkey="Ctrl+F8")))
    assert file.path.read_bytes() == before
    assert file.load(Settings()) == Settings()


def test_newer_settings_version_does_not_silently_load_old_backup(tmp_path):
    file = JsonFile(tmp_path / "settings.json", Settings.decode)
    file.save(asdict(Settings()))
    raw = asdict(Settings()); raw["schema"] = 3
    file.path.write_text(json.dumps(raw))
    with pytest.raises(DataVersionError):
        file.load(Settings())
    assert json.loads(file.path.read_text())["schema"] == 3
    before = file.path.read_bytes()
    with pytest.raises(DataVersionError):
        file.save(asdict(Settings()))
    assert file.path.read_bytes() == before
    assert not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("changes", [
    {"video_directory": "relative"}, {"video_directory": r"\\server\share"},
    {"hotkey": "Alt+Alt+F9"}, {"hotkey": "Alt+F9;quit"}, {"output_verified": True},
    {"nvidia_path_confirmed": 1}, {"schema": True}, {"console_key": "F6"},
    {"console_key": "Slash;quit"},
])
def test_invalid_settings_fail_before_write(changes):
    raw = asdict(Settings()); raw.update(changes)
    with pytest.raises(DataError):
        Settings.decode(raw)


def test_old_settings_upgrade_preserves_choices_and_backup(tmp_path):
    file = JsonFile(tmp_path / "settings.json", Settings.decode)
    old = asdict(Settings(video_directory=str(tmp_path), hotkey="Ctrl+F8"))
    old.pop("console_key")
    old["schema"] = 1
    file.path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    upgraded = file.load(Settings())
    assert upgraded.schema == 2 and upgraded.console_key == "Backtick"
    assert upgraded.hotkey == "Ctrl+F8" and upgraded.video_directory == str(tmp_path)
    file.save(asdict(upgraded.updated(console_key="Slash")))
    assert json.loads(file.backup.read_text(encoding="utf-8")) == old
    assert JsonFile(file.path, Settings.decode).load(Settings()).console_key == "Slash"


@pytest.mark.parametrize("changes", [{"hud_scale": math.nan}, {"viewmodel_x": 3},
    {"crosshair": "quit;exec bad"}, {"viewmodel_fov": True}, {"name": ""}])
def test_hud_ranges_and_codes_reject_unsafe_or_invalid_values(changes):
    raw = asdict(HudPreset()); raw.update(changes)
    with pytest.raises(DataError):
        HudPreset.decode(raw)


def test_hud_copy_default_save_restart_and_builtin_protection(tmp_path):
    presets = Presets(tmp_path)
    clone = presets.copy("builtin")
    presets.save(replace(clone, hud_scale=.8, viewmodel_x=-1,
                         crosshair="CSGO-abc12-def34-ghi56-jkl78-mno90"))
    presets.set_default(clone.id)
    reloaded = Presets(tmp_path)
    assert reloaded.default == clone.id
    assert next(p for p in reloaded.items if p.id == clone.id).hud_scale == .8
    with pytest.raises(DataError):
        reloaded.remove("builtin")
    reloaded.remove(clone.id)
    assert Presets(tmp_path).default == "builtin"


def test_bad_settings_and_bad_copy_block_instead_of_reset(tmp_path):
    file = JsonFile(tmp_path / "settings.json", Settings.decode)
    file.path.write_text("bad"); file.backup.write_text("bad too")
    with pytest.raises(DataError):
        file.load(Settings())
    assert file.path.read_text() == "bad"
