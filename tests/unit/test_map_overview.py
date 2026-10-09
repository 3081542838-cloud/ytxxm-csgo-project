import struct
from pathlib import Path
import pytest
from cs2pov.adapters.map_overview import parse_overview, read_overview
from cs2pov.adapters.vpk import build_vpk
from cs2pov.storage.settings import DataError


TEXT = b'"de_mirage" { "pos_x" "-3230" "pos_y" "1713" "scale" "5" }'
IMAGE = 'panorama/images/overheadmaps/de_mirage_radar_psd.vtex_c'


def test_world_transform_uses_installed_map_scale_and_y_inversion():
    config = parse_overview('de_mirage', TEXT, IMAGE)
    assert config.percent(-3230, 1713) == (0, 0)
    assert config.percent(1890, -3407) == (100, 100)
    assert config.texture == 's2r://panorama/images/overheadmaps/de_mirage_radar_psd.vtex'


@pytest.mark.parametrize('text', [b'"scale" "5"', TEXT + b' "scale" "4"',
                                  TEXT.replace(b'"5"', b'"nan"'), TEXT.replace(b'"5"', b'"0"')])
def test_bad_coordinate_metadata_is_rejected(text):
    with pytest.raises(DataError): parse_overview('de_mirage', text, IMAGE)


def index(tmp_path):
    root = tmp_path / 'game/csgo'; root.mkdir(parents=True)
    path = root / 'pak01_dir.vpk'
    path.write_bytes(build_vpk({'resource/overviews/de_mirage.txt': TEXT, IMAGE: b'installed texture'}))
    return path


def test_bounded_read_does_not_copy_or_modify_game_archives(tmp_path):
    path = index(tmp_path); before = path.read_bytes()
    assert read_overview(tmp_path, 'de_mirage').percent(-3230, 1713) == (0, 0)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ['pak01_dir.vpk']


def test_crc_truncation_and_path_injection_rejected(tmp_path):
    path = index(tmp_path)
    raw = path.read_bytes(); needle = raw.index(TEXT)
    changed = bytearray(raw); changed[needle] ^= 1; path.write_bytes(changed)
    with pytest.raises(DataError): read_overview(tmp_path, 'de_mirage')
    path.write_bytes(raw[:15])
    with pytest.raises(DataError): read_overview(tmp_path, 'de_mirage')
    with pytest.raises(DataError): read_overview(tmp_path, '../../Windows')
