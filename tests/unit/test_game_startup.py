from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.game_startup import GameStartupEvidence, StartupLogReader
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.storage.settings import DataError


FIXTURES = Path(__file__).parents[1]/'fixtures'
READY = FIXTURES/'pipe-startup-ready-20261005.log'
INCOMPLETE = FIXTURES/'pipe-startup-incomplete-20261005.log'
PATCH_READY = FIXTURES/'patch-startup-20261006.log'
SOURCE_WALL = datetime(2026, 10, 5, 1, 39, 9).timestamp()
FILETIME_EPOCH = 11644473600


@pytest.fixture
def setup(tmp_path):
    clock, wall = [10.0], [SOURCE_WALL]
    path = tmp_path/'engine.log'
    path.write_bytes(b'')
    identity = ProcessIdentity(123, int((SOURCE_WALL-14+FILETIME_EPOCH)*10_000_000),
                               str(tmp_path/'game/bin/win64/cs2.exe'))
    current = [identity]
    game = SimpleNamespace(verify=lambda: current[0], argv=[identity.executable, '-insecure',
        '-condebug', '-con_logfile', str(path)])
    reader = StartupLogReader(tmp_path, game, '1.41.8.8', clock=lambda: clock[0], wall=lambda: wall[0])
    return reader, path, game, current, clock, wall


def append(path, raw):
    with path.open('ab') as stream:
        stream.write(raw)


def test_real_ready_excerpt_is_ordered_current_startup_qualification_not_replay_evidence(setup):
    reader, path, _, current, *_ = setup
    append(path, READY.read_bytes())
    proof = reader.poll()
    assert type(proof) is GameStartupEvidence
    assert proof.process_identity == current[0] and proof.observed_at == 10.0
    assert not hasattr(proof, 'tick') and not hasattr(proof, 'player_id')
    assert reader.poll() == proof


def test_worldwide_launch_accepts_real_startup_but_region_change_revokes_identity(setup):
    _, path, game, current, clock, wall = setup
    game.argv.insert(2, '-worldwide')
    reader = StartupLogReader(path.parent, game, '1.41.8.8',
                              clock=lambda: clock[0], wall=lambda: wall[0])
    append(path, READY.read_bytes())

    proof = reader.poll()
    assert type(proof) is GameStartupEvidence
    assert proof.process_identity == current[0] and proof.observed_at == 10.0
    assert reader.state == 'ready' and reader.argv == tuple(game.argv)

    game.argv[2] = '-perfectworld'
    with pytest.raises(DataError, match='参数发生变化'):
        reader.poll()
    assert reader.state == 'failed' and reader.proof is None

    # Returning the argument to its original value cannot revive authority.
    game.argv[2] = '-worldwide'
    with pytest.raises(DataError, match='已失效'):
        reader.poll()


def test_real_incomplete_first_attempt_cannot_be_ready_or_refresh_original_deadline(setup):
    _, path, game, current, clock, _ = setup
    first_wall = datetime(2026,10,5,1,6,15).timestamp()
    current[0] = replace(current[0], created=int((first_wall-5+FILETIME_EPOCH)*10_000_000))
    reader = StartupLogReader(path.parent, game, '1.41.8.8', clock=lambda:clock[0], wall=lambda:first_wall)
    append(path, INCOMPLETE.read_bytes())
    assert reader.poll() is None
    clock[0] = 130.0
    with pytest.raises(DataError):
        reader.poll()


def test_partial_current_startup_waits_without_using_host_or_server_loop_markers(setup):
    reader, path, *_ = setup
    lines = READY.read_bytes().splitlines(keepends=True)
    append(path, b''.join(lines[:-1]))
    assert reader.poll() is None
    append(path, lines[-1])
    assert type(reader.poll()) is GameStartupEvidence


def test_split_final_line_waits_for_complete_utf8_line(setup):
    reader, path, *_ = setup
    raw = READY.read_bytes()
    append(path, raw[:-1])
    assert reader.poll() is None
    append(path, raw[-1:])
    assert type(reader.poll()) is GameStartupEvidence


@pytest.mark.parametrize('patch', [None, '', '1.41.8.7', '1.41.8.10', True, [], {}])
def test_unverified_patch_is_blocked_before_reading_or_accepting_startup(tmp_path, patch):
    path = tmp_path/'engine.log'; path.write_bytes(READY.read_bytes())
    identity = ProcessIdentity(123, 456, str(tmp_path/'cs2.exe'))
    game = SimpleNamespace(verify=lambda: identity, argv=[identity.executable, '-insecure', '-con_logfile', str(path)])
    with pytest.raises(DataError) as failure:
        StartupLogReader(tmp_path, game, patch)
    if patch == '1.41.8.10':
        assert all(version in str(failure.value) for version in ('1.41.8.10','1.41.8.8','1.41.8.9'))


def test_real_patch_startup_markers_authorize_only_startup_not_demo_or_player(tmp_path):
    wall=datetime(2026,10,6,14,56,24).timestamp()
    path=tmp_path/'engine.log'; path.write_bytes(b'')
    identity=ProcessIdentity(123,int((wall-96+FILETIME_EPOCH)*10_000_000),
                             str(tmp_path/'game/bin/win64/cs2.exe'))
    game=SimpleNamespace(verify=lambda:identity,argv=[identity.executable,'-insecure',
                         '-con_logfile',str(path)])
    reader=StartupLogReader(tmp_path,game,'1.41.8.9',clock=lambda:100.0,wall=lambda:wall)
    append(path,PATCH_READY.read_bytes())
    proof=reader.poll()
    assert type(proof) is GameStartupEvidence and proof.process_identity==identity
    assert reader.stage==4 and reader.state=='ready' and proof.observed_at==100.0
    assert not any(hasattr(proof,field) for field in ('tick','server_tick','player_id','first_person','paused'))


def test_supported_patch_metadata_without_actual_markers_cannot_be_ready(tmp_path):
    wall=datetime(2026,10,6,14,56,24).timestamp()
    path=tmp_path/'engine.log'; path.write_bytes(b'')
    identity=ProcessIdentity(123,int((wall-96+FILETIME_EPOCH)*10_000_000),
                             str(tmp_path/'game/bin/win64/cs2.exe'))
    game=SimpleNamespace(verify=lambda:identity,argv=[identity.executable,'-insecure',
                         '-con_logfile',str(path)])
    reader=StartupLogReader(tmp_path,game,'1.41.8.9',clock=lambda:100.0,wall=lambda:wall)
    assert reader.poll() is None and reader.state=='waiting' and reader.stage==0


@pytest.mark.parametrize('argv', [None, '-insecure', [], ['cs2.exe'],
    ['cs2.exe', '-insecure', '-con_logfile'],
    ['cs2.exe', '-insecure', '-con_logfile', 'C:/another/engine.log'],
    ['cs2.exe', '-insecure', '-con_logfile', 'engine.log'],
    ['cs2.exe', '-insecure', '-con_logfile', 'x', '-con_logfile', 'y']])
def test_unknown_or_wrong_launch_logfile_binding_cannot_accept_any_marker(setup, argv):
    reader, path, game, *_ = setup
    game.argv = argv
    append(path, READY.read_bytes())
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('change', [{'pid':124}, {'created':457}, {'executable':'C:/unknown.exe'}])
def test_full_owned_game_identity_is_frozen_and_change_revokes_startup(setup, change):
    reader, path, _, current, *_ = setup
    append(path, READY.read_bytes())
    current[0] = replace(current[0], **change)
    with pytest.raises(DataError):
        reader.poll()


def test_unknown_identity_cannot_construct_reader(tmp_path):
    game = SimpleNamespace(verify=lambda: None, argv=['cs2.exe', '-insecure'])
    with pytest.raises(DataError):
        StartupLogReader(tmp_path, game, '1.41.8.8')


def test_identity_changed_during_real_log_read_cannot_become_ready(setup):
    reader, path, _, current, *_ = setup
    append(path, READY.read_bytes())
    original = reader.log.read_after
    def changed(cursor):
        result = original(cursor)
        current[0] = replace(current[0], created=current[0].created+1)
        return result
    reader.log.read_after = changed
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('kind', ['replace', 'truncate', 'invalid_utf8'])
def test_log_replacement_truncation_or_bad_encoding_is_terminal(setup, kind):
    reader, path, *_ = setup
    first = READY.read_bytes().splitlines(keepends=True)[0]
    append(path, first)
    assert reader.poll() is None
    if kind == 'replace':
        path.rename(path.with_suffix('.previous'))
        path.write_bytes(READY.read_bytes())
    elif kind == 'truncate':
        path.write_bytes(b'x')
    else:
        append(path, b'\xff\n')
    with pytest.raises(DataError):
        reader.poll()
    with pytest.raises(DataError):
        reader.poll()


def test_oversized_startup_log_is_rejected_before_unbounded_read(setup):
    reader, path, *_ = setup
    append(path, b'x'*(512*1024+1))
    reader.log.read_after = lambda _: pytest.fail('oversized file was read')
    with pytest.raises(DataError):
        reader.poll()


def test_log_growth_between_size_guard_and_console_cursor_read_cannot_authorize_startup(setup):
    reader, path, *_ = setup
    append(path, READY.read_bytes())
    original = reader.log.read_after
    def grew(cursor):
        append(path, b'unrelated\n'*(512*1024//10+1))
        return original(cursor)
    reader.log.read_after = grew
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('kind', ['future_final', 'reversed_source', 'before_launch', 'reversed_duration'])
def test_invalid_actual_startup_source_time_or_duration_never_authorizes(setup, kind):
    reader, path, _, _, _, wall = setup
    raw = READY.read_bytes()
    if kind == 'future_final':
        raw = raw.replace(b'01:39:09 [Client] CL:  }', b'01:39:10 [Client] CL:  }')
    elif kind == 'reversed_source':
        raw = raw.replace(b'01:39:07 [STARTUP]', b'01:38:59 [STARTUP]')
    elif kind == 'before_launch':
        raw = raw.replace(b'01:39:00 [STARTUP]', b'01:38:54 [STARTUP]')
    else:
        raw = raw.replace(b'{12.182}', b'{4.000}')
    append(path, raw)
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('index', range(4))
def test_wrong_tag_or_echo_cannot_substitute_each_actual_required_marker(setup, index):
    reader, path, *_ = setup
    lines = READY.read_bytes().splitlines(keepends=True)
    required = [0,1,2,5]
    line = lines[required[index]]
    stamp, body = line[:15], line[15:]
    lines[required[index]] = stamp+b'[Console] echo '+body
    append(path, b''.join(lines))
    try:
        proof = reader.poll()
    except DataError:
        proof = None
    assert proof is None


@pytest.mark.parametrize('order', [(1,0,2,3), (0,2,1,3), (0,1,3,2), (3,0,1,2)])
def test_actual_required_markers_in_wrong_order_terminally_block(setup, order):
    reader, path, *_ = setup
    lines = READY.read_bytes().splitlines(keepends=True)
    required = [lines[i] for i in (0,1,2,5)]
    append(path, b''.join(required[i] for i in order))
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('age', [5.0, 5.001])
def test_only_actual_final_marker_supplies_fresh_bootstrap_qualification(setup, age):
    reader, path, _, _, clock, wall = setup
    append(path, READY.read_bytes())
    clock[0] += age; wall[0] += age
    if age == 5.0:
        proof = reader.poll()
        assert proof.observed_at == 10.0
    else:
        with pytest.raises(DataError):
            reader.poll()


def test_unrelated_later_log_output_cannot_refresh_ready_source_time(setup):
    reader, path, _, _, clock, wall = setup
    append(path, READY.read_bytes())
    assert reader.poll()
    clock[0] += 6; wall[0] += 6
    append(path, b'10/05 01:39:15 [RenderSystem] irrelevant heartbeat\n')
    with pytest.raises(DataError):
        reader.poll()


@pytest.mark.parametrize('when', [130.0, 131.0])
def test_original_120_second_deadline_blocks_even_with_unpolled_complete_markers(setup, when):
    reader, path, _, _, clock, _ = setup
    append(path, READY.read_bytes())
    clock[0] = when
    with pytest.raises(DataError):
        reader.poll()


def test_slow_log_read_cannot_cross_original_deadline_and_return_ready(setup):
    reader, path, _, _, clock, _ = setup
    append(path, READY.read_bytes())
    original = reader.log.read_after
    def delayed(cursor):
        content = original(cursor)
        clock[0] = 130.0
        return content
    reader.log.read_after = delayed
    with pytest.raises(DataError):
        reader.poll()


def test_missing_log_waits_but_does_not_extend_deadline(setup):
    reader, path, game, _, clock, wall = setup
    path.unlink()
    # A new reader constructed before a current launch creates its log waits.
    reader = StartupLogReader(path.parent, game, '1.41.8.8', clock=lambda:clock[0], wall=lambda:wall[0])
    assert reader.poll() is None
    clock[0] = 130
    with pytest.raises(DataError):
        reader.poll()


def test_fixture_contains_only_exact_required_startup_lines_with_verified_small_manifest():
    for path in (READY, INCOMPLETE, PATCH_READY):
        raw = path.read_bytes()
        manifest = json.loads(path.with_suffix('.json').read_text('utf-8'))
        assert len(raw) == manifest['fixture_bytes'] < 1024
        assert hashlib.sha256(raw).hexdigest() == manifest['fixture_sha256']
        assert len(raw.splitlines()) == len(manifest['source_lines'])
        assert all(any(marker in line for marker in (b'server module init ok',b'created game rules',
            b'connection succeeded',b'LoopActivateAllSystems done')) for line in raw.splitlines())
