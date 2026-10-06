from dataclasses import replace
from types import SimpleNamespace

import pytest
import ctypes

from cs2pov.adapters.command_pipe import CommandPipeError, CommandPipes, Win32PipeBackend, _RETIRED
from cs2pov.adapters.owned_process import ProcessIdentity


class Backend:
    def __init__(self):
        self.created = []
        self.closed = []
        self.writes = []
        self.connects = []
        self.pids = {1: 123, 2: 123}
        self.connections = {1: True, 2: True}
        self.fail_create = None
        self.write_result = None
        self.write_hook = self.connect_hook = self.drain_hook = lambda: None
        self.drained = []

    def create(self, path):
        self.created.append(path)
        if len(self.created) == self.fail_create:
            raise OSError('creation failed')
        return len(self.created)

    def connect(self, handle):
        self.connects.append(handle)
        self.connect_hook()
        return self.connections[handle]

    def client_pid(self, handle):
        return self.pids[handle]

    def drain(self, handle, *, limit):
        self.drained.append((handle, limit))
        self.drain_hook()
        return 0

    def write(self, handle, payload, *, deadline, clock):
        self.writes.append((handle, payload, deadline))
        self.write_hook()
        if isinstance(self.write_result, Exception):
            raise self.write_result
        return len(payload) if self.write_result is None else self.write_result

    def close(self, handle):
        self.closed.append(handle)


@pytest.fixture
def setup(tmp_path):
    now = [10.0]
    identity = ProcessIdentity(123, 456, str(tmp_path/'game/bin/win64/cs2.exe'))
    current = [identity]
    game = SimpleNamespace(verify=lambda: current[0], argv=['cs2.exe', '-insecure'])
    backend = Backend()
    pipes = CommandPipes(backend=backend, clock=lambda: now[0], nonce='a'*32)
    return pipes, backend, game, current, now


def connected(setup):
    pipes, backend, game, current, now = setup
    assert pipes.create() is pipes
    assert pipes.poll_connection(game) is True
    return pipes, backend, game, current, now


def test_create_provisions_both_unique_pipe_servers_before_argument_is_available(setup):
    pipes, backend, *_ = setup
    with pytest.raises(CommandPipeError):
        _ = pipes.argument
    assert pipes.create() is pipes
    assert backend.created == [rf'\\.\pipe\cs2pov_{"a"*32}_cmd', rf'\\.\pipe\cs2pov_{"a"*32}_out']
    assert pipes.argument == ','.join(backend.created)
    with pytest.raises(CommandPipeError):
        pipes.create()
    other = CommandPipes(backend=Backend())
    other.create()
    assert other.argument != pipes.argument
    other.close()


@pytest.mark.parametrize('failed', [1, 2])
def test_partial_create_failure_closes_only_successfully_created_servers(setup, failed):
    pipes, backend, *_ = setup
    backend.fail_create = failed
    with pytest.raises(CommandPipeError):
        pipes.create()
    assert backend.closed == ([] if failed == 1 else [1])
    with pytest.raises(CommandPipeError):
        _ = pipes.argument
    pipes.close()
    assert backend.closed == ([] if failed == 1 else [1])


def test_connection_waits_for_both_clients_and_drains_only_output(setup):
    pipes, backend, game, *_ = setup
    pipes.create()
    backend.connections[2] = False
    assert pipes.poll_connection(game) is False
    assert backend.writes == []
    backend.connections[2] = True
    assert pipes.poll_connection(game) is True
    assert backend.drained and all(h == 2 and 0 < limit <= 65536 for h, limit in backend.drained)


@pytest.mark.parametrize('handle', [1, 2])
@pytest.mark.parametrize('pid', [124, None, True, 0])
def test_any_wrong_or_unreadable_client_pid_revokes_and_closes_without_input(setup, handle, pid):
    pipes, backend, game, *_ = setup
    pipes.create()
    backend.pids[handle] = pid
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert sorted(backend.closed) == [1, 2] and backend.writes == []
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)


@pytest.mark.parametrize('phase', ['connecting', 'connected'])
@pytest.mark.parametrize('change', [{'pid': 124}, {'created': 457}, {'executable': 'C:/unknown.exe'}])
def test_full_identity_change_before_or_after_connection_never_sends(setup, phase, change):
    pipes, backend, game, current, _ = setup
    pipes.create()
    if phase == 'connecting':
        backend.connections[2] = False
        assert pipes.poll_connection(game) is False
    else:
        assert pipes.poll_connection(game) is True
    current[0] = replace(current[0], **change)
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game) if phase == 'connected' else pipes.poll_connection(game)
    assert backend.writes == [] and sorted(backend.closed) == [1, 2]


@pytest.mark.parametrize('argv', [[], ['cs2.exe'], None, '-insecure'])
def test_missing_or_malformed_insecure_evidence_cannot_connect(setup, argv):
    pipes, backend, game, *_ = setup
    pipes.create()
    game.argv = argv
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert backend.writes == [] and sorted(backend.closed) == [1, 2]


def test_identity_changed_while_connection_checked_is_not_accepted(setup):
    pipes, backend, game, current, _ = setup
    pipes.create()
    backend.connect_hook = lambda: current.__setitem__(0, replace(current[0], created=457))
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert backend.writes == []


def test_valid_command_is_one_utf8_lf_write_and_write_receipt_is_not_execution_proof(setup):
    pipes, backend, game, *_ = connected(setup)
    assert pipes.send_command('spec_player " 一条小虾米OVO"', game) is None
    assert backend.writes == [(1, 'spec_player " 一条小虾米OVO"\n'.encode('utf-8'), 11.0)]
    assert not hasattr(pipes, 'replay_verified')


@pytest.mark.parametrize('value', ['', None, 'a'*513, 'a\nb', 'a\rb', 'a\0b', 'a\tb', 'a\x1fb', 'a\x7fb', '\ud800'])
def test_invalid_command_cannot_produce_any_write(setup, value):
    pipes, backend, game, *_ = connected(setup)
    with pytest.raises(CommandPipeError):
        pipes.send_command(value, game)
    assert backend.writes == []


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 6, '1'])
def test_invalid_timeout_cannot_write(setup, timeout):
    pipes, backend, game, *_ = connected(setup)
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game, timeout=timeout)
    assert backend.writes == []


@pytest.mark.parametrize('result', [0, 1, True, OSError('broken'), TimeoutError('pending cancelled')])
def test_partial_or_failed_write_is_unknown_and_transport_cannot_automatically_retry(setup, result):
    pipes, backend, game, *_ = connected(setup)
    backend.write_result = result
    with pytest.raises(CommandPipeError, match='未知'):
        pipes.send_command('demo_pause', game)
    backend.write_result = None
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)
    assert len(backend.writes) == 1 and sorted(backend.closed) == [1, 2]


def test_slow_readonly_guard_cannot_send_after_original_deadline(setup):
    pipes, backend, game, _, now = connected(setup)
    backend.drain_hook = lambda: now.__setitem__(0, 11.0)
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)
    assert backend.writes == []


def test_successful_late_write_remains_unknown_and_cannot_repeat(setup):
    pipes, backend, game, _, now = connected(setup)
    backend.write_hook = lambda: now.__setitem__(0, 11.0)
    with pytest.raises(CommandPipeError, match='未知'):
        pipes.send_command('demo_pause', game)
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)
    assert len(backend.writes) == 1


def test_identity_changed_after_written_command_remains_unknown_and_cannot_repeat(setup):
    pipes, backend, game, current, _ = connected(setup)
    backend.write_hook = lambda: current.__setitem__(0, replace(current[0], created=457))
    with pytest.raises(CommandPipeError, match='未知'):
        pipes.send_command('demo_pause', game)
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)
    assert len(backend.writes) == 1


def test_insecure_removed_after_write_is_unknown(setup):
    pipes, backend, game, *_ = connected(setup)
    backend.write_hook = lambda: setattr(game, 'argv', ['cs2.exe'])
    with pytest.raises(CommandPipeError, match='未知'):
        pipes.send_command('demo_pause', game)
    assert len(backend.writes) == 1


def test_expired_connection_and_closed_transport_are_terminal(setup):
    pipes, backend, game, _, now = setup
    pipes.create()
    now[0] = 130.0
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert backend.writes == []
    pipes.close()
    pipes.close()
    assert sorted(backend.closed) == [1, 2]
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)


def test_lock_wait_cannot_renew_absolute_dispatch_deadline(setup):
    pipes, backend, game, _, now = connected(setup)
    class SlowLock:
        def __enter__(self): now[0] += .2
        def __exit__(self, *args): return False
    pipes._lock = SlowLock()
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game, timeout=1, deadline=10.1)
    assert not backend.writes and pipes.state == 'failed'
    with pytest.raises(CommandPipeError): pipes.send_command('demo_pause', game)
    assert not backend.writes


@pytest.mark.parametrize('ready', [False, True])
def test_slow_connection_poll_cannot_return_a_wait_or_connection_after_original_deadline(setup, ready):
    pipes, backend, game, _, now = setup
    pipes.create()
    backend.connections[2] = ready
    backend.connect_hook = lambda: now.__setitem__(0, 130.0)
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert backend.writes == [] and sorted(backend.closed) == [1, 2]


def test_cleanup_failure_keeps_failed_handle_for_explicit_close_without_input_authority(setup):
    pipes, backend, game, *_ = connected(setup)
    failed_once = [False]
    original = backend.close

    def close(handle):
        if handle == 1 and not failed_once[0]:
            failed_once[0] = True
            raise OSError('close failed')
        original(handle)

    backend.close = close
    backend.pids[1] = 999
    with pytest.raises(CommandPipeError):
        pipes.poll_connection(game)
    assert pipes.handles == [1]
    assert pipes.cleanup_errors
    with pytest.raises(CommandPipeError):
        pipes.send_command('demo_pause', game)
    pipes.close()
    assert pipes.handles == [] and sorted(backend.closed) == [1, 2] and backend.writes == []


@pytest.mark.parametrize('nonce', ['a', 'a'*31, 'g'*32, 'a'*32+'\\x', None, True])
def test_explicit_nonce_cannot_select_an_arbitrary_pipe_name(nonce):
    with pytest.raises(CommandPipeError):
        CommandPipes(backend=Backend(), nonce=nonce)


@pytest.mark.parametrize('operation', ['connect', 'read', 'write'])
def test_immediate_os_io_failure_releases_nonpending_event_instead_of_retaining_forever(operation):
    backend = Win32PipeBackend()
    closed = []

    def failure(*_):
        ctypes.set_last_error(109)  # ERROR_BROKEN_PIPE, no pending request
        return False

    fake = SimpleNamespace(CreateEventW=lambda *_: 17, ConnectNamedPipe=failure,
        ReadFile=failure, WriteFile=failure, CloseHandle=lambda handle: (closed.append(handle) or True),
        CancelIoEx=lambda *_: False, WaitForSingleObject=lambda *_: 258)
    backend.api = fake
    backend._owned.add(1)
    try:
        with pytest.raises(CommandPipeError):
            if operation == 'connect':
                backend.connect(1)
            elif operation == 'read':
                backend.drain(1, limit=4096)
            else:
                backend.write(1, b'echo test\n', deadline=11, clock=lambda: 10)
        assert 17 in closed
        backend.close(1)
        assert not any(api is fake for api, _ in _RETIRED)
    finally:
        # Failed red assertions must not leave fake events in global state.
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]


def event_failure_backend():
    backend = Win32PipeBackend()
    closed, writes, failures = [], [], [1]

    def close(handle):
        closed.append(handle)
        if handle == 17 and failures[0]:
            failures[0] -= 1
            ctypes.set_last_error(6)
            return False
        return True

    def write(_handle, _buffer, size, *_):
        writes.append(size)
        return True

    def result(_handle, _overlap, count, _wait):
        count._obj.value = writes[-1] if writes else 0
        return True

    fake = SimpleNamespace(CreateEventW=lambda *_: 17, CloseHandle=close,
        WriteFile=write, GetOverlappedResult=result, CancelIoEx=lambda *_: True,
        WaitForSingleObject=lambda *_: 258)
    backend.api = fake
    backend._owned.update((1, 2))
    return backend, fake, closed, writes, failures


def test_completed_operation_event_close_failure_retains_storage_reports_and_explicit_reap_retries():
    backend, fake, closed, *_ = event_failure_backend()
    operation = backend._operation(1, buffer=ctypes.create_string_buffer(b'held'), size=4)
    try:
        with pytest.raises(CommandPipeError, match='事件'):
            backend._release_operation(operation)
        assert any(api is fake and held is operation for api, held in _RETIRED)
        assert operation.buffer.raw == b'held\0' and backend.cleanup_errors
        backend._reap()
        assert closed.count(17) == 2 and not backend.cleanup_errors
        assert not any(api is fake for api, _ in _RETIRED)
    finally:
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]


def test_event_close_failure_after_complete_write_revokes_transport_and_does_not_repeat(setup):
    pipes, _, game, *_ = setup
    backend, fake, closed, writes, _ = event_failure_backend()
    created = []
    backend.create = lambda path: (created.append(path) or len(created))
    backend.connect = lambda _handle: True
    backend.client_pid = lambda _handle: 123
    backend.drain = lambda _handle, limit: 0
    pipes.backend = backend
    try:
        pipes.create()
        assert pipes.poll_connection(game)
        with pytest.raises(CommandPipeError, match='未知'):
            pipes.send_command('demo_pause', game)
        assert pipes.state == 'failed' and pipes.cleanup_errors
        with pytest.raises(CommandPipeError):
            pipes.send_command('demo_pause', game)
        assert writes == [len(b'demo_pause\n')]
        assert any(api is fake for api, _ in _RETIRED)
        backend._reap()
        assert closed.count(17) == 2 and not backend.cleanup_errors
    finally:
        pipes.close()
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]


def test_retired_pending_operation_failed_event_close_is_kept_for_later_reap():
    backend, fake, closed, _, _ = event_failure_backend()
    operation = backend._operation(1, buffer=ctypes.create_string_buffer(b'pending'), size=7)
    fake.WaitForSingleObject = lambda *_: 0
    _RETIRED.append((fake, operation))
    try:
        with pytest.raises(CommandPipeError, match='事件'):
            backend._reap()
        assert any(api is fake and held is operation for api, held in _RETIRED)
        assert operation.buffer.raw == b'pending\0' and backend.cleanup_errors
        backend._reap()
        assert not any(api is fake for api, _ in _RETIRED)
        assert closed.count(17) == 2 and not backend.cleanup_errors
    finally:
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]


def test_cancelled_operation_event_close_failure_during_pipe_shutdown_keeps_storage_for_reap():
    backend, fake, closed, _, _ = event_failure_backend()
    operation = backend._operation(1, buffer=ctypes.create_string_buffer(b'cancelled'), size=9)
    backend._reads[1] = operation
    fake.WaitForSingleObject = lambda *_: 0
    try:
        with pytest.raises(CommandPipeError, match='事件'):
            backend.close(1)
        assert 1 not in backend._owned and backend.cleanup_errors
        assert any(api is fake and held is operation for api, held in _RETIRED)
        assert operation.buffer.raw == b'cancelled\0'
        backend._reap()
        assert not any(api is fake for api, _ in _RETIRED)
        assert closed.count(17) == 2 and not backend.cleanup_errors
        backend.close(1)
        assert closed.count(1) == 1
    finally:
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]


def test_public_close_retries_held_event_cleanup_without_private_reap_or_restoring_input(setup):
    pipes, _, game, *_ = setup
    backend, fake, closed, writes, _ = event_failure_backend()
    created = []
    backend.create = lambda path: (created.append(path) or len(created))
    backend.connect = lambda _handle: True
    backend.client_pid = lambda _handle: 123
    backend.drain = lambda _handle, limit: 0
    pipes.backend = backend
    try:
        pipes.create()
        assert pipes.poll_connection(game)
        with pytest.raises(CommandPipeError, match='未知'):
            pipes.send_command('demo_pause', game)
        assert pipes.state == 'failed' and pipes.handles == [] and pipes.cleanup_errors
        pipes.close()
        assert pipes.state == 'closed' and not pipes.cleanup_errors
        assert not any(api is fake for api, _ in _RETIRED)
        assert closed.count(17) == 2 and writes == [len(b'demo_pause\n')]
        with pytest.raises(CommandPipeError):
            pipes.send_command('demo_pause', game)
        pipes.close()
        assert closed.count(17) == 2
    finally:
        _RETIRED[:] = [(api, op) for api, op in _RETIRED if api is not fake]
