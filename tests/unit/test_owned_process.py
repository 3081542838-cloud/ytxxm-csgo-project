from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from cs2pov.adapters.owned_process import OwnedGame, ProcessIdentity
from cs2pov.adapters.processes import ProcessError


def fixture_game(tmp_path, **changes):
    exe = tmp_path / "game/bin/win64/cs2.exe"
    exe.parent.mkdir(parents=True); exe.write_bytes(b"fake exe, never executed")
    session = tmp_path / "session"; session.mkdir()
    identity = ProcessIdentity(123, 456, str(exe))
    calls = []
    defaults = dict(query=lambda _: identity, closed=lambda: True,
                    popen=lambda argv, cwd: (calls.append(argv) or SimpleNamespace(pid=123, poll=lambda: None)),
                    terminate=lambda id: calls.append(id))
    defaults.update(changes)
    return OwnedGame(**defaults), session, identity, calls


def test_owned_launch_forces_insecure_and_persists_full_identity(tmp_path):
    import json
    game, session, identity, calls = fixture_game(tmp_path)
    assert game.launch(tmp_path, session) == identity
    assert calls[0][1:] == ["-insecure", "-novid", "-worldwide", "-console"]
    assert json.loads((session / "process.json").read_text())["created"] == 456
    game.force_close()
    assert calls[-1] == identity


def test_existing_game_unknown_state_or_launch_failure_cannot_gain_ownership(tmp_path):
    game, session, _, calls = fixture_game(tmp_path, closed=lambda: False)
    with pytest.raises(ProcessError): game.launch(tmp_path, session)
    assert calls == [] and game.identity is None
    def fail(*_, **__): raise OSError("launch failed")
    game.closed = lambda: True; game.popen = fail
    with pytest.raises(OSError): game.launch(tmp_path, session)
    assert game.identity is None


@pytest.mark.parametrize("change", [{"pid": 124}, {"created": 457}, {"executable": "C:/unknown.exe"}])
def test_identity_change_blocks_actions_and_close(tmp_path, change):
    game, session, identity, calls = fixture_game(tmp_path)
    game.launch(tmp_path, session)
    game.query = lambda _: replace(identity, **change)
    with pytest.raises(ProcessError): game.verify()
    with pytest.raises(ProcessError): game.force_close()
    assert len(calls) == 1


def test_exited_game_or_unidentified_process_is_never_closed(tmp_path):
    game, session, _, calls = fixture_game(tmp_path)
    game.launch(tmp_path, session)
    game.process.poll = lambda: 0
    with pytest.raises(ProcessError): game.force_close()
    assert len(calls) == 1


def test_launch_metadata_failure_leaves_unknown_process_untouched(tmp_path):
    def unavailable(_): raise ProcessError("metadata unavailable")
    game, session, _, calls = fixture_game(tmp_path, query=unavailable)
    with pytest.raises(ProcessError): game.launch(tmp_path, session)
    with pytest.raises(ProcessError): game.force_close()
    assert game.identity is None and len(calls) == 1


def test_telemetry_launch_uses_new_absolute_session_log_without_socket(tmp_path):
    game,session,identity,calls = fixture_game(tmp_path)
    assert game.launch(tmp_path,session,telemetry=True)==identity
    assert calls[0][1:]==['-insecure','-novid','-worldwide','-console','-condebug','-con_logfile',str(session/'engine.log')]
    assert not any('vconsole' in arg for arg in calls[0])


def test_existing_log_or_invalid_telemetry_option_blocks_before_launch(tmp_path):
    game,session,_,calls = fixture_game(tmp_path)
    (session/'engine.log').write_text('previous session')
    with pytest.raises(ProcessError,match='已有日志'): game.launch(tmp_path,session,telemetry=True)
    assert calls==[] and game.identity is None
    with pytest.raises(ProcessError,match='选项'): game.launch(tmp_path,session,telemetry='yes')
    assert calls==[]


def created_pipes():
    from cs2pov.adapters.command_pipe import CommandPipes
    backend = SimpleNamespace(create=lambda _: 77, close=lambda _: None)
    return CommandPipes(backend=backend, nonce='a'*32).create()


@pytest.mark.parametrize('mode', ['manual', 'telemetry', 'automatic'])
def test_local_demo_launch_bypasses_region_prompt_and_persists_process_options(tmp_path, mode):
    import json
    game, session, identity, calls = fixture_game(tmp_path)
    options = {'telemetry': mode != 'manual'}
    if mode == 'automatic':
        options['command_pipes'] = created_pipes()

    assert game.launch(tmp_path, session, **options) == identity

    launched = calls[0]
    assert launched.count('-worldwide') == 1
    assert launched.count('-insecure') == 1
    assert '-perfectworld' not in launched and '-promptperfectworld' not in launched
    assert json.loads((session / 'launch.json').read_text(encoding='utf-8'))['argv'] == launched
    assert json.loads((session / 'process.json').read_text(encoding='utf-8'))['argv'] == launched


def test_pipe_launch_requires_created_pair_and_session_log_without_open_console(tmp_path):
    game, session, identity, calls = fixture_game(tmp_path)
    pipes = created_pipes()
    assert game.launch(tmp_path, session, telemetry=True, command_pipes=pipes) == identity
    assert calls[0][1:] == ['-insecure', '-novid', '-worldwide', '-concommandpipe', pipes.argument,
                            '-condebug', '-con_logfile', str(session/'engine.log')]
    assert '-console' not in calls[0]


@pytest.mark.parametrize('invalid', ['raw', 'new', 'closed', 'connected', 'failed', 'paths', 'owner'])
def test_invalid_or_reused_pipe_channel_never_starts_game(tmp_path, invalid):
    from cs2pov.adapters.command_pipe import CommandPipes
    game, session, identity, calls = fixture_game(tmp_path)
    pipes = created_pipes()
    if invalid == 'raw': pipes = pipes.argument
    elif invalid == 'new': pipes = CommandPipes(backend=SimpleNamespace(), nonce='b'*32)
    elif invalid == 'paths': pipes.paths = (r'\\.\pipe\foreign_cmd', r'\\.\pipe\foreign_out')
    elif invalid == 'owner': pipes.identity = identity
    else: pipes.state = invalid
    with pytest.raises(ProcessError, match='管道'):
        game.launch(tmp_path, session, telemetry=True, command_pipes=pipes)
    assert calls == [] and game.identity is None
    assert not (session/'launch.json').exists()


def test_pipe_launch_cannot_omit_execution_readback_or_mix_nonces(tmp_path):
    game, session, _, calls = fixture_game(tmp_path)
    pipes = created_pipes()
    with pytest.raises(ProcessError, match='回读'):
        game.launch(tmp_path, session, command_pipes=pipes)
    pipes.paths = (pipes.paths[0], pipes.paths[1].replace('a'*32, 'b'*32))
    with pytest.raises(ProcessError, match='管道'):
        game.launch(tmp_path, session, telemetry=True, command_pipes=pipes)
    assert calls == [] and not (session/'launch.json').exists()
