from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cs2pov.adapters.disk import DiskSnapshot, MIN_VIDEO_BYTES, DiskError
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.services.recording import RecordingError, RecordingSession
from cs2pov.services.replay import ReplayEvidence


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Hotkey:
    def __init__(self, fail=False): self.sent = []; self.fail = fail; self.on_send = None
    def send_once(self, key, game=None, *, expected_identity):
        assert game.verify() == expected_identity
        if self.fail: raise OSError('input result unknown')
        self.sent.append((key, game))
        if self.on_send is not None: self.on_send()


def fixture(tmp_path, *, disk_free=MIN_VIDEO_BYTES + 1, fail=False):
    clock = Clock(); path = tmp_path / 'demo.dem'; path.write_bytes(b'demo')
    identity = ProcessIdentity(42, 123, 'C:/game/cs2.exe')
    game = SimpleNamespace(argv=['cs2.exe', '-insecure'], verify=lambda: identity)
    start = ReplayEvidence(identity, True, path, 10, 100, True, '101', True, 100.)
    end = replace(start, tick=20, server_tick=110, observed_at=101.)
    sha = hashlib.sha256(b'demo').hexdigest()
    draft = dict(demo=str(path), fingerprint={'sha256': sha}, selection=dict(
        player_id='101', start_tick=10, end_tick=20, server_start_tick=100,
        server_end_tick=110, content_sha256=sha))
    checks = []
    def disk(directory, required=MIN_VIDEO_BYTES):
        checks.append((Path(directory), required))
        if disk_free < required: raise DiskError('below 10GB')
        return DiskSnapshot(Path(directory), disk_free, required)
    hotkey = Hotkey(fail=fail)
    checkpoints = []
    session = RecordingSession(tmp_path / 'videos', 'Alt+F9', nvidia_path_confirmed=True,
                               draft=draft, expected_identity=identity,
                               disk_checker=disk, hotkey_adapter=hotkey, game=game,
                               clock=clock, checkpoint=checkpoints.append,
                               preview_reader=lambda: replace(start, observed_at=clock()),
                               end_reader=lambda: replace(end, observed_at=clock()))
    session.checkpoints = checkpoints
    session.input_draft = draft  # Caller-owned value, deliberately mutable for freeze tests.
    return session, start, end, clock, hotkey, checks


def ready_recording(session, start, end):
    session.arm(start)
    session.confirm_not_recording(step_token=session.confirmation_token)
    session.send_start()
    session.confirm_started(step_token=session.confirmation_token)
    session.clock.advance(1)
    end = replace(end, observed_at=session.clock())
    session.mark_end(end)
    session.send_stop()
    session.confirm_stopped(step_token=session.confirmation_token)


def test_recording_requires_two_explicit_confirmations_and_sends_each_hotkey_once(tmp_path):
    session, start, end, clock, hotkey, checks = fixture(tmp_path)
    session.arm(start)
    assert session.state == 'awaiting_not_recording' and hotkey.sent == []
    with pytest.raises(RecordingError): session.send_start()
    session.confirm_not_recording(step_token=session.confirmation_token); session.send_start()
    assert session.state == 'awaiting_started' and len(hotkey.sent) == 1
    session.confirm_started(step_token=session.confirmation_token); assert session.started_at == clock.value
    clock.advance(1)
    session.mark_end(end); session.send_stop()
    assert session.state == 'awaiting_stopped' and len(hotkey.sent) == 2
    session.confirm_stopped(step_token=session.confirmation_token)
    assert session.state == 'stopped' and len(checks) == 3


def test_proven_zero_delivery_cancels_without_unknown_recovery_or_reauthorization(tmp_path):
    from cs2pov.adapters.nvidia import NvidiaNotSentError
    session, start, _, _, hotkey, _ = fixture(tmp_path)
    def fail(*args, **kwargs):
        raise NvidiaNotSentError('0/4')
    hotkey.send_once = fail
    session.arm(start)
    session.confirm_not_recording(step_token=session.confirmation_token)
    with pytest.raises(RecordingError, match='未启动'):
        session.send_start()
    assert session.state == 'cancelled' and session.triggered == 'none'
    assert session.start_attempted_at is not None and session.started_at is None
    assert session.start_not_sent is True and session.checkpoints[-1].start_not_sent is True
    restarted, _, _, _, new_hotkey, _ = fixture(tmp_path)
    restarted.restore_checkpoint(session.checkpoints[-1])
    assert restarted.state == 'cancelled' and restarted.start_not_sent is True
    with pytest.raises(RecordingError):
        restarted.send_start()
    assert new_hotkey.sent == []
    with pytest.raises(RecordingError):
        session.send_start()


def test_space_is_checked_again_before_start_and_changed_preview_blocks_hotkey(tmp_path):
    session, start, _, _, hotkey, checks = fixture(tmp_path)
    session.arm(start); session.confirm_not_recording(step_token=session.confirmation_token)
    session.preview_reader = lambda: replace(start, tick=11)
    with pytest.raises(RecordingError, match='已改变'):
        session.send_start()
    assert hotkey.sent == [] and len(checks) == 3

    session, start, _, _, hotkey, _ = fixture(tmp_path, disk_free=MIN_VIDEO_BYTES - 1)
    with pytest.raises(RecordingError): session.arm(start)
    assert session.state == 'idle' and hotkey.sent == []


def test_hotkey_failure_and_confirmation_timeout_are_unknown_and_not_retried(tmp_path):
    session, start, _, clock, hotkey, _ = fixture(tmp_path, fail=True)
    session.arm(start); session.confirm_not_recording(step_token=session.confirmation_token)
    with pytest.raises(RecordingError, match='未知'):
        session.send_start()
    assert session.state == 'unknown' and len(hotkey.sent) == 0
    with pytest.raises(RecordingError): session.send_start()

    session, start, _, clock, hotkey, _ = fixture(tmp_path)
    session.arm(start); session.confirm_not_recording(step_token=session.confirmation_token); session.send_start()
    clock.advance(61); session.poll()
    assert session.state == 'unknown' and len(hotkey.sent) == 1
    with pytest.raises(RecordingError): session.confirm_started(step_token=session.confirmation_token)


def test_cancel_before_start_is_safe_but_cancel_during_recording_is_unknown(tmp_path):
    session, start, end, _, hotkey, _ = fixture(tmp_path)
    session.arm(start); session.cancel()
    assert session.state == 'cancelled' and hotkey.sent == []

    session, start, end, _, hotkey, _ = fixture(tmp_path)
    session.arm(start); session.confirm_not_recording(step_token=session.confirmation_token); session.send_start(); session.confirm_started(step_token=session.confirmation_token)
    session.cancel()
    assert session.state == 'unknown' and hotkey.sent == [('Alt+F9', session.game)]


def test_end_player_or_demo_mismatch_is_blocked(tmp_path):
    session, start, end, _, hotkey, _ = fixture(tmp_path)
    session.arm(start); session.confirm_not_recording(step_token=session.confirmation_token); session.send_start(); session.confirm_started(step_token=session.confirmation_token)
    with pytest.raises(RecordingError): session.mark_end(replace(end, player_id='202'))
    assert session.state == 'recording' and len(hotkey.sent) == 1


def start_ready(session, start):
    session.arm(start)
    session.confirm_not_recording(step_token=session.confirmation_token)


def recording(session, start):
    start_ready(session, start)
    session.send_start()
    session.confirm_started(step_token=session.confirmation_token)


def stop_ready(session, start, end, clock):
    recording(session, start)
    clock.advance(1)
    session.mark_end(replace(end, observed_at=clock()))


def test_scope_and_process_identity_are_frozen_and_checkpoint_is_json_serializable(tmp_path):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    session.input_draft['demo'] = str(tmp_path/'changed.dem')
    session.input_draft['selection']['player_id'] = '202'
    session.input_draft['selection']['end_tick'] = 1000
    assert session.scope.demo == str(start.path) and session.scope.player_id == '101'
    assert session.scope.end_tick == 20
    with pytest.raises(FrozenInstanceError): session.scope.end_tick = 1000
    with pytest.raises(AttributeError): session.scope = session.scope
    with pytest.raises(AttributeError): session.expected_identity = replace(start.process_identity, created=124)
    with pytest.raises(AttributeError): session.hotkey = 'Alt+F10'
    with pytest.raises(AttributeError): session.output_directory = tmp_path/'other'
    ready_recording(session, start, end)
    value = json.loads(json.dumps(asdict(session.checkpoints[-1]), allow_nan=False))
    assert value['scope']['demo'] == str(start.path) and value['state'] == 'stopped'
    assert value['expected_identity']['created'] == 123 and len(hotkey.sent) == 2


PROOF_CHANGES = [
    {'process_identity': ProcessIdentity(42, 124, 'C:/game/cs2.exe')},
    {'local_demo': False}, {'local_demo': 'yes'}, {'paused': False}, {'paused': 1},
    {'first_person': False}, {'first_person': 'yes'}, {'player_id': '202'},
    {'path': Path('C:/other.dem')}, {'tick': 11}, {'tick': True}, {'server_tick': 101},
    {'observed_at': 94.99}, {'observed_at': 101.}, {'observed_at': float('nan')},
    {'observed_at': float('inf')}, {'observed_at': True},
]


@pytest.mark.parametrize('changes', PROOF_CHANGES)
@pytest.mark.parametrize('phase', ['arm', 'send_start'])
def test_invalid_or_stale_start_proof_never_sends_input(tmp_path, changes, phase):
    session, start, _, _, hotkey, _ = fixture(tmp_path)
    invalid = replace(start, **changes)
    if phase == 'arm':
        operation = lambda: session.arm(invalid)
    else:
        start_ready(session, start)
        session.preview_reader = lambda: invalid
        operation = session.send_start
    with pytest.raises(RecordingError, match='证据'):
        operation()
    assert hotkey.sent == [] and not any(cp.state == 'start_pending' for cp in session.checkpoints)


@pytest.mark.parametrize('phase', ['mark_end', 'send_stop'])
@pytest.mark.parametrize('change', ['start', 'wrong_server', 'old', 'future', 'before_recording', 'wrong_identity'])
def test_wrong_or_stale_endpoint_cannot_authorize_stop(tmp_path, phase, change):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    recording(session, start)
    clock.advance(10)
    correct = replace(end, observed_at=clock())
    invalid = {
        'start': replace(start, observed_at=clock()),
        'wrong_server': replace(correct, server_tick=109),
        'old': replace(correct, observed_at=clock()-5.01),
        'future': replace(correct, observed_at=clock()+.01),
        'before_recording': replace(correct, observed_at=session.started_at),
        'wrong_identity': replace(correct, process_identity=replace(start.process_identity, created=124)),
    }[change]
    if phase == 'mark_end':
        operation = lambda: session.mark_end(invalid)
    else:
        session.mark_end(correct)
        session.end_reader = lambda: invalid
        operation = session.send_stop
    with pytest.raises(RecordingError, match='端点证据'):
        operation()
    assert len(hotkey.sent) == 1 and not any(cp.state == 'stop_pending' for cp in session.checkpoints)


@pytest.mark.parametrize('state', ['awaiting_not_recording', 'awaiting_started', 'awaiting_stopped'])
@pytest.mark.parametrize('after_deadline', [0, 1])
def test_confirmation_checks_deadline_without_a_prior_poll(tmp_path, state, after_deadline):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    if state == 'awaiting_not_recording': session.arm(start)
    elif state == 'awaiting_started': start_ready(session, start); session.send_start()
    else: stop_ready(session, start, end, clock); session.send_stop()
    token = session.confirmation_token
    count = len(hotkey.sent)
    clock.value = session.deadline + after_deadline
    action = {'awaiting_not_recording': session.confirm_not_recording,
              'awaiting_started': session.confirm_started,
              'awaiting_stopped': session.confirm_stopped}[state]
    with pytest.raises(RecordingError, match='超时'): action(step_token=token)
    assert session.state == ('blocked' if count == 0 else 'unknown')
    assert session.confirmation_token is None and session.deadline is None
    assert len(hotkey.sent) == count
    with pytest.raises(RecordingError): action(step_token=token)
    assert len(hotkey.sent) == count


def test_start_ready_authorization_expires_without_input_or_poll(tmp_path):
    session, start, _, clock, hotkey, _ = fixture(tmp_path)
    start_ready(session, start)
    clock.value = session.deadline
    with pytest.raises(RecordingError, match='超时'): session.send_start()
    assert session.state == 'blocked' and hotkey.sent == []


def test_each_confirmation_uses_a_new_token_and_rejects_old_foreign_and_duplicate_tokens(tmp_path):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    other_path = tmp_path/'other'; other_path.mkdir()
    other, other_start, _, _, other_hotkey, _ = fixture(other_path)
    other.arm(other_start)
    foreign = other.confirmation_token
    session.arm(start)
    old_tokens = [foreign]
    steps = [session.confirm_not_recording, session.confirm_started, session.confirm_stopped]
    for index, action in enumerate(steps):
        token, deadline, state = session.confirmation_token, session.deadline, session.state
        for invalid in [*old_tokens, None, '']:
            with pytest.raises(RecordingError): action(step_token=invalid)
            assert session.state == state and session.deadline == deadline
        action(step_token=token)
        count = len(hotkey.sent)
        with pytest.raises(RecordingError): action(step_token=token)
        assert len(hotkey.sent) == count
        old_tokens.append(token)
        if index == 0: session.send_start()
        elif index == 1:
            clock.advance(1); session.mark_end(replace(end, observed_at=clock())); session.send_stop()
    assert session.state == 'stopped' and len(hotkey.sent) == 2 and other_hotkey.sent == []


@pytest.mark.parametrize('phase', ['send_start', 'confirm_started', 'mark_end', 'send_stop', 'poll'])
def test_process_identity_change_blocks_future_input_and_consumed_attempt_becomes_unknown(tmp_path, phase):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    start_ready(session, start)
    if phase != 'send_start': session.send_start()
    if phase in ('mark_end', 'send_stop', 'poll'):
        session.confirm_started(step_token=session.confirmation_token)
    if phase == 'send_stop':
        clock.advance(1); session.mark_end(replace(end, observed_at=clock()))
    session.game.verify = lambda: replace(start.process_identity, created=124)
    count = len(hotkey.sent)
    action = {'send_start': session.send_start,
              'confirm_started': lambda: session.confirm_started(step_token=session.confirmation_token),
              'mark_end': lambda: session.mark_end(end),
              'send_stop': session.send_stop, 'poll': session.poll}[phase]
    with pytest.raises(RecordingError): action()
    assert len(hotkey.sent) == count
    if count: assert session.state == 'unknown'


@pytest.mark.parametrize('stage', ['start', 'stop'])
@pytest.mark.parametrize('cut', ['pending_save_before_write', 'pending_save_after_write', 'after_input_crash', 'result_save'])
def test_toggle_is_consumed_and_durable_before_input_and_recovery_never_replays(tmp_path, stage, cut):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    if stage == 'start': start_ready(session, start)
    else: stop_ready(session, start, end, clock)
    pending = stage+'_pending'
    after = 'awaiting_started' if stage == 'start' else 'awaiting_stopped'
    durable = list(session.checkpoints)
    events = []
    def checkpoint(value):
        events.append(('checkpoint', value.state))
        if value.state == pending and cut == 'pending_save_before_write': raise OSError('no write')
        if value.state == after and cut == 'result_save': raise OSError('post-input disk full')
        durable.append(value)
        if value.state == pending and cut == 'pending_save_after_write': raise OSError('write succeeded then crashed')
    session.checkpoint = checkpoint
    def input_event():
        events.append(('input', pending))
        assert durable[-1].state == pending and durable[-1].triggered == stage
        assert durable[-1].confirmation_token is None
        if cut == 'after_input_crash': raise SystemExit('application terminated')
    hotkey.on_send = input_event
    before_count = len(hotkey.sent)
    expected = SystemExit if cut == 'after_input_crash' else RecordingError
    with pytest.raises(expected):
        (session.send_start if stage == 'start' else session.send_stop)()
    added = len(hotkey.sent)-before_count
    assert added == (1 if cut in ('after_input_crash', 'result_save') else 0)
    if added: assert events[0] == ('checkpoint', pending)
    with pytest.raises(RecordingError): (session.send_start if stage == 'start' else session.send_stop)()
    assert len(hotkey.sent) == before_count + added
    # Reconstruct the same frozen task without restoring any authorization.
    restarted, _, _, _, restarted_hotkey, _ = fixture(tmp_path)
    restarted.restore_checkpoint(durable[-1])
    assert restarted.state in ('unknown', 'cancelled')
    assert restarted.confirmation_token is None and restarted.deadline is None
    with pytest.raises(RecordingError): restarted.send_start()
    with pytest.raises(RecordingError): restarted.send_stop()
    assert restarted_hotkey.sent == []


@pytest.mark.parametrize('stage', ['start', 'stop'])
def test_slow_checkpoint_cannot_authorize_input_from_an_aged_proof(tmp_path, stage):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    if stage == 'start':
        start_ready(session, start)
        session.preview_reader = lambda: start
    else:
        stop_ready(session, start, end, clock)
        session.end_reader = lambda: replace(end, observed_at=clock.value_before_save)
    clock.value_before_save = clock()
    def slow(value):
        session.checkpoints.append(value)
        if value.state == stage+'_pending': clock.advance(6)
    session.checkpoint = slow
    count = len(hotkey.sent)
    with pytest.raises(RecordingError, match='未知'):
        (session.send_start if stage == 'start' else session.send_stop)()
    assert session.state == 'unknown' and len(hotkey.sent) == count


@pytest.mark.parametrize('phase', ['start', 'stop'])
def test_readback_exception_never_sends_the_next_toggle(tmp_path, phase):
    session, start, end, clock, hotkey, _ = fixture(tmp_path)
    def unreadable(): raise OSError('log cannot be read')
    if phase == 'start':
        start_ready(session, start); session.preview_reader = unreadable
    else:
        stop_ready(session, start, end, clock); session.end_reader = unreadable
    count = len(hotkey.sent)
    with pytest.raises(RecordingError, match='回读失败'):
        (session.send_start if phase == 'start' else session.send_stop)()
    assert len(hotkey.sent) == count


def test_missing_insecure_and_new_space_failure_block_the_start_toggle(tmp_path):
    session, start, _, _, hotkey, _ = fixture(tmp_path)
    start_ready(session, start)
    session.game.argv = ['cs2.exe']
    with pytest.raises(RecordingError): session.send_start()
    assert hotkey.sent == []
    session.game.argv.append('-insecure')
    def depleted(*_): raise DiskError('disk fell below 10GB')
    session.disk_checker = depleted
    with pytest.raises(RecordingError): session.send_start()
    assert hotkey.sent == []


@pytest.mark.parametrize('checkpoint_change', [
    {'stopped_at': None}, {'stopped_at': float('nan')}, {'stopped_at': True},
    {'stopped_at': 99.}, {'started_at': None}, {'start_attempted_at': None},
    {'stop_attempted_at': None}, {'triggered': 'none'},
])
def test_corrupt_stopped_checkpoint_cannot_claim_known_stopped_or_restore_input(tmp_path, checkpoint_change):
    session, start, end, _, _, _ = fixture(tmp_path)
    ready_recording(session, start, end)
    checkpoint = replace(session.checkpoints[-1], **checkpoint_change)
    restarted, _, _, _, hotkey, _ = fixture(tmp_path)
    restarted.restore_checkpoint(checkpoint)
    assert restarted.state == 'unknown' and restarted.confirmation_token is None
    with pytest.raises(RecordingError): restarted.send_stop()
    assert hotkey.sent == []


def test_valid_stopped_checkpoint_remains_a_result_but_has_no_input_authority(tmp_path):
    session, start, end, _, _, _ = fixture(tmp_path)
    ready_recording(session, start, end)
    restarted, _, _, _, hotkey, _ = fixture(tmp_path)
    restarted.restore_checkpoint(session.checkpoints[-1])
    assert restarted.state == 'stopped' and restarted.confirmation_token is None
    with pytest.raises(RecordingError): restarted.send_start()
    with pytest.raises(RecordingError): restarted.send_stop()
    assert hotkey.sent == []


@pytest.mark.parametrize('change', ['scope', 'identity', 'task_id'])
def test_recovery_rejects_checkpoint_for_a_different_or_malformed_task(tmp_path, change):
    session, start, _, _, _, _ = fixture(tmp_path)
    session.arm(start)
    checkpoint = session.checkpoints[-1]
    checkpoint = replace(checkpoint, **{
        'scope': {'scope': replace(checkpoint.scope, player_id='202')},
        'identity': {'expected_identity': replace(checkpoint.expected_identity, created=124)},
        'task_id': {'task_id': ''},
    }[change])
    restarted, _, _, _, hotkey, _ = fixture(tmp_path)
    with pytest.raises(RecordingError, match='检查点'): restarted.restore_checkpoint(checkpoint)
    assert restarted.state == 'idle' and hotkey.sent == []


@pytest.mark.parametrize('field,value', [('start_tick', True), ('end_tick', 10),
    ('server_end_tick', 100), ('player_id', ''), ('content_sha256', 'wrong')])
def test_invalid_frozen_scope_is_rejected_before_any_effect(tmp_path, field, value):
    session, _, _, _, hotkey, _ = fixture(tmp_path)
    draft = session.input_draft
    draft['selection'][field] = value
    with pytest.raises(RecordingError, match='任务'):
        RecordingSession(session.output_directory, session.hotkey, nvidia_path_confirmed=True,
            draft=draft, expected_identity=session.expected_identity, game=session.game,
            preview_reader=session.preview_reader, end_reader=session.end_reader,
            checkpoint=lambda _: None, hotkey_adapter=hotkey)
    assert hotkey.sent == []
