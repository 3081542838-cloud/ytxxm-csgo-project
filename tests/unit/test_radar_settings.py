import pytest

from cs2pov.services.radar_settings import RadarSettings, SETTING_NAMES
from cs2pov.storage.settings import DataError


def test_game_setting_snapshot_preserves_values_without_defaults_or_set_commands():
    raw = {'schema': 1, 'values': [.42, 0, 1, 1.25, .7, 1, 0]}
    settings = RadarSettings.decode(raw)
    raw['values'][0] = .9
    assert settings.as_settings() == dict(zip(SETTING_NAMES, [.42, 0, 1, 1.25, .7, 1, 0]))
    exported = settings.snapshot()
    exported['values'][1] = 1
    assert settings.values[1] == 0
    assert RadarSettings.decode(settings.snapshot()) == settings


@pytest.mark.parametrize('raw', [
    {}, {'schema': 2, 'values': [.42, 0, 1, 1, .7, 1, 0]},
    {'schema': True, 'values': [.42, 0, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': []},
    {'schema': 1, 'values': [.42, 2, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': [0, 0, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': [float('nan'), 0, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': [True, 0, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': ['.42', 0, 1, 1, .7, 1, 0]},
    {'schema': 1, 'values': [.42, 0, 1, 1, .7, 1, 0], 'defaults': True},
])
def test_missing_unknown_or_malformed_settings_block_instead_of_guessing(raw):
    with pytest.raises(DataError):
        RadarSettings.decode(raw)
