from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import ReplayLogReader, ReplaySnapshot
from cs2pov.services.hidden_playback import HiddenPlayback
from cs2pov.services.replay import ReplayController, ReplayEvidence, ReplayError


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


class Reader:
    def __init__(self, evidence):
        self.evidence = evidence

    def readback(self):
        return self.evidence


@pytest.mark.parametrize('step', ['install','trigger'])
def test_slow_consumed_checkpoint_cannot_use_expired_start_proof(tmp_path, step):
    playback, controller, console, *_ = fixture(tmp_path)
    playback.begin()
    if step == 'trigger':
        playback.poll(); playback.poll(); playback.request_play()
    playback.checkpoint = lambda: controller.clock.advance(6)
    before = len(console.commands)
    with pytest.raises(ReplayError): playback.poll()
    assert playback.state == 'failed' and console.keys == 0
    assert len(console.commands) == before
    if step == 'trigger': assert playback.triggered is True


def fixture(tmp_path, *, binding_values=('', 'installed', ''), clock=None):
    clock = clock or Clock()
    demo = tmp_path / 'sample.dem'
    demo.write_bytes(b'unchanged')
    identity = ProcessIdentity(42, 123, 'C:/game/cs2.exe')
    selection = dict(start_tick=10, end_tick=20, server_start_tick=100,
                     server_end_tick=110, player_id='101', duration=1.0)
    draft = {'demo': str(demo), 'selection': selection}
    start = ReplayEvidence(identity, True, demo, 10, 100, True, '101', True, 100.0)
    end = replace(start, tick=20, server_tick=110)
    moving = ReplaySnapshot(identity, demo, True, 12, False, '101', True, 101.0)
    ended = ReplaySnapshot(identity, demo, True, 20, True, '101', True, 102.0)
    # The start proof comes from ReplayController; playback snapshots begin
    # only after the one-shot F8 has been delivered.
    snapshots = iter([moving, ended])
    values = list(binding_values)
    sent = []
    console = SimpleNamespace(console_open_confirmed=True, foreground=42,
                              commands=sent, hidden=0, keys=0)
    console.foreground_pid = lambda: console.foreground
    console.command = sent.append
    console.hide_console = lambda: (setattr(console, 'hidden', console.hidden + 1),
                                    setattr(console, 'console_open_confirmed', False))
    console.press_play_key = lambda: setattr(console, 'keys', console.keys + 1)
    console.snapshot = lambda: next(snapshots)

    game = SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=lambda: identity)
    calls = []

    class Controller:
        def __init__(self):
            self.game = game
            self.console = console
            self.state = 'preview_verified'
            self.clock = clock

        def guard(self):
            if console.foreground != identity.pid:
                raise ReplayError('前台失焦')
            if '-insecure' not in game.argv:
                raise ReplayError('insecure')
            return identity

        def verify_result(self, _draft, *, since, at_end=False,
                          require_foreground=True):
            if require_foreground:
                self.guard()
            proof = end if at_end else start
            if proof.observed_at < since or clock() - proof.observed_at > 5:
                raise ReplayError('证据过期')
            calls.append(('verify', at_end, require_foreground))
            return proof

    controller = Controller()

    def readers(_session, current_identity, nonce, *, expected=None, clock):
        value = values.pop(0)
        if value == 'installed':
            value = 'unbind F8; demo_resume; demo_pauseatservertick 110'
        evidence = SimpleNamespace(process_identity=current_identity, key='F8', value=value,
                                   observed_at=clock(), request_nonce=nonce)
        reader = Reader(evidence)
        calls.append(('query', expected, nonce))
        return reader

    playback = HiddenPlayback(controller, tmp_path, draft, clock=clock,
                              reader_factory=readers)
    return playback, controller, console, clock, calls, moving, ended


def arm_ready(playback, clock):
    playback.begin()
    playback.poll()  # empty binding query and install query are submitted
    playback.poll()  # exact install proof, then hide console
    assert playback.state == 'ready'
    playback.request_play()


def test_hidden_playback_requires_unused_f8_and_proves_end_before_unbind(tmp_path):
    playback, controller, console, clock, calls, moving, ended = fixture(tmp_path)
    arm_ready(playback, clock)
    assert console.keys == 0 and console.hidden == 1
    assert 'bind "F8" "unbind F8; demo_resume; demo_pauseatservertick 110"' in console.commands[1]

    playback.poll()  # sends the one-shot key
    assert console.keys == 1
    clock.advance(1)
    playback.poll()  # real movement
    clock.advance(1)
    playback.poll()  # exact paused endpoint
    assert playback.state == 'ended'
    console.console_open_confirmed = True
    playback.confirm_end_console()
    assert playback.state == 'querying_unbound'
    playback.poll()
    assert playback.state == 'verified'
    assert playback.binding_may_exist is False
    assert console.keys == 1
    query_expectations = [value for kind, value, _ in calls if kind == 'query']
    assert len(query_expectations) == 3
    assert query_expectations[0] is None and query_expectations[1] == playback.binding
    assert query_expectations[2] is None


def test_existing_f8_is_never_overwritten(tmp_path):
    playback, _, console, _, _, _, _ = fixture(tmp_path, binding_values=('slot1',))
    playback.begin()
    with pytest.raises(ReplayError, match='占用'):
        playback.poll()
    assert playback.state == 'failed'
    assert not any('bind "F8" "unbind' in value for value in console.commands)
    assert console.keys == 0


def test_wrong_binding_identity_or_nonce_blocks_install_and_play(tmp_path):
    playback, controller, console, clock, calls, _, _ = fixture(tmp_path)
    playback.begin()
    original = playback.reader_factory

    def wrong_reader(session, identity, nonce, *, expected=None, clock):
        reader = original(session, identity, nonce, expected=expected, clock=clock)
        evidence = reader.evidence
        reader.evidence = SimpleNamespace(process_identity=ProcessIdentity(42, 124, 'C:/game/cs2.exe'),
                                           key=evidence.key, value=evidence.value,
                                           observed_at=evidence.observed_at,
                                           request_nonce='0' * 32)
        return reader

    playback.reader_factory = wrong_reader
    playback.poll()
    with pytest.raises(ReplayError, match='身份、请求'):
        playback.poll()
    assert playback.state == 'failed' and console.keys == 0


def test_checkpoint_failure_after_install_query_keeps_unknown_binding_flag(tmp_path):
    playback, _, console, clock, _, _, _ = fixture(tmp_path)
    calls = {'count': 0}

    def checkpoint():
        calls['count'] += 1
        if calls['count'] == 1:
            raise OSError('checkpoint unavailable')

    playback.checkpoint = checkpoint
    playback.begin()
    with pytest.raises(OSError, match='checkpoint'):
        playback.poll()
    assert playback.state == 'failed'
    assert playback.binding_may_exist is True
    assert console.keys == 0


def test_focus_loss_waits_without_sending_f8_and_timeout_does_not_retry(tmp_path):
    playback, _, console, clock, _, _, _ = fixture(tmp_path)
    arm_ready(playback, clock)
    console.foreground = 999
    playback.poll()
    assert playback.state == 'awaiting_foreground' and console.keys == 0
    clock.advance(31)
    with pytest.raises(ReplayError, match='超时'):
        playback.poll()
    assert playback.state == 'failed' and console.keys == 0


def test_end_without_observed_movement_is_not_accepted(tmp_path):
    playback, _, console, clock, _, _, _ = fixture(tmp_path)
    arm_ready(playback, clock)
    # Replace the first moving observation with an endpoint snapshot.
    console.snapshot = lambda: ReplaySnapshot(ProcessIdentity(42, 123, 'C:/game/cs2.exe'),
                                               Path(tmp_path / 'sample.dem'), True,
                                               20, True, '101', True, 101.0)
    playback.poll()  # sends the key
    clock.advance(1)
    with pytest.raises(ReplayError, match='实际播放'):
        playback.poll()
    assert playback.state == 'failed' and console.keys == 1


def test_play_is_one_shot_even_if_requested_twice(tmp_path):
    playback, _, console, clock, _, _, _ = fixture(tmp_path)
    arm_ready(playback, clock)
    playback.poll()
    assert console.keys == 1
    with pytest.raises(ReplayError):
        playback.request_play()


def test_foreground_wait_keeps_reading_fresh_start_without_sending_a_key(tmp_path):
    from cs2pov.services.replay import ReplayController
    playback,stub,console,clock,_,_,_=fixture(tmp_path)
    proof=ReplayEvidence(stub.game.verify(),True,Path(playback.draft['demo']),10,100,
                         True,'101',True,clock())
    reads=[]
    def read():
        reads.append(clock())
        return replace(proof,observed_at=clock())
    console.readback=read
    playback.controller=ReplayController(stub.game,console,clock=clock)
    arm_ready(playback,clock);console.foreground=999
    for _ in range(24):
        clock.advance(.5);playback.poll()
        assert playback.state=='awaiting_foreground' and console.keys==0
    assert len(reads)>=27
    console.foreground=42;playback.poll()
    assert playback.state=='playing' and console.keys==1


def test_foreground_wait_rejects_changed_preview_before_any_input(tmp_path):
    playback,controller,console,clock,_,_,_=fixture(tmp_path)
    arm_ready(playback,clock);console.foreground=999;clock.advance(6)
    with pytest.raises(ReplayError,match='证据过期'):playback.poll()
    assert playback.state=='failed' and console.keys==0


def pending_query_with_real_replay_reader(tmp_path, phase):
    """Only binding replies and keyboard insertion are fake; state is log bytes."""
    playback, stub, console, clock, _, _, _ = fixture(tmp_path)
    target = '76561199198478034'
    playback.draft['selection']['player_id'] = target
    nonce = 'REAL_PLAYBACK_READER_01'
    log = tmp_path / 'engine.log'
    log.write_bytes(b'')
    # This fractional source clock makes the first unread half-second report
    # older than five seconds after a 4.8s batch, despite the query deadline.
    origin = datetime(2026, 10, 3, 12, 0, 0).timestamp() + 0.4
    origin_mono = clock()
    source = SimpleNamespace(sequence=0, tick=10, server=100, paused=True, mode=2)
    reader = ReplayLogReader(tmp_path, stub.game.verify(), nonce, clock=clock,
        wall=lambda: origin + clock() - origin_mono)
    console.readback = reader.readback
    console.snapshot = reader.snapshot
    controller = ReplayController(stub.game, console, clock=clock)
    playback.controller = controller

    def append_state(*, anchor=False, unpause=False):
        stamp = datetime.fromtimestamp(origin + clock() - origin_mono).strftime('%m/%d %H:%M:%S')
        source.sequence += 1
        payload = dict(nonce=nonce, sequence=source.sequence, context='HudDemoController',
            state=dict(sFileName=playback.draft['demo'], nTick=source.tick,
                bIsPaused=source.paused, nObserverMode=source.mode, nSpectatingPlayerId=1288,
                bIsPlayingDemoFile=True, bIsPlayingBroadcast=False), xuid=target)
        bodies = []
        if unpause:
            bodies.append(f'CGameRules - unpaused on tick {source.server}, pause duration was 0 ticks')
        if anchor:
            bodies.extend((f'CGameRules - paused on tick {source.server}',
                f'[Demo] Demo paused at engine time {source.server}, demo tick {source.tick}'))
        bodies.append('[PanoramaScript] POV_READBACK ' + json.dumps(payload, ensure_ascii=False))
        with log.open('a', encoding='utf-8', newline='\n') as stream:
            for body in bodies:
                stream.write(f'{stamp} {body}\n')

    append_state(anchor=True)
    playback.begin()
    if phase in ('querying_installed', 'querying_unbound'):
        playback.poll()
    if phase == 'querying_unbound':
        playback.poll()
        assert playback.state == 'ready'
        playback.request_play()
        playback.poll()
        assert playback.triggered and console.keys == 1
        clock.advance(1)
        source.tick, source.paused = 12, False
        append_state(unpause=True)
        playback.poll()
        assert playback.moving_seen
        clock.advance(1)
        source.tick, source.server, source.paused = 20, 110, True
        append_state(anchor=True)
        assert playback.poll().tick == 20 and playback.state == 'ended'
        console.console_open_confirmed = True
        playback.confirm_end_console()
    assert playback.state == phase
    reply = playback.reader.evidence
    playback.reader.evidence = None  # A delayed binding response, not missing live state.
    return playback, console, clock, reader, source, append_state, reply


@pytest.mark.parametrize('phase', ['querying_empty', 'querying_installed', 'querying_unbound'])
def test_binding_query_wait_continuously_reads_real_state_and_sends_no_extra_input(tmp_path, phase):
    playback, console, clock, reader, source, append_state, reply = pending_query_with_real_replay_reader(tmp_path, phase)
    commands, keys, hidden = tuple(console.commands), console.keys, console.hidden
    nonce = playback.request_nonce
    since = clock()
    for delay in [0.5] * 9 + [0.3]:
        clock.advance(delay)
        append_state()
        assert playback.poll() is None
        assert playback.state == phase and playback.request_nonce == nonce
        # Inspect decoder only: calling reader.readback here would mask a
        # product that forgot to drain its real cursor while querying.
        proof = reader.decoder.readback()
        assert proof is not None and (proof.tick, proof.server_tick) == (source.tick, source.server)
        assert proof.player_id == '76561199198478034' and proof.first_person and proof.paused
        assert reader.cursor.offset == (tmp_path / 'engine.log').stat().st_size
        assert tuple(console.commands) == commands and console.keys == keys and console.hidden == hidden
    assert clock() - since == pytest.approx(4.8)
    playback.reader.evidence = SimpleNamespace(**{**vars(reply), 'observed_at': clock()})
    result = playback.poll()
    expected = dict(querying_empty='querying_installed', querying_installed='ready', querying_unbound='verified')
    assert playback.state == expected[phase]
    assert console.keys == keys
    if phase == 'querying_empty':
        assert len(console.commands) == len(commands) + 1 and playback.binding_may_exist
    elif phase == 'querying_installed':
        assert tuple(console.commands) == commands and console.hidden == hidden + 1
    else:
        assert result.tick == 20 and result.server_tick == 110 and not playback.binding_may_exist
        assert tuple(console.commands) == commands


@pytest.mark.parametrize('phase', ['querying_empty', 'querying_installed', 'querying_unbound'])
def test_binding_query_event_loop_gap_over_five_seconds_fails_without_retry(tmp_path, phase):
    playback, console, clock, reader, source, append_state, reply = pending_query_with_real_replay_reader(tmp_path, phase)
    before = tuple(console.commands), console.keys, console.hidden
    # A fresh last report and binding reply cannot undo an expired operation.
    clock.advance(5.01)
    append_state()
    playback.reader.evidence = SimpleNamespace(**{**vars(reply), 'observed_at': clock()})
    with pytest.raises(ReplayError, match='超时'):
        playback.poll()
    assert playback.state == 'failed' and not console.console_open_confirmed
    assert (tuple(console.commands), console.keys, console.hidden) == before
    assert reader.decoder.readback() is None
    playback.poll()
    assert (tuple(console.commands), console.keys, console.hidden) == before


@pytest.mark.parametrize('phase', ['querying_empty', 'querying_installed', 'querying_unbound'])
def test_binding_query_rejects_changed_first_person_before_a_binding_reply(tmp_path, phase):
    playback, console, clock, reader, source, append_state, _ = pending_query_with_real_replay_reader(tmp_path, phase)
    before = tuple(console.commands), console.keys, console.hidden
    clock.advance(0.5)
    source.mode = 3
    append_state()
    with pytest.raises(ReplayError, match='第一人称回读'):
        playback.poll()
    assert playback.state == 'failed' and not console.console_open_confirmed
    assert (tuple(console.commands), console.keys, console.hidden) == before
