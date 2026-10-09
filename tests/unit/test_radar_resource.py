from dataclasses import replace
import json
from pathlib import Path
import struct
import pytest

from cs2pov.adapters.compiled_script import replace_script_slot
from cs2pov.adapters.vpk import validate_resource
from cs2pov.services.telemetry_resource import BASE_SHA256, PATHS, SCRIPT_PATH, SLOT_LENGTH, build_session_resource, verify_session_resource
from cs2pov.services.radar_payload import decode_payload
from cs2pov.storage.settings import DataError

BASE = Path('resources/private/minimal-pov/pov.vpk')


def sample(count=2):
    return json.dumps(dict(schema=1, player='1', map='de_mirage', overview='a'*64,demo='C:/demo/test.dem',
        stride=8, start=0, end=count*8-1,
        frames=[[tick*8, [['1', 0, 50.125, 40.5, 0, 90]]] for tick in range(count)]))


def blocks(raw):
    result = {}
    for index in range(3):
        at = 16+12*index
        start, length = struct.unpack_from('<II', raw, at+4)
        result[raw[at:at+4]] = raw[at+4+start:at+4+start+length]
    return result


@pytest.mark.parametrize('count', [2, 12000])
def test_radar_resource_reconstructs_exactly_and_only_changes_script_data(tmp_path, count):
    before = {e.path:e.data for e in validate_resource(BASE, BASE_SHA256, set(PATHS)).entries}
    result = build_session_resource(BASE, tmp_path/'session.vpk', nonce='RADAR_SESSION_0001', radar=sample(count))
    after = {e.path:e.data for e in validate_resource(result.path, result.sha256, set(PATHS)).entries}
    assert verify_session_resource(result, result.path) == result.sha256
    for path in PATHS - {SCRIPT_PATH}:
        assert before[path] == after[path]
    old, new = blocks(before[SCRIPT_PATH]), blocks(after[SCRIPT_PATH])
    assert old[b'RED2'] == new[b'RED2'] and old[b'STAT'] == new[b'STAT']
    assert b'POV_RADAR_SETTINGS' in new[b'DATA']
    assert b'__CS2POV_RADAR_DATA__' not in new[b'DATA']
    assert b'"HudRadar", "CSGOHudRadar", "HudDemoController"' not in new[b'DATA']
    assert b'GameInterfaceAPI.SetSetting' not in new[b'DATA']
    if count == 12000:
        assert len(after[SCRIPT_PATH]) > len(before[SCRIPT_PATH])
    with pytest.raises(DataError):
        verify_session_resource(replace(result, radar=sample(3)), result.path)
    assert validate_resource(BASE, BASE_SHA256, set(PATHS))


def test_changed_compiled_header_or_slot_boundary_rejected():
    raw = next(e.data for e in validate_resource(BASE, BASE_SHA256, set(PATHS)).entries if e.path == SCRIPT_PATH)
    original = Path('src/cs2pov/resources/pov_visibility.js').read_bytes().replace(b'\r\n',b'\n').ljust(SLOT_LENGTH,b' ')
    bad = bytearray(raw)
    struct.pack_into('<I', bad, 0, len(raw)+1)
    with pytest.raises(DataError): replace_script_slot(bytes(bad), original, b'new')
    with pytest.raises(DataError): replace_script_slot(raw, original[:-1], b'new')


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(schema=True),
    lambda d: d.update(player=1.0),
    lambda d: d.update(map='de_mirage";quit'),
    lambda d: d['frames'][0][1][0].__setitem__(2,float('nan')),
    lambda d: d['frames'][0][1].append(['2',0,0,0,0,0]),
    lambda d: d['frames'][0][1].append(d['frames'][0][1][0]),
    lambda d: d['frames'][0][1][0].__setitem__(1,True),
    lambda d: d['frames'][0].__setitem__(0,8),
])
def test_malformed_embedded_data_does_not_create_resource(tmp_path, mutate):
    data = json.loads(sample())
    mutate(data)
    with pytest.raises(DataError):
        build_session_resource(BASE,tmp_path/'bad.vpk',radar=json.dumps(data))
    assert not (tmp_path/'bad.vpk').exists()


def test_duplicate_payload_fields_are_not_accepted():
    with pytest.raises(DataError): decode_payload(sample().replace('"schema": 1', '"schema": 1, "schema": 1'))
