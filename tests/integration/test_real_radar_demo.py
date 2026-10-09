"""Mandatory read-only evidence for both user-supplied maps; not live acceptance."""
from pathlib import Path
import pytest

from cs2pov.adapters.demo import fingerprint
from cs2pov.adapters.map_overview import read_overview
from cs2pov.services.radar import FIELDS, normalize, RadarTrack
from cs2pov.services.radar_payload import payload, decode_payload


@pytest.mark.parametrize('path,map_name,sha256', [
    (Path.home()/'AppData/Roaming/Wmpvp/demo/9210181067658518284_0.dem', 'de_mirage',
     'c2a52a4b2d536053c64f369bc804fa19a3607f0d8484f9444606cce3485867b4'),
    (Path('local-validation/radar-demo/9216020002435578892_0.dem').absolute(), 'de_ancient',
     '17029363ca2d21794fa8b2c05fb820e6cc51f470e9715e29f97d62a5b79502b6'),
])
def test_real_two_maps_have_valid_team_visibility_and_native_overviews_without_modification(path, map_name, sha256):
    from demoparser2 import DemoParser
    before = fingerprint(path)
    assert before['sha256'] == sha256
    parser = DemoParser(str(path))
    assert parser.parse_header()['map_name'] == map_name
    records = parser.parse_ticks(FIELDS,ticks=list(range(0,150000,8))).to_dict('records')
    frames = normalize(records)
    track = RadarTrack(frames)
    overview = read_overview(Path(r'D:/steam/steamapps/common/Counter-Strike Global Offensive'), map_name)
    seen_teams, unseen_teams = set(), set()
    for tick, frame in frames.items():
        for enemy in frame.values():
            for observer in enemy.spotted_by:
                owner = frame[observer]
                if not owner.alive or owner.team == enemy.team or not enemy.alive:
                    continue
                markers = track.state(tick, observer)['markers']
                assert any(m['id'] == enemy.id and m['kind'] == 'enemy' for m in markers)
                seen_teams.add(owner.team)
            if not enemy.spotted_by and enemy.alive:
                for owner in frame.values():
                    if owner.alive and owner.team != enemy.team:
                        assert not any(m['id']==enemy.id and m['kind']=='enemy' for m in track.state(tick,owner.id)['markers'])
                        unseen_teams.add(owner.team)
                        break
        if seen_teams == unseen_teams == {2,3}:
            alive = next(s for s in frame.values() if s.alive)
            data = decode_payload(payload(track,overview,alive.id,tick,tick+7,demo=path))
            assert data['map'] == map_name and data['overview'] == overview.sha256
            break
    assert seen_teams == unseen_teams == {2,3}
    assert fingerprint(path) == before
