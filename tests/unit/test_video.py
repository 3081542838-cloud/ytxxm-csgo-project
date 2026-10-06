import json
import os
from pathlib import Path
import subprocess

import pytest

from cs2pov.adapters.video import (VideoError, candidates_since, probe_video,
                                   snapshot_directory, wait_stable)
import cs2pov.adapters.video as video_adapter
from cs2pov.storage.library import Library


def ffprobe_runner(payload, returncode=0):
    def run(*args, **kwargs):
        return type('Result', (), {'stdout': json.dumps(payload), 'returncode': returncode})()
    return run


def valid_payload():
    return {'streams': [
        {'codec_type': 'video', 'width': 1920, 'height': 1080},
        {'codec_type': 'audio'},
    ], 'format': {'duration': '12.5'}}


def test_snapshot_and_candidate_discovery_never_moves_or_includes_unrelated_files(tmp_path):
    before = tmp_path / 'old.mp4'; before.write_bytes(b'old')
    (tmp_path / 'notes.txt').write_text('keep')
    baseline = snapshot_directory(tmp_path)
    before.write_bytes(b'newer')
    fresh = tmp_path / 'new.mp4'; fresh.write_bytes(b'new')
    candidates = candidates_since(tmp_path, baseline)
    assert {item.path for item in candidates} == {before, fresh}
    assert before.read_bytes() == b'newer' and fresh.read_bytes() == b'new'


def test_stable_file_requires_nonzero_and_two_equal_signatures(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    now = [0.0]; sleeps = []
    def clock(): return now[0]
    def sleep(value): sleeps.append(value); now[0] += value
    result = wait_stable(path, timeout=2, interval=.5, clock=clock, sleep=sleep)
    assert result.bytes == 5 and sleeps == [.5]
    empty = tmp_path / 'empty.mp4'; empty.touch()
    with pytest.raises(VideoError): wait_stable(empty, clock=clock, sleep=sleep)


@pytest.mark.parametrize('payload', [
    {'streams': [{'codec_type': 'video', 'width': 1, 'height': 1}], 'format': {'duration': '1'}},
    {'streams': [{'codec_type': 'audio'}], 'format': {'duration': '1'}},
    {'streams': [{'codec_type': 'video', 'width': 0, 'height': 1}, {'codec_type': 'audio'}], 'format': {'duration': '1'}},
    {'streams': [{'codec_type': 'video', 'width': 1, 'height': 1}, {'codec_type': 'audio'}], 'format': {'duration': 'nan'}},
])
def test_probe_rejects_missing_streams_invalid_dimensions_or_duration(tmp_path, payload):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    with pytest.raises(VideoError): probe_video(path, runner=ffprobe_runner(payload))


def test_probe_accepts_one_video_and_audio_stream(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    metadata = probe_video(path, runner=ffprobe_runner(valid_payload()))
    assert (metadata.duration, metadata.width, metadata.height, metadata.audio_streams) == (12.5, 1920, 1080, 1)


def test_probe_rejects_ffprobe_failure_or_malformed_json(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    with pytest.raises(VideoError): probe_video(path, runner=ffprobe_runner({}, returncode=1))
    def malformed(*args, **kwargs):
        return type('Result', (), {'stdout': '{', 'returncode': 0})()
    with pytest.raises(VideoError): probe_video(path, runner=malformed)


def test_library_record_remove_keeps_video_file(tmp_path):
    library = Library(tmp_path / 'library.sqlite')
    video = tmp_path / 'clip.mp4'; video.write_bytes(b'video')
    library.save_record('r1', {'video': str(video), 'result': 'verified'}, 'complete')
    assert library.record('r1')['restore_status'] == 'complete'
    library.remove_record('r1')
    assert library.record('r1') is None and video.is_file()
    library.close()


def test_snapshot_includes_one_nvidia_game_subdirectory_and_has_bounded_depth(tmp_path):
    game = tmp_path / 'Counter-strike 2'; game.mkdir()
    clip = game / 'clip.mp4'; clip.write_bytes(b'video')
    result = snapshot_directory(tmp_path)
    assert result[clip].bytes == 5
    assert result[clip].identity is not None
    nested = game / 'too-deep'; nested.mkdir()
    (nested / 'hidden.mp4').write_bytes(b'do not silently omit')
    with pytest.raises(VideoError, match='层|深度'):
        snapshot_directory(tmp_path)


def test_snapshot_entry_and_directory_limits_fail_instead_of_partial_results(tmp_path):
    (tmp_path / 'one.mp4').write_bytes(b'1')
    (tmp_path / 'two.mp4').write_bytes(b'2')
    with pytest.raises(VideoError, match='数量|限额'):
        snapshot_directory(tmp_path, max_entries=1)
    (tmp_path / 'game').mkdir()
    with pytest.raises(VideoError, match='数量|限额'):
        snapshot_directory(tmp_path, max_directories=0)


def test_stability_waits_for_initial_empty_file_to_grow_with_same_identity(tmp_path):
    path = tmp_path / 'growing.mp4'; path.touch()
    expected = video_adapter.current_signature(path)
    now = [0.0]
    def sleep(value):
        now[0] += value
        if now[0] == .5:
            path.write_bytes(b'completed video')
    result = wait_stable(path, expected=expected, timeout=2, interval=.5,
                         clock=lambda: now[0], sleep=sleep)
    assert result.identity == expected.identity and result.bytes == 15
    assert now[0] == 1.0


def test_stability_continuous_write_and_cancel_have_fixed_deadline(tmp_path):
    path = tmp_path / 'growing.mp4'; path.write_bytes(b'1')
    now = [0.0]
    def sleep(value):
        now[0] += value
        with path.open('ab') as output:
            output.write(b'1')
    with pytest.raises(VideoError, match='写入|超时'):
        wait_stable(path, timeout=1, interval=.5, clock=lambda: now[0], sleep=sleep)
    assert now[0] <= 1.0
    with pytest.raises(VideoError, match='取消'):
        wait_stable(path, cancel=lambda: True)


def test_signature_rejects_path_outside_frozen_output_root(tmp_path):
    root = tmp_path / 'output'; root.mkdir()
    outside = tmp_path / 'outside.mp4'; outside.write_bytes(b'video')
    with pytest.raises(VideoError, match='目录|越界'):
        video_adapter.current_signature(outside, directory=root)


def test_wait_stable_rejects_identity_change_even_with_restored_size_and_mtime(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'first')
    expected = video_adapter.current_signature(path)
    replacement = tmp_path / 'replacement.mp4'; replacement.write_bytes(b'other')
    os.utime(replacement, ns=(expected.mtime_ns, expected.mtime_ns))
    os.replace(replacement, path)
    with pytest.raises(VideoError, match='身份|替换'):
        wait_stable(path, expected=expected)


def test_probe_returns_frozen_signature_and_rejects_earlier_stable_signature(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    stable = video_adapter.current_signature(path)
    metadata = probe_video(path, expected=stable, runner=ffprobe_runner(valid_payload()))
    assert metadata.signature == stable
    path.write_bytes(b'a larger video')
    with pytest.raises(VideoError, match='变化|写入'):
        probe_video(path, expected=stable, runner=ffprobe_runner(valid_payload()))


def test_probe_cancellation_before_and_after_runner_never_returns_metadata(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    calls = []
    def runner(*args, **kwargs):
        calls.append(1)
        return ffprobe_runner(valid_payload())(*args, **kwargs)
    with pytest.raises(VideoError, match='取消'):
        probe_video(path, runner=runner, cancel=lambda: True)
    assert calls == []
    with pytest.raises(VideoError, match='取消'):
        probe_video(path, runner=runner, cancel=lambda: bool(calls))
    assert calls == [1]


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf')])
def test_probe_timeout_is_positive_finite_and_bounded(tmp_path, value):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    with pytest.raises(VideoError, match='超时'):
        probe_video(path, runner=ffprobe_runner(valid_payload()), timeout=value)


def test_probe_oversized_or_ambiguous_payloads_are_rejected(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    def oversized(*args, **kwargs):
        return type('Result', (), {'stdout': ' ' * 1_048_577, 'returncode': 0})()
    with pytest.raises(VideoError, match='大小|限额'):
        probe_video(path, runner=oversized)
    def duplicates(*args, **kwargs):
        return type('Result', (), {'stdout': '{"streams":[],"streams":[{"codec_type":"video","width":1,"height":1},{"codec_type":"audio"}],"format":{"duration":"1"}}', 'returncode': 0})()
    with pytest.raises(VideoError, match='JSON|重复'):
        probe_video(path, runner=duplicates)


def test_candidates_use_frozen_wall_window_and_do_not_accept_future_modification(tmp_path):
    before = snapshot_directory(tmp_path)
    path = tmp_path / 'later.mp4'; path.write_bytes(b'video')
    signature = video_adapter.current_signature(path)
    event_time = signature.mtime_ns / 1_000_000_000
    assert candidates_since(tmp_path, before, started_wall=event_time-1, stopped_wall=event_time+1)
    assert not candidates_since(tmp_path, before, started_wall=event_time-10, stopped_wall=event_time-5)


def test_production_probe_requires_explicit_existing_local_tool(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    with pytest.raises(VideoError, match='ffprobe.*路径|工具'):
        probe_video(path)


def test_probe_timeout_is_forwarded_without_retry(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    calls = []
    def runner(*args, **kwargs):
        calls.append(kwargs['timeout'])
        raise subprocess.TimeoutExpired('ffprobe', kwargs['timeout'])
    with pytest.raises(VideoError):
        probe_video(path, runner=runner, timeout=1.25)
    assert calls == [1.25]


def test_unknown_candidate_identity_cannot_authorize_stability_or_probe(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    actual = video_adapter.current_signature(path)
    legacy = video_adapter.FileSignature(path, actual.bytes, actual.mtime_ns, actual.ctime_ns)
    with pytest.raises(VideoError, match='身份未知'):
        wait_stable(path, expected=legacy)
    with pytest.raises(VideoError, match='身份未知'):
        probe_video(path, expected=legacy, runner=ffprobe_runner(valid_payload()))


def test_cancelled_scan_does_not_return_partial_baseline(tmp_path):
    (tmp_path / 'one.mp4').write_bytes(b'1')
    (tmp_path / 'two.mp4').write_bytes(b'2')
    called = []
    def cancel():
        called.append(1)
        return len(called) >= 2
    with pytest.raises(video_adapter.VideoCancelled):
        snapshot_directory(tmp_path, cancel=cancel)
    assert all(path.is_file() for path in tmp_path.iterdir())


def test_probe_postread_mutation_cannot_return_old_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    initial = video_adapter.current_signature(path)
    real = video_adapter.current_signature
    calls = []
    def named_signature(value, **kwargs):
        calls.append(1)
        # This exercises the independent final pathname check after the native
        # held handle reported its unchanged signature. A rename race on a
        # platform/filesystem must never be accepted from the held handle alone.
        return video_adapter.FileSignature(path, initial.bytes, initial.mtime_ns,
                                            initial.ctime_ns, ('windows', 1, 2),
                                            initial.change_ns)
    monkeypatch.setattr(video_adapter, 'current_signature', named_signature)
    with pytest.raises(VideoError, match='替换|写入'):
        probe_video(path, expected=initial, runner=ffprobe_runner(valid_payload()))
    assert calls == [1] and real(path) == initial


@pytest.mark.parametrize('payload', [
    {'streams': [None], 'format': {'duration': '1'}},
    {'streams': [{'codec_type': 'video', 'width': True, 'height': 1}, {'codec_type': 'audio'}], 'format': {'duration': '1'}},
    {'streams': [{'codec_type': 'video', 'width': 1, 'height': 1}, {'codec_type': 'audio'}], 'format': {'duration': True}},
    {'streams': [{'codec_type': 'video', 'width': 1, 'height': 1}, {'codec_type': 'audio'}], 'format': {'duration': 'Infinity'}},
    {'streams': [{'codec_type': 'video', 'width': 1, 'height': 1}, {'codec_type': 'audio'}] * 33, 'format': {'duration': '1'}},
])
def test_probe_rejects_invalid_structure_boolean_numbers_nonfinite_or_unbounded_streams(tmp_path, payload):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    with pytest.raises(VideoError):
        probe_video(path, runner=ffprobe_runner(payload))


def test_probe_releases_read_locks_after_runner_error_or_cancellation(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    def fail(*args, **kwargs):
        raise OSError('simulated tool cannot run')
    with pytest.raises(VideoError):
        probe_video(path, runner=fail)
    path.write_bytes(b'after error')
    with pytest.raises(VideoError):
        probe_video(path, cancel=lambda: True, runner=ffprobe_runner(valid_payload()))
    path.rename(tmp_path / 'after-error.mp4')


@pytest.mark.parametrize('started,stopped,tolerance', [
    (True, None, 0), (1, 0, 0), (None, 2, 0), (float('nan'), None, 0),
    (1, float('inf'), 0), (1, 2, 3), (1, 2, True),
])
def test_discovery_rejects_invalid_or_extended_wall_time_windows(tmp_path, started, stopped, tolerance):
    with pytest.raises(VideoError):
        candidates_since(tmp_path, {}, started_wall=started, stopped_wall=stopped,
                         wall_tolerance=tolerance)


@pytest.mark.parametrize('stdout,returncode', [(chr(0xD800), 0), (None, 0), ('{}', False), ('{}', None)])
def test_probe_invalid_utf8_or_nonprocess_result_is_a_clear_video_error(tmp_path, stdout, returncode):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    def runner(*args, **kwargs):
        return type('Result', (), {'stdout': stdout, 'returncode': returncode})()
    with pytest.raises(VideoError):
        probe_video(path, runner=runner)
