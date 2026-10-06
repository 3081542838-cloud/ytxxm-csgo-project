"""Replay the successful formal session's original observations, without input.

Input counters stand in for a keyboard. Actual engine and binding responses
must pass the production readers; preview-state.json supplies no evidence.
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
from cs2pov.services.hidden_playback import HiddenPlayback
from cs2pov.services.replay import ReplayController, ReplayError


@pytest.fixture
def actual():
    fixture = Path('tests/fixtures/replay-formal-verified-playback-20261004.log')
    raw = fixture.read_bytes()
    metadata = json.loads(fixture.with_suffix('.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == metadata['fixture_sha256']
    lines = raw.splitlines(keepends=True)
    assert len(raw) == metadata['fixture_bytes'] == 5766
    assert len(lines) == len(metadata['records']) == 27
    assert all(line.endswith(b'\r\n') for line in lines)
    assert [r['source_line'] for r in metadata['records']] == sorted(
        r['source_line'] for r in metadata['records'])
    assert metadata['binding_query_count_in_source'] == 3
    assert len({q['nonce'] for q in metadata['binding_queries']}) == 3
    return metadata, lines


def source_time(line):
    return datetime.strptime('2026/' + line.decode('utf-8')[:14],
                             '%Y/%m/%d %H:%M:%S').timestamp()


def timer_for(lines):
    origin = source_time(lines[0])
    timer = SimpleNamespace(wall=origin + .25, mono=100.25)

    def advance(line):
        timer.wall = source_time(line) + .25
        timer.mono = 100.25 + source_time(line) - origin

    return timer, advance


def append(log, line):
    with log.open('ab') as stream:
        stream.write(line)


def playback_from_actual(actual, tmp_path, monkeypatch, *, final_response='actual'):
    metadata, lines = actual
    identity = ProcessIdentity(**metadata['process_identity'])
    timer, advance = timer_for(lines)
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    reader = ReplayLogReader(tmp_path, identity, metadata['nonce'],
        clock=lambda: timer.mono, wall=lambda: timer.wall)
    console = SimpleNamespace(console_open_confirmed=True, commands=[], keys=0, hidden=0,
        foreground_pid=lambda: identity.pid, readback=reader.readback, snapshot=reader.snapshot)
    console.command = console.commands.append

    def hide_console():
        console.hidden += 1
        console.console_open_confirmed = False

    def count_key():
        console.keys += 1

    console.hide_console = hide_console
    console.press_play_key = count_key
    controller = ReplayController(
        SimpleNamespace(verify=lambda: identity, argv=['cs2.exe', '-insecure']),
        console, clock=lambda: timer.mono)
    draft = dict(demo=metadata['demo'], selection=metadata['selection'])
    nonces = iter(q['nonce'] for q in metadata['binding_queries'])
    monkeypatch.setattr('cs2pov.services.hidden_playback.uuid.uuid4',
                        lambda: SimpleNamespace(hex=next(nonces)))
    requests = []

    def binding_reader(session, current_identity, nonce, *, expected=None, clock):
        requests.append(nonce)
        return BindingLogReader(session, current_identity, nonce, expected=expected,
                                clock=clock, wall=lambda: timer.wall)

    playback = HiddenPlayback(controller, tmp_path, draft, clock=lambda: timer.mono,
                              reader_factory=binding_reader)
    boundaries, moving = [], []
    final_nonce = metadata['binding_queries'][-1]['nonce'].encode('ascii')
    for original, record in zip(lines, metadata['records']):
        advance(original)
        meaning = record['meaning']
        line = original
        if meaning == 'final_unbound_value':
            if final_response == 'missing_value':
                continue
            if final_response == 'echo_value':
                line = line.replace(b'[Console] bind ', b'[Console] echo bind ')
        if meaning.startswith('final_unbound_') and final_response == 'wrong_nonce':
            line = line.replace(final_nonce, b'f' * 32)
        append(log, line)
        reader.readback()
        if meaning == 'wrong_player_at_start':
            assert reader.decoder.readback().player_id == '76561199797409805'
            with pytest.raises(ReplayError, match='回读'):
                controller.verify_result(draft, since=timer.mono - 5)
        elif meaning == 'target_first_person_at_start':
            boundaries.append(controller.verify_result(draft, since=timer.mono - 5))
        elif meaning == 'fresh_start_before_binding':
            playback.begin()
            assert playback.state == 'querying_empty' and console.keys == 0
        elif meaning.startswith('original_empty_') or meaning.startswith('installed_binding_'):
            playback.poll()
        elif meaning == 'binding_install_command_echo':
            playback.poll()
            assert playback.state == 'querying_installed' and playback.reader.readback() is None
        elif meaning == 'fresh_start_after_install':
            playback.poll()
            assert playback.state == 'ready' and console.hidden == 1 and console.keys == 0
        elif meaning == 'fresh_start_before_play':
            assert controller.verify_result(draft, since=timer.mono - 5).tick == 10350
            playback.request_play()
            playback.poll()
            assert playback.state == 'playing' and console.keys == 1
        elif meaning == 'playback_unpaused':
            playback.poll()
            assert reader.decoder.readback() is None
        elif meaning.startswith('playback_movement_'):
            observed = reader.snapshot()
            assert observed is not None and not observed.paused and observed.first_person
            assert observed.player_id == metadata['selection']['player_id']
            assert reader.decoder.readback() is None  # No inferred server tick while moving.
            moving.append(observed.tick)
            playback.poll()
        elif meaning in ('exact_end_pause', 'exact_end_engine'):
            playback.poll()
            assert playback.state == 'playing'
        elif meaning == 'target_first_person_at_end':
            boundaries.append(playback.poll())
            assert playback.state == 'ended' and playback.binding_may_exist
        elif meaning == 'fresh_end_before_final_query':
            console.console_open_confirmed = True
            playback.confirm_end_console()
            assert playback.state == 'querying_unbound'
            assert playback.request_nonce == metadata['binding_queries'][-1]['nonce']
        elif meaning.startswith('final_unbound_') or meaning == 'fresh_end_after_final_query':
            playback.poll()
    assert [(p.tick, p.server_tick) for p in boundaries] == [(10350,13682),(10478,13810)]
    assert all(p.paused and p.first_person and p.player_id == '76561199198478034'
               and p.process_identity == identity for p in boundaries)
    assert moving == [10361,10393,10425,10458]
    assert requests == [q['nonce'] for q in metadata['binding_queries']]
    assert console.keys == 1 and console.hidden == 1 and len(console.commands) == 3
    assert reader.cursor.offset == log.stat().st_size
    return SimpleNamespace(playback=playback, controller=controller, console=console,
        reader=reader, draft=draft, timer=timer, identity=identity, metadata=metadata)


def test_actual_exact_end_and_new_final_empty_query_complete_formal_playback(actual, tmp_path, monkeypatch):
    env = playback_from_actual(actual, tmp_path, monkeypatch)
    playback = env.playback
    assert playback.state == 'verified' and not playback.binding_may_exist and playback.triggered
    final = playback.reader.readback()
    assert final is not None and final.value == '' and final.key == 'F8'
    assert final.process_identity == env.identity
    assert final.request_nonce == env.metadata['binding_queries'][-1]['nonce']
    assert final.observed_at == pytest.approx(env.timer.mono - .25)
    # The later query still requires fresh, independent exact endpoint evidence.
    endpoint = env.controller.verify_result(env.draft, since=playback.started_at, at_end=True)
    assert (endpoint.tick,endpoint.server_tick) == (10478,13810)
    assert env.controller.state == 'end_verified'


@pytest.mark.parametrize('final_response',['missing_value','echo_value','wrong_nonce'])
def test_successful_endpoint_without_actual_matching_final_binding_cannot_verify(
        actual, tmp_path, monkeypatch, final_response):
    env = playback_from_actual(actual, tmp_path, monkeypatch, final_response=final_response)
    playback = env.playback
    assert playback.state == 'querying_unbound' and playback.binding_may_exist
    assert playback.reader.readback() is None
    assert env.controller.verify_result(env.draft, since=playback.started_at, at_end=True)
    env.timer.mono += 6; env.timer.wall += 6
    with pytest.raises(ReplayError,match='超时'): playback.poll()
    assert playback.state == 'failed' and playback.binding_may_exist and env.console.keys == 1


@pytest.mark.parametrize('nonce_kind',['original_empty','installed','other'])
def test_final_actual_empty_binding_rejects_wrong_request_nonce(actual, tmp_path, nonce_kind):
    metadata, lines = actual
    query = metadata['binding_queries'][-1]
    response = [line for line,r in zip(lines,metadata['records'])
                if r['source_line'] in query['source_lines']]
    nonce = ('f' * 32 if nonce_kind == 'other' else
             next(q['nonce'] for q in metadata['binding_queries'] if q['kind'] == nonce_kind))
    timer, advance = timer_for(response)
    log = tmp_path / 'engine.log'; log.write_bytes(b'')
    reader = BindingLogReader(tmp_path,ProcessIdentity(**metadata['process_identity']),nonce,
                              clock=lambda:timer.mono,wall=lambda:timer.wall)
    for line in response:
        advance(line); append(log,line)
    assert reader.readback() is None and reader.decoder.state == 'invalid'


def test_installed_unbind_command_echo_does_not_prove_final_empty_binding(actual, tmp_path):
    metadata, lines = actual
    echoes = [line for line,r in zip(lines,metadata['records'])
              if r['meaning'] == 'binding_install_command_echo']
    assert b'unbind F8; demo_resume; demo_pauseatservertick 13810' in b''.join(echoes)
    timer, advance = timer_for(echoes)
    log = tmp_path / 'engine.log'; log.write_bytes(b'')
    reader = BindingLogReader(tmp_path,ProcessIdentity(**metadata['process_identity']),
        metadata['binding_queries'][-1]['nonce'],clock=lambda:timer.mono,wall=lambda:timer.wall)
    for line in echoes:
        advance(line); append(log,line)
    assert reader.readback() is None and reader.decoder.state == 'waiting'


def test_actual_endpoints_cannot_be_reused_under_wrong_telemetry_nonce(actual, tmp_path):
    metadata, lines = actual
    timer, advance = timer_for(lines)
    log = tmp_path / 'engine.log'; log.write_bytes(b'')
    identity = ProcessIdentity(**metadata['process_identity'])
    reader = ReplayLogReader(tmp_path,identity,'f' * 32,
                              clock=lambda:timer.mono,wall=lambda:timer.wall)
    for line in lines:
        advance(line); append(log,line); reader.readback()
    assert reader.readback() is None and reader.snapshot() is None
    controller = ReplayController(SimpleNamespace(verify=lambda:identity,argv=['cs2.exe','-insecure']),
        SimpleNamespace(readback=reader.readback,foreground_pid=lambda:identity.pid),clock=lambda:timer.mono)
    with pytest.raises(ReplayError,match='回读'):
        controller.verify_result(dict(demo=metadata['demo'],selection=metadata['selection']),
                                 since=timer.mono-5,at_end=True)


def test_actual_final_binding_and_endpoint_expire_after_five_seconds(actual, tmp_path, monkeypatch):
    env = playback_from_actual(actual,tmp_path,monkeypatch)
    env.timer.mono += 6; env.timer.wall += 6
    assert env.playback.reader.readback() is None
    with pytest.raises(ReplayError,match='回读'):
        env.controller.verify_result(env.draft,since=env.playback.started_at,at_end=True)
    assert env.controller.state == 'unverified'
