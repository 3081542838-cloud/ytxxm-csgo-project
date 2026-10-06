from dataclasses import replace
import json
from types import SimpleNamespace
import pytest

from cs2pov.adapters.binding_log import BindingEvidence
from cs2pov.adapters.command_pipe import CommandPipes
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.pipe_console import PipeConsole
from cs2pov.storage.settings import DataError


def setup(tmp_path):
    identity = ProcessIdentity(42, 123, 'C:/game/cs2.exe')
    env = SimpleNamespace(time=100., writes=[], events=[], proof=None, invalid=False, failure=False)
    game = SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=lambda: identity)
    next_handle = iter([1, 2])
    def write(handle, payload, **kwargs):
        env.events.append('write')
        env.writes.append(payload)
        if env.failure: raise OSError('partial/unknown OS result')
        return len(payload)
    backend = SimpleNamespace(create=lambda _: next(next_handle), close=lambda _: None,
        connect=lambda _: True, client_pid=lambda _: 42, drain=lambda *_, **__: 0, write=write)
    pipes = CommandPipes(backend=backend, clock=lambda: env.time).create()
    assert pipes.poll_connection(game)
    def reader_factory(session, actual_identity, nonce, **kwargs):
        env.nonce = nonce
        return SimpleNamespace(readback=lambda: env.proof,
                               decoder=SimpleNamespace(state='waiting'))
    def persist(path, payload):
        env.events.append('persist')
        assert json.loads(payload)['consumed'] is True
        path.write_bytes(payload)
    console = PipeConsole(game, pipes, tmp_path, clock=lambda: env.time,
                          keyboard=SimpleNamespace(foreground_pid=lambda: 99),
                          binding_reader_factory=reader_factory, persist=persist)
    return console, env, identity


def startup(identity, at=100.):
    from cs2pov.adapters.game_startup import GameStartupEvidence
    return GameStartupEvidence(identity, at)


def ready(console, env, identity):
    console.begin(startup(identity))
    env.proof = BindingEvidence(identity, 'F8', '', env.time, env.nonce)
    assert console.poll_ready()


def test_bootstrap_requires_real_query_before_any_replay_command(tmp_path):
    console, env, identity = setup(tmp_path)
    with pytest.raises(DataError): console.command('demo_resume')
    console.begin(startup(identity))
    assert console.state == 'querying' and env.events == ['persist', 'write']
    assert env.writes[0].endswith(b'\n') and b'POV_BIND_BEGIN_' in env.writes[0]
    assert not console.poll_ready()
    with pytest.raises(DataError): console.command('demo_resume')
    env.proof = BindingEvidence(identity, 'F8', '', env.time, env.nonce)
    assert console.poll_ready()
    console.command('spec_player " 一条小虾米OVO"')
    assert env.events == ['persist', 'write', 'persist', 'write']
    assert env.writes[-1] == 'spec_player " 一条小虾米OVO"\n'.encode('utf-8')
    assert json.loads(console.ledger.read_bytes())['sequence'] == 2
    assert console.foreground_pid() == 99  # No synthetic game focus.


@pytest.mark.parametrize('change', [dict(pid=43), dict(created=124), dict(executable='C:/other.exe')])
def test_startup_wrong_game_never_sends_bootstrap(tmp_path, change):
    console, env, identity = setup(tmp_path)
    with pytest.raises(DataError): console.begin(startup(replace(identity, **change)))
    assert env.writes == [] and console.state == 'failed'


@pytest.mark.parametrize('age', [5.001, -0.001])
def test_stale_or_future_startup_never_sends(tmp_path, age):
    console, env, identity = setup(tmp_path)
    with pytest.raises(DataError): console.begin(startup(identity, env.time-age))
    assert env.writes == []


@pytest.mark.parametrize('change', [dict(key='F9'), dict(value='toggleconsole'),
    dict(request_nonce='a'*32), dict(observed_at=94.999), dict(observed_at=100.001),
    dict(process_identity=ProcessIdentity(42, 124, 'C:/game/cs2.exe'))])
def test_wrong_bootstrap_receipt_permanently_blocks_commands(tmp_path, change):
    console, env, identity = setup(tmp_path)
    console.begin(startup(identity))
    proof = BindingEvidence(identity, 'F8', '', 100., env.nonce)
    env.proof = replace(proof, **change)
    with pytest.raises(DataError): console.poll_ready()
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 1 and console.state == 'failed'


def test_bootstrap_timeout_and_slow_read_do_not_extend_deadline(tmp_path):
    console, env, identity = setup(tmp_path)
    console.begin(startup(identity))
    def slow():
        env.time = 105.
        return BindingEvidence(identity, 'F8', '', 104., env.nonce)
    console.binding_reader.readback = slow
    with pytest.raises(DataError, match='期限'): console.poll_ready()
    with pytest.raises(DataError): console.begin(startup(identity, 105.))
    assert len(env.writes) == 1


@pytest.mark.parametrize('slow_step', ['reader','persist'])
def test_bootstrap_construction_and_persistence_cannot_extend_input_authority(tmp_path, slow_step):
    console, env, identity = setup(tmp_path)
    if slow_step == 'reader':
        original = console.binding_reader_factory
        def slow(*args, **kwargs):
            env.time += 6
            return original(*args, **kwargs)
        console.binding_reader_factory = slow
    else:
        original = console.persist
        def slow(*args):
            original(*args)
            env.time += 6
        console.persist = slow
    with pytest.raises(DataError): console.begin(startup(identity))
    assert env.writes == [] and console.state == 'failed'
    with pytest.raises(DataError): console.begin(startup(identity,env.time))


def test_command_slow_persistence_never_writes_after_frozen_dispatch_deadline(tmp_path):
    console, env, identity = setup(tmp_path); ready(console, env, identity)
    original = console.persist
    def slow(*args):
        original(*args)
        env.time += 1
    console.persist = slow
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 1 and console.state == 'failed'


def test_bootstrap_source_expiring_during_short_persistence_does_not_send(tmp_path):
    console, env, identity = setup(tmp_path)
    original = console.persist
    def delayed(*args):
        original(*args); env.time += .2
    console.persist = delayed
    with pytest.raises(DataError): console.begin(startup(identity,95.1))
    assert env.writes == [] and console.state == 'failed'


def test_durable_consumption_failure_cannot_write_or_retry(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    def unavailable(*_): raise OSError('disk full')
    console.persist = unavailable
    with pytest.raises(OSError): console.command('demo_resume')
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 1


def test_uncertain_send_permanently_consumes_one_attempt(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    env.failure = True
    with pytest.raises(DataError): console.command('demo_resume')
    assert json.loads(console.ledger.read_bytes())['sequence'] == 2
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 2 and console.pipes.state == 'failed'


def test_ledger_prevents_crash_restart_and_keyboard_confirmation_is_never_granted(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    with pytest.raises(DataError, match='旧会话'):
        PipeConsole(console.game, console.pipes, tmp_path, keyboard=console.keyboard)
    assert console.console_open_confirmed is False
    with pytest.raises(DataError): console.console_open_confirmed = True
    with pytest.raises(DataError): console.confirm_open_and_empty()
    assert len(env.writes) == 1


def test_fixed_playback_command_has_no_keyboard_input_and_checks_end(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    console.trigger_playback(16732)
    assert env.writes[-1] == b'unbind F8; demo_resume\n'
    console.schedule_playback_end(16732, deadline=env.time+1, input_guard=lambda: None)
    assert env.writes[-1] == b'demo_pauseatservertick 16732\n'
    for end in (True, 0, -1, 2147483648, '16732; quit'):
        with pytest.raises(DataError): console.trigger_playback(end)
        with pytest.raises(DataError):
            console.schedule_playback_end(end, deadline=env.time+1, input_guard=lambda: None)
    assert len(env.writes) == 3


def test_changed_game_identity_or_missing_insecure_revokes_ready_channel(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    console.game.verify = lambda: replace(identity, created=124)
    assert not console.input_ready()
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 1


def test_public_close_revokes_input_and_retains_actual_readback_interface(tmp_path):
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    console.reader = SimpleNamespace(readback=lambda: 'actual proof', snapshot=lambda: 'actual snapshot')
    assert console.readback() == 'actual proof' and console.snapshot() == 'actual snapshot'
    console.close(); console.close()
    with pytest.raises(DataError): console.command('demo_resume')
    assert len(env.writes) == 1 and console.state == 'closed'


def test_replay_guard_accepts_only_verified_pipe_without_faking_foreground(tmp_path):
    from cs2pov.services.replay import ReplayController
    console, env, identity = setup(tmp_path)
    controller = ReplayController(console.game, console, clock=lambda: env.time)
    with pytest.raises(DataError): controller.send('demo_pause')
    ready(console, env, identity)
    controller.send('demo_pause')
    assert console.foreground_pid() == 99 and env.writes[-1] == b'demo_pause\n'
    console.game.argv = ['cs2.exe']
    with pytest.raises(DataError): controller.send('demo_resume')
    assert len(env.writes) == 2


def test_pipe_hidden_playback_keeps_binding_queries_consumption_moving_and_final_empty(tmp_path):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path)
    ready(console, env, identity)
    clip = dict(start_tick=12602, end_tick=13400, server_start_tick=15934,
                server_end_tick=16732, player_id='76561199198478034', duration=12.46875)
    draft = dict(demo=str(tmp_path/'original.dem'), selection=clip)
    env.replay = ReplayEvidence(identity, True, tmp_path/'original.dem', 12602, 15934,
                               True, clip['player_id'], True, env.time)
    console.reader = SimpleNamespace(readback=lambda: env.replay, snapshot=lambda: env.replay)
    requests, checkpoints = [], []
    def factory(session, observed_identity, nonce, *, expected=None, clock):
        requests.append(nonce)
        return SimpleNamespace(readback=lambda: BindingEvidence(identity, 'F8', expected or '', env.time, nonce))
    controller = ReplayController(console.game, console, clock=lambda: env.time)
    playback = HiddenPlayback(controller, tmp_path, draft, clock=lambda: env.time,
        reader_factory=factory, checkpoint=lambda: checkpoints.append((playback.state, playback.triggered)))
    playback.begin(); playback.poll(); playback.poll()
    assert playback.state == 'ready' and playback.binding_may_exist
    assert not console.console_open_confirmed and console.foreground_pid() == 99
    playback.request_play(); playback.poll()
    assert not playback.triggered and playback.state == 'awaiting_foreground'
    console.keyboard.foreground_pid = lambda: identity.pid
    playback.poll()
    assert playback.triggered and playback.state == 'playing'
    assert checkpoints[-1] == ('playing', True)
    assert playback.binding == 'unbind F8; demo_resume'
    assert env.writes[-1] == b'unbind F8; demo_resume\n'
    env.time += .5
    env.replay = replace(env.replay, tick=12630, server_tick=15962, paused=False, observed_at=env.time)
    playback.poll()
    assert playback.pipe_end_attempted and env.writes[-1] == b'demo_pauseatservertick 16732\n'
    env.time += 12
    env.replay = replace(env.replay, tick=13400, server_tick=16732, paused=True, observed_at=env.time)
    assert playback.poll() == env.replay and playback.state == 'ended'
    playback.confirm_end_console(); playback.poll()
    assert playback.state == 'verified' and not playback.binding_may_exist
    assert len(requests) == len(set(requests)) == 3
    with pytest.raises(DataError): playback.request_play()


def test_pipe_cannot_claim_end_without_observed_motion(tmp_path):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path); ready(console, env, identity)
    clip = dict(start_tick=10, end_tick=20, server_start_tick=30, server_end_tick=40,
                player_id='76561199198478034', duration=1.)
    draft = dict(demo=str(tmp_path/'original.dem'), selection=clip)
    proof = ReplayEvidence(identity, True, tmp_path/'original.dem', 20, 40, True,
                           clip['player_id'], True, env.time)
    console.reader = SimpleNamespace(snapshot=lambda: proof, readback=lambda: proof)
    playback = HiddenPlayback(ReplayController(console.game, console, clock=lambda: env.time),
                              tmp_path, draft, clock=lambda: env.time)
    playback.state = 'playing'; playback.started_at = 99.; playback.deadline = 110.
    with pytest.raises(DataError, match='实际播放'): playback.poll()
    assert playback.state == 'failed' and len(env.writes) == 1


@pytest.mark.parametrize('delay', ['checkpoint','ledger'])
def test_pipe_playback_rechecks_current_start_after_each_persistence_boundary(tmp_path, delay):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path); ready(console,env,identity)
    console.keyboard.foreground_pid = lambda: 42
    clip = dict(start_tick=10,end_tick=20,server_start_tick=30,server_end_tick=40,
                player_id='76561199198478034',duration=1.)
    draft = dict(demo=str(tmp_path/'original.dem'),selection=clip)
    proof = ReplayEvidence(identity,True,tmp_path/'original.dem',10,30,True,
                           clip['player_id'],True,100. if delay=='checkpoint' else 95.1)
    console.reader = SimpleNamespace(readback=lambda: proof,snapshot=lambda: proof)
    playback = HiddenPlayback(ReplayController(console.game,console,clock=lambda:env.time),
                              tmp_path,draft,clock=lambda:env.time)
    playback.state='awaiting_foreground'; playback.deadline=130.
    if delay=='checkpoint':
        playback.checkpoint=lambda: setattr(env,'time',env.time+6)
    else:
        original=console.persist
        def delayed(*args):
            original(*args); env.time+=.2
        console.persist=delayed
    with pytest.raises(DataError): playback.poll()
    assert playback.triggered and playback.state=='failed' and len(env.writes)==1


@pytest.mark.parametrize('boundary', ['initial_reader', 'install_reader', 'install_ledger'])
def test_hidden_query_cannot_refresh_expired_input_authority_with_fresh_replay(tmp_path, boundary):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path); ready(console, env, identity)
    clip = dict(start_tick=10, end_tick=20, server_start_tick=30, server_end_tick=40,
                player_id='76561199198478034', duration=1.)
    draft = dict(demo=str(tmp_path/'original.dem'), selection=clip)
    proof = ReplayEvidence(identity, True, tmp_path/'original.dem', 10, 30,
                           True, clip['player_id'], True, env.time)
    # Continuous current replay telemetry must not renew the earlier F8 query.
    console.reader = SimpleNamespace(readback=lambda: replace(proof, observed_at=env.time))
    def factory(session, actual_identity, nonce, *, expected=None, clock):
        if (boundary == 'initial_reader' and expected is None
                or boundary == 'install_reader' and expected is not None):
            env.time += 6
        at = env.time
        return SimpleNamespace(readback=lambda: BindingEvidence(identity, 'F8', expected or '', at, nonce))
    playback = HiddenPlayback(ReplayController(console.game, console, clock=lambda: env.time),
        tmp_path, draft, clock=lambda: env.time, reader_factory=factory)
    before = len(env.writes)
    if boundary == 'initial_reader':
        with pytest.raises(DataError): playback.begin()
    else:
        playback.begin()
        before = len(env.writes)
        if boundary == 'install_ledger':
            env.time += 4.9
            original = console.persist
            def slow(*args):
                original(*args); env.time += .2
            console.persist = slow
        with pytest.raises(DataError): playback.poll()
        assert playback.state == 'failed'
    assert len(env.writes) == before and not playback.triggered
    assert not any(value.startswith(b'bind "F8"') for value in env.writes)
    playback.poll()
    assert len(env.writes) == before


def test_pipe_trigger_ledger_cannot_cross_original_foreground_deadline(tmp_path):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path); ready(console, env, identity)
    console.keyboard.foreground_pid = lambda: identity.pid
    clip = dict(start_tick=10, end_tick=20, server_start_tick=30, server_end_tick=40,
                player_id='76561199198478034', duration=1.)
    draft = dict(demo=str(tmp_path/'original.dem'), selection=clip)
    env.time = 129.9
    proof = ReplayEvidence(identity, True, tmp_path/'original.dem', 10, 30,
                           True, clip['player_id'], True, env.time)
    console.reader = SimpleNamespace(readback=lambda: proof, snapshot=lambda: proof)
    playback = HiddenPlayback(ReplayController(console.game, console, clock=lambda: env.time),
        tmp_path, draft, clock=lambda: env.time)
    playback.state = 'awaiting_foreground'; playback.deadline = 130.
    original = console.persist
    def slow(*args):
        original(*args); env.time += .2
    console.persist = slow
    with pytest.raises(DataError): playback.poll()
    assert playback.triggered and playback.state == 'failed' and len(env.writes) == 1
    playback.poll()
    assert len(env.writes) == 1


@pytest.mark.parametrize('boundary', ['checkpoint', 'ledger'])
def test_pipe_playback_rechecks_real_foreground_after_persistence(tmp_path, boundary):
    from cs2pov.services.hidden_playback import HiddenPlayback
    from cs2pov.services.replay import ReplayController, ReplayEvidence
    console, env, identity = setup(tmp_path); ready(console, env, identity)
    env.foreground = identity.pid
    console.keyboard.foreground_pid = lambda: env.foreground
    clip = dict(start_tick=10, end_tick=20, server_start_tick=30, server_end_tick=40,
                player_id='76561199198478034', duration=1.)
    draft = dict(demo=str(tmp_path/'original.dem'), selection=clip)
    proof = ReplayEvidence(identity, True, tmp_path/'original.dem', 10, 30,
                           True, clip['player_id'], True, env.time)
    console.reader = SimpleNamespace(readback=lambda: proof, snapshot=lambda: proof)
    playback = HiddenPlayback(ReplayController(console.game, console, clock=lambda: env.time),
        tmp_path, draft, clock=lambda: env.time)
    playback.state = 'awaiting_foreground'; playback.deadline = 130.
    if boundary == 'checkpoint':
        playback.checkpoint = lambda: setattr(env, 'foreground', 99)
    else:
        original = console.persist
        def lose_focus(*args):
            original(*args); env.foreground = 99
        console.persist = lose_focus
    with pytest.raises(DataError): playback.poll()
    assert playback.triggered and playback.state == 'failed' and len(env.writes) == 1
    playback.poll()
    assert len(env.writes) == 1
