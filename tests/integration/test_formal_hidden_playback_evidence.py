"""Re-read exact formal-button evidence without a game or keyboard adapter.

The source session ended after the paused endpoint and exit/restoration. It
never completed its final runtime binding query; tests preserve that limit.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.binding_log import BindingLogReader
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogReader
from cs2pov.services.replay import ReplayController, ReplayError


@pytest.fixture
def actual():
    fixture = Path('tests/fixtures/replay-formal-hidden-playback-20261003.log')
    raw = fixture.read_bytes()
    metadata = json.loads(fixture.with_suffix('.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == metadata['fixture_sha256']
    lines = raw.splitlines(keepends=True)
    assert len(lines) == len(metadata['records']) == 25
    assert len(raw) == 5611 and all(line.endswith(b'\n') for line in lines)
    assert [r['source_line'] for r in metadata['records']] == sorted(r['source_line'] for r in metadata['records'])
    return metadata, lines


def stamp(line):
    return datetime.strptime('2026/' + line.decode('utf-8')[:14], '%Y/%m/%d %H:%M:%S').timestamp()


def timer_for(lines):
    origin = stamp(lines[0])
    timer = SimpleNamespace(wall=origin + .25, mono=100.25)

    def advance(line):
        timer.wall = stamp(line) + .25
        timer.mono = 100.25 + stamp(line) - origin

    return timer, advance


def append(log, line):
    with log.open('ab') as stream:
        stream.write(line)


def test_formal_button_log_proves_exact_start_movement_and_predeath_endpoint(actual, tmp_path):
    metadata, lines = actual
    identity = ProcessIdentity(**metadata['process_identity'])
    timer, advance = timer_for(lines)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    reader = ReplayLogReader(tmp_path, identity, metadata['nonce'],
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    controller = ReplayController(
        SimpleNamespace(verify=lambda: identity, argv=['cs2.exe', '-insecure']),
        SimpleNamespace(readback=reader.readback, foreground_pid=lambda: identity.pid),
        clock=lambda: timer.mono)
    draft = dict(demo=r'C:\Users\测试员\AppData\Roaming\Wmpvp\demo\9210181067658518284_0.dem',
        selection=dict(start_tick=10350, server_start_tick=13682, end_tick=10478,
            server_end_tick=13810, player_id='76561199198478034'))
    boundaries, moving = [], []
    started_at = None
    rejected_other_player = False
    for line, record in zip(lines, metadata['records']):
        advance(line)
        append(log, line)
        reader.readback()
        meaning = record['meaning']
        if meaning == 'wrong_player_at_start':
            assert reader.decoder.readback().player_id == '76561199797409805'
            with pytest.raises(ReplayError):
                controller.verify_result(draft, since=timer.mono - 1)
            rejected_other_player = True
        elif meaning == 'target_first_person_at_start':
            boundaries.append(controller.verify_result(draft, since=timer.mono - 1))
        elif meaning == 'fresh_start_before_play':
            assert controller.verify_result(draft, since=timer.mono - 5).tick == 10350
        elif meaning == 'playback_unpaused':
            started_at = timer.mono
            assert reader.decoder.readback() is None
        elif meaning.startswith('playback_') and meaning != 'playback_unpaused':
            snapshot = reader.snapshot()
            assert snapshot is not None and not snapshot.paused and snapshot.first_person
            assert snapshot.player_id == '76561199198478034'
            assert reader.decoder.readback() is None  # No inferred server clock while moving.
            moving.append(snapshot.tick)
        elif meaning == 'target_first_person_at_end':
            boundaries.append(controller.verify_result(draft, since=started_at, at_end=True))
    assert rejected_other_player and moving == [10372, 10404, 10437, 10469]
    assert [(proof.tick, proof.server_tick) for proof in boundaries] == [(10350, 13682), (10478, 13810)]
    assert controller.state == 'end_verified'
    assert boundaries[-1].tick == metadata['death_analysis']['death']['tick'] - 1
    assert reader.cursor.offset == log.stat().st_size


@pytest.mark.parametrize('query_kind', ['original_empty', 'installed'])
def test_actual_binding_query_uses_its_own_cursor_and_exact_value(actual, tmp_path, query_kind):
    metadata, lines = actual
    query = next(q for q in metadata['binding_queries'] if q['kind'] == query_kind)
    response = [line for line, r in zip(lines, metadata['records']) if r['source_line'] in query['source_lines']]
    identity = ProcessIdentity(**metadata['process_identity'])
    timer, advance = timer_for(response)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    replay = ReplayLogReader(tmp_path, identity, metadata['nonce'],
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    binding = BindingLogReader(tmp_path, identity, query['nonce'],
        expected=query['value'] if query_kind == 'installed' else None,
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    replay_cursor = replay.cursor
    for line in response:
        advance(line)
        append(log, line)
    proof = binding.readback()
    assert proof is not None and proof.value == query['value']
    assert proof.process_identity == identity and proof.request_nonce == query['nonce'] and proof.key == 'F8'
    assert proof.observed_at == pytest.approx(timer.mono - .25)
    assert replay.cursor == replay_cursor  # Binding reads cannot advance replay state.
    assert replay.readback() is None  # Console query text does not supply Demo evidence.
    assert replay.cursor == binding.cursor


def test_formal_endpoint_and_exit_do_not_prove_runtime_unbinding(actual, tmp_path):
    metadata, lines = actual
    identity = ProcessIdentity(**metadata['process_identity'])
    installed_end = next(i for i, r in enumerate(metadata['records']) if r['meaning'] == 'installed_binding_end')
    timer, advance = timer_for(lines)
    log = tmp_path / 'engine.log'
    log.write_bytes(b''.join(lines[:installed_end + 1]))
    # A read-only fresh request after installation must not reuse the original
    # empty query as evidence that the runtime action later self-unbound.
    final_binding = BindingLogReader(tmp_path, identity, 'f' * 32,
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    replay = ReplayLogReader(tmp_path, identity, metadata['nonce'],
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    for line in lines[installed_end + 1:]:
        advance(line)
        append(log, line)
        replay_cursor = replay.cursor
        assert final_binding.readback() is None
        assert replay.cursor == replay_cursor
        replay.readback()
    endpoint = replay.readback()
    assert endpoint is not None and (endpoint.tick, endpoint.server_tick) == (10478, 13810)
    assert endpoint.player_id == '76561199198478034' and endpoint.first_person and endpoint.paused
    assert final_binding.decoder.state == 'waiting' and final_binding.decoder.proof is None
    assert metadata['binding_query_count_in_source'] == 2 and metadata['final_binding_query_in_source'] is False
    summary = metadata['source_summary']
    assert summary['state'] == 'complete' and summary['error'] == ''
    assert summary['playback'] == dict(state='ended', triggered=True, binding_may_exist=True)
    assert summary['playback']['state'] != 'verified'


def test_actual_self_unbind_command_echo_is_not_an_unbound_response(actual, tmp_path):
    metadata, lines = actual
    query = next(q for q in metadata['binding_queries'] if q['kind'] == 'installed')
    echoes = [line for line, r in zip(lines, metadata['records']) if r['meaning'].startswith('binding_execution_echo')]
    assert b'unbind F8; demo_resume; demo_pauseatservertick 13810' in b''.join(echoes)
    timer, advance = timer_for(echoes)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    reader = BindingLogReader(tmp_path, ProcessIdentity(**metadata['process_identity']), query['nonce'],
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    for line in echoes:
        advance(line)
        append(log, line)
    assert reader.readback() is None and reader.decoder.state == 'waiting'


def test_original_empty_response_cannot_authorize_actual_install_request(actual, tmp_path):
    metadata, lines = actual
    original, installed = metadata['binding_queries']
    response = [line for line, r in zip(lines, metadata['records']) if r['source_line'] in original['source_lines']]
    timer, advance = timer_for(response)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    reader = BindingLogReader(tmp_path, ProcessIdentity(**metadata['process_identity']), installed['nonce'],
        expected=installed['value'], clock=lambda: timer.mono, wall=lambda: timer.wall)
    for line in response:
        advance(line)
        append(log, line)
    assert reader.readback() is None and reader.decoder.state == 'invalid'
