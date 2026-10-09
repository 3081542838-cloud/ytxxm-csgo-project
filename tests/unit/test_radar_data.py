from copy import deepcopy
import pytest
from cs2pov.services.radar import normalize, RadarTrack
from cs2pov.storage.settings import DataError


def row(tick, id, team, *, x=10, spotted=None, alive=True):
    return dict(tick=tick, steamid=id, team_num=team, **{'CCSPlayerController.m_iTeamNum': team},
                X=x, Y=20, Z=30, yaw=90, is_alive=alive,
                spotted=bool(spotted), approximate_spotted_by=spotted or [])


def records():
    result = []
    for tick in range(0, 112, 8):
        result += [row(tick, '1', 2), row(tick, '2', 2),
                   row(tick, '3', 3, x=100 if tick == 8 else 9999,
                       spotted=['2'] if tick == 8 else [])]
    return result


def marker(state, id):
    return next((m for m in state['markers'] if m['id'] == id), None)


def test_enemy_only_seen_by_current_team_then_frozen_and_expired():
    track = RadarTrack(normalize(records()))
    assert marker(track.state(0, '1'), '3') is None
    assert marker(track.state(8, '1'), '3')['kind'] == 'enemy'
    hidden = marker(track.state(16, '1'), '3')
    assert hidden['kind'] == 'last_known' and hidden['x'] == 100
    assert marker(track.state(104, '1'), '3')['x'] == 100
    assert marker(track.state(111, '1'), '3') is None


def test_paused_rewind_seek_player_switch_do_not_reuse_or_leak_future_intel():
    track = RadarTrack(normalize(records()))
    assert track.state(16, '1') == track.state(16, '1')
    assert marker(track.state(104, '1'), '3')['x'] == 100
    assert marker(track.state(0, '1'), '3') is None
    assert marker(track.state(16, '3'), '1') is None
    assert marker(track.state(16, '3'), '2') is None
    assert marker(track.state(16, '3'), '3')['kind'] == 'self'


def test_half_time_team_switch_clears_old_intel_and_follows_tick_team():
    data = records()
    for r in data:
        if r['tick'] >= 16:
            r['team_num'] = r['CCSPlayerController.m_iTeamNum'] = 5 - r['team_num']
    track = RadarTrack(normalize(data))
    assert track.state(16, '1')['team'] == 3
    assert marker(track.state(16, '1'), '3') is None


@pytest.mark.parametrize('change', [
    {'team_num': 3}, {'steamid': 1.5}, {'is_alive': None}, {'X': float('nan')},
    {'spotted': None}, {'spotted': False, 'approximate_spotted_by': ['2']},
    {'spotted': True, 'approximate_spotted_by': ['999']}, {'tick': True},
])
def test_missing_ambiguous_nonfinite_and_unresolved_identity_rejected(change):
    data = records(); data[0].update(change)
    with pytest.raises(DataError): normalize(data)


def test_duplicate_player_and_dead_pov_and_stale_time_rejected():
    data = records()
    with pytest.raises(DataError): normalize(data + [deepcopy(data[0])])
    data[0]['is_alive'] = False
    data[0]['X'] = None
    track = RadarTrack(normalize(data))
    with pytest.raises(DataError): track.state(0, '1')
    with pytest.raises(DataError): track.state(-1, '1')
    with pytest.raises(DataError): track.state(112, '1')


def test_unseen_enemy_death_never_updates_last_known_position():
    data = records()
    for r in data:
        if r['steamid'] == '3' and r['tick'] >= 16:
            r['is_alive'] = False
    track = RadarTrack(normalize(data))
    assert marker(track.state(16, '1'), '3')['x'] == 100


def test_uint64_player_ids_preserved_without_float_rounding():
    ids = ['76561199198478034', '76561199388663519', '76561198858239408']
    data = records()
    for r in data:
        r['steamid'] = int(ids[int(r['steamid']) - 1])
        r['approximate_spotted_by'] = [int(ids[int(s) - 1]) for s in r['approximate_spotted_by']]
    assert marker(RadarTrack(normalize(data)).state(8, ids[0]), ids[2])['kind'] == 'enemy'
