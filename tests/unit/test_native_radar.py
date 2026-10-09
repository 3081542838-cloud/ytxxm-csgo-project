"""Native spectator radar, explicitly accepted instead of POV intelligence."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import pytest

from cs2pov.storage.settings import HudPreset, Presets, DataError, DataVersionError
from cs2pov.adapters.vpk import validate_resource
from cs2pov.services.telemetry_resource import (BASE_SHA256, PATHS, SCRIPT_PATH,
    build_session_resource, verify_session_resource)
from cs2pov.services.replay import preview_commands

BASE = Path('resources/private/minimal-pov/pov.vpk')


def test_old_hud_migrates_hidden_and_keeps_original_backup(tmp_path):
    old = asdict(HudPreset(hud_scale=.75, name='我的 HUD'))
    old.pop('show_radar', None)
    raw = dict(schema=1, default='builtin', items=[old])
    path = tmp_path/'hud-presets.json'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    before = path.read_bytes()
    presets = Presets(tmp_path)
    assert presets.items[0].show_radar is False
    assert presets.items[0].hud_scale == .75
    assert path.read_bytes() == before  # Opening does not rewrite old data.
    presets.save(replace(presets.items[0], show_radar=True))
    assert path.with_suffix('.json.bak').read_bytes() == before
    assert json.loads(path.read_text(encoding='utf-8'))['schema'] == 2
    assert Presets(tmp_path).items[0].show_radar is True


@pytest.mark.parametrize('value', [1, 0, None, 'true', [], {}])
def test_radar_flag_is_strict_boolean(value):
    raw = asdict(HudPreset()); raw['show_radar'] = value
    with pytest.raises(DataError): HudPreset.decode(raw)


def test_newer_hud_schema_never_overwritten(tmp_path):
    path = tmp_path/'hud-presets.json'
    path.write_text(json.dumps(dict(schema=3, default='builtin', items=[asdict(HudPreset())])))
    before = path.read_bytes()
    with pytest.raises(DataVersionError): Presets(tmp_path)
    assert path.read_bytes() == before


def test_current_schema_missing_radar_is_corrupt_not_legacy():
    raw = asdict(HudPreset()); raw.pop('show_radar')
    with pytest.raises(DataError): Presets.decode(dict(schema=2, default='builtin', items=[raw]))


def test_copy_default_and_snapshot_are_independent(tmp_path):
    presets = Presets(tmp_path)
    clone = presets.copy('builtin')
    presets.save(replace(clone, show_radar=True)); presets.set_default(clone.id)
    snapshot = asdict(next(p for p in presets.items if p.id == presets.default))
    presets.save(replace(clone, show_radar=False))
    assert snapshot['show_radar'] is True
    assert Presets(tmp_path).items[-1].show_radar is False


def test_native_variant_preserves_archive_and_authenticates_display_mode(tmp_path):
    result = build_session_resource(BASE, tmp_path/'native.vpk', native_radar=True)
    original = {e.path:e.data for e in validate_resource(BASE, BASE_SHA256, set(PATHS)).entries}
    derived = {e.path:e.data for e in validate_resource(result.path, result.sha256, set(PATHS)).entries}
    assert result.path.stat().st_size == BASE.stat().st_size
    assert set(derived) == set(original)
    for path in PATHS - {SCRIPT_PATH}: assert original[path] == derived[path]
    script = derived[SCRIPT_PATH]
    assert b'"HudRadar", "CSGOHudRadar", ' not in script
    assert b'"HudSpecplayer"' in script and b'"HudDemoController"' in script
    assert b'XiamiPOVRadar' not in script and b'__CS2POV_RADAR_DATA__' not in script
    assert verify_session_resource(result, result.path) == result.sha256
    with pytest.raises(DataError): verify_session_resource(replace(result, native_radar=False), result.path)


@pytest.mark.parametrize('value', [1, None, 'true'])
def test_native_resource_flag_cannot_be_forged(tmp_path, value):
    with pytest.raises(DataError): build_session_resource(BASE, tmp_path/'bad.vpk', native_radar=value)
    assert not (tmp_path/'bad.vpk').exists()


@pytest.mark.parametrize('show', [False, True])
def test_commands_match_frozen_radar_choice(show):
    hud = asdict(HudPreset(show_radar=show))
    draft = dict(fingerprint={'sha256':'a'*64}, selection=dict(content_sha256='a'*64,
        hud=hud, player_id='1', start_tick=64, server_end_tick=128))
    commands = preview_commands({'players':[{'id':'1','name':'Player'}]}, draft)
    if show:
        assert commands.count('cl_drawhud_force_radar 1') == 1
        assert commands.index('cl_radar_square_when_spectating 1') < commands.index('cl_drawhud_force_radar 1')
        assert 'cl_drawhud_force_radar -1' not in commands
    else:
        assert 'cl_drawhud_force_radar -1' in commands
        assert not any(c.startswith('cl_radar_square') for c in commands)
    assert 'spec_show_xray 0' in commands
    assert 'demo_ui_mode 0' in commands
    assert draft['selection']['hud'] == hud
