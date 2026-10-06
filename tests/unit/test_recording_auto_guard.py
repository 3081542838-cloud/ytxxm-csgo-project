"""Fresh state guards prevent NVIDIA toggle inversion and consumed retries."""
from dataclasses import replace

import pytest

from cs2pov.services.recording import RecordingError, RecordingSession
from tests.unit.test_recording import fixture


def guarded_fixture(tmp_path, callback):
    tmp_path.mkdir(parents=True, exist_ok=True)
    legacy, start, end, clock, hotkey, checks = fixture(tmp_path)
    saved = []
    recorder = RecordingSession(legacy.output_directory, legacy.hotkey,
        nvidia_path_confirmed=True, draft=legacy.input_draft,
        expected_identity=legacy.expected_identity, game=legacy.game,
        preview_reader=legacy.preview_reader, end_reader=legacy.end_reader,
        checkpoint=saved.append, disk_checker=legacy.disk_checker,
        hotkey_adapter=hotkey, clock=clock, input_guard=callback)
    recorder.checkpoints = saved
    return recorder, start, end, clock, hotkey


def start_ready(recorder, start):
    recorder.arm(start)
    recorder.confirm_not_recording(step_token=recorder.confirmation_token)


def stop_ready(recorder, start, end, clock):
    start_ready(recorder, start)
    recorder.send_start()
    recorder.confirm_started(step_token=recorder.confirmation_token)
    clock.advance(1)
    recorder.mark_end(replace(end, observed_at=clock()))


def test_input_state_is_checked_before_consumed_checkpoint_and_again_before_input(tmp_path):
    calls = []
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path,
        lambda expected: calls.append((expected, recorder.state, recorder.triggered)) or True)
    start_ready(recorder, start)
    recorder.send_start()
    recorder.confirm_started(step_token=recorder.confirmation_token)
    clock.advance(1); recorder.mark_end(replace(end, observed_at=clock()))
    recorder.send_stop()
    assert calls == [('idle', 'start_ready', 'none'), ('idle', 'start_pending', 'start'),
                     ('recording', 'stop_ready', 'start'), ('recording', 'stop_pending', 'stop')]
    assert len(hotkey.sent) == 2


@pytest.mark.parametrize('invalid', [False, None, 1, 'recording'])
def test_unknown_or_nonboolean_start_guard_never_toggles(tmp_path, invalid):
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path, lambda expected: invalid)
    start_ready(recorder, start)
    with pytest.raises(RecordingError, match='NVIDIA|状态'):
        recorder.send_start()
    assert recorder.state == 'start_ready' and recorder.triggered == 'none' and hotkey.sent == []


def test_user_manual_stop_cannot_turn_automatic_stop_into_reverse_start_toggle(tmp_path):
    actual = ['idle']
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path,
        lambda expected: actual[0] == expected)
    start_ready(recorder, start); recorder.send_start()
    actual[0] = 'recording'; recorder.confirm_started(step_token=recorder.confirmation_token)
    clock.advance(1); recorder.mark_end(replace(end, observed_at=clock()))
    actual[0] = 'idle'  # User stopped through NVIDIA while the Demo was playing.
    with pytest.raises(RecordingError, match='NVIDIA|状态'):
        recorder.send_stop()
    assert len(hotkey.sent) == 1 and recorder.stop_attempted_at is None


@pytest.mark.parametrize('phase', ['start', 'stop'])
def test_guard_change_after_durable_consumption_becomes_unknown_without_input_or_retry(tmp_path, phase):
    calls = []
    def guard(expected):
        calls.append(expected)
        if expected == ('idle' if phase == 'start' else 'recording'):
            return len([value for value in calls if value == expected]) == 1
        return True
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path, guard)
    if phase == 'start': start_ready(recorder, start)
    else: stop_ready(recorder, start, end, clock)
    method = recorder.send_start if phase == 'start' else recorder.send_stop
    prior_inputs = len(hotkey.sent)
    with pytest.raises(RecordingError): method()
    assert recorder.state == 'unknown' and recorder.triggered == phase
    assert len(hotkey.sent) == prior_inputs
    consumed = [row for row in recorder.checkpoints if row.state == phase + '_pending']
    assert len(consumed) == 1
    with pytest.raises(RecordingError): method()
    assert len(hotkey.sent) == prior_inputs


def test_guard_exception_and_invalid_configuration_are_clear_recording_errors(tmp_path):
    def fail(expected): raise RuntimeError('readonly sample no longer fresh')
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path, fail)
    start_ready(recorder, start)
    with pytest.raises(RecordingError, match='NVIDIA|状态'):
        recorder.send_start()
    assert hotkey.sent == []
    with pytest.raises(RecordingError): guarded_fixture(tmp_path / 'bad', True)


def test_automatic_guard_is_readonly_and_cannot_be_downgraded_to_manual_mode(tmp_path):
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path, lambda expected: True)
    assert callable(recorder.input_guard)
    with pytest.raises(AttributeError): recorder.input_guard = None


@pytest.mark.parametrize('phase', ['start', 'stop'])
def test_slow_state_guard_does_not_authorize_input_with_aged_replay_proof(tmp_path, phase):
    def guard(expected):
        if recorder.state == phase + '_pending': clock.advance(5.01)
        return True
    recorder, start, end, clock, hotkey = guarded_fixture(tmp_path, guard)
    if phase == 'start': start_ready(recorder, start)
    else: stop_ready(recorder, start, end, clock)
    method = recorder.send_start if phase == 'start' else recorder.send_stop
    prior = len(hotkey.sent)
    with pytest.raises(RecordingError): method()
    assert recorder.state == 'unknown' and len(hotkey.sent) == prior
