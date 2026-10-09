import json
from dataclasses import replace
import pytest

from cs2pov.adapters.map_overview import Overview
from cs2pov.services.radar import Sample, RadarTrack
from cs2pov.services.radar_payload import payload
from cs2pov.storage.settings import DataError


def example():
    owner = Sample('1', 2, True, 0, 0, 0, 0, frozenset())
    enemy = Sample('2', 3, True, 100, 100, 0, 90, frozenset({'1'}))
    hidden = replace(enemy, x=9999, y=9999, spotted_by=frozenset())
    return RadarTrack({0: {'1': owner, '2': hidden}, 8: {'1': owner, '2': enemy},
                       16: {'1': owner, '2': hidden},
                       24: {'1': replace(owner, alive=False), '2': hidden}})


OVERVIEW = Overview('de_mirage', 0, 1024, 1, 'a'*64, 'texture')


def test_export_contains_no_hidden_enemy_position_and_keeps_dead_invalidation():
    raw = payload(example(), OVERVIEW, '1', 0, 31, demo='C:/demo/test.dem')
    data = json.loads(raw)
    assert '9999' not in raw
    assert len(data['frames'][0][1]) == 1
    visible = data['frames'][1][1][1]
    ghost = data['frames'][2][1][1]
    assert visible[1] == 2 and ghost[1] == 3
    assert visible[2:] == ghost[2:]
    assert data['frames'][3] == [24, []]
    assert data['player'] == '1' and data['overview'] == 'a'*64


def test_subclip_has_start_floor_sample_and_no_frames_after_end():
    data = json.loads(payload(example(), OVERVIEW, '1', 9, 17, demo='C:/demo/test.dem'))
    assert [f[0] for f in data['frames']] == [8, 16]


@pytest.mark.parametrize('start,end', [(0, 0), (-1, 8), (0, 32), (True, 8), (0, 8.0)])
def test_invalid_or_incomplete_clip_is_not_silently_trimmed(start, end):
    with pytest.raises(DataError):
        payload(example(), OVERVIEW, '1', start, end, demo='C:/demo/test.dem')
