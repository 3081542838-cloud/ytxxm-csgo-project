"""Exact failed-session bytes prove a paused seek is not a server-clock proof."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogReader
from cs2pov.services.replay import ReplayController, ReplayError


def replay_fixture(tmp_path, *, nonce=None, source_age=.25):
    fixture = Path('tests/fixtures/replay-preroll-paused-seek-20261004.log')
    raw = fixture.read_bytes()
    metadata = json.loads(fixture.with_suffix('.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == metadata['fixture_sha256']
    assert len(raw) == metadata['fixture_bytes'] == 1396
    lines = raw.splitlines(keepends=True)
    assert len(lines) == len(metadata['records']) == 6
    assert [r['source_line'] for r in metadata['records']] == [936, 937, 939, 940, 941, 942]
    identity = ProcessIdentity(**metadata['process_identity'])
    stamp = lambda line: datetime.strptime('2026/' + line.decode('utf-8')[:14], '%Y/%m/%d %H:%M:%S').timestamp()
    origin = stamp(lines[0])
    timer = SimpleNamespace(wall=origin + source_age, mono=100. + source_age)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    reader = ReplayLogReader(tmp_path, identity, nonce or metadata['nonce'],
                             clock=lambda: timer.mono, wall=lambda: timer.wall)
    for line in lines:
        timer.wall = stamp(line) + source_age
        timer.mono = 100. + stamp(line) - origin + source_age
        with log.open('ab') as stream:
            stream.write(line)
        reader.readback()
    return reader, metadata, identity, timer


def test_actual_paused_seek_has_snapshot_without_inventing_server_clock(tmp_path):
    reader, metadata, identity, timer = replay_fixture(tmp_path)
    snapshot = reader.snapshot()
    assert snapshot is not None
    assert snapshot.process_identity == identity and snapshot.local_demo and snapshot.paused
    assert snapshot.tick == metadata['expected_preroll_tick'] == 10286
    assert snapshot.player_id == '76561199153246852' and snapshot.first_person
    assert timer.mono - snapshot.observed_at == pytest.approx(.25)
    assert reader.readback() is None
    assert reader.decoder.pause_tick is None and reader.decoder.anchor is None
    assert not hasattr(snapshot, 'server_tick')


@pytest.mark.parametrize('change', ['wrong_nonce', 'stale_source'])
def test_actual_seek_with_wrong_session_or_expired_source_is_no_evidence(tmp_path, change):
    reader, _, _, _ = replay_fixture(tmp_path,
        nonce='f' * 32 if change == 'wrong_nonce' else None,
        source_age=5.01 if change == 'stale_source' else .25)
    assert reader.readback() is None and reader.snapshot() is None


def test_actual_preroll_snapshot_cannot_authorize_exact_start_or_endpoint(tmp_path):
    reader, metadata, identity, timer = replay_fixture(tmp_path)
    controller = ReplayController(
        SimpleNamespace(verify=lambda: identity, argv=['cs2.exe', '-insecure']),
        SimpleNamespace(readback=reader.readback, foreground_pid=lambda: identity.pid),
        clock=lambda: timer.mono)
    draft = dict(demo=r'C:\Users\测试员\AppData\Roaming\Wmpvp\demo\9210181067658518284_0.dem',
        selection=dict(start_tick=metadata['target_start_tick'],
            server_start_tick=metadata['target_server_start_tick'],
            end_tick=10478, server_end_tick=13810, player_id='76561199198478034'))
    for at_end in (False, True):
        with pytest.raises(ReplayError):
            controller.verify_result(draft, since=timer.mono - 1, at_end=at_end)
        assert controller.state == 'unverified'
    assert reader.snapshot().tick == 10286 and reader.readback() is None
