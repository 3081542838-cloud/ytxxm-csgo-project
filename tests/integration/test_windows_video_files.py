"""Real Windows file identities and sharing locks; no game/video interaction."""
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cs2pov.adapters.video import (VideoError, current_signature, probe_video,
                                   read_lease, snapshot_directory, wait_stable)
import cs2pov.adapters.video as video_adapter


def _result():
    return subprocess.CompletedProcess([], 0, json.dumps({
        'streams': [{'codec_type': 'video', 'width': 1920, 'height': 1080},
                    {'codec_type': 'audio'}], 'format': {'duration': '12.5'}}), '')


def test_real_windows_id_survives_rename_but_not_same_size_timestamp_replacement(tmp_path):
    assert os.name == 'nt'
    original = tmp_path / 'first.mp4'; original.write_bytes(b'first')
    first = current_signature(original)
    assert first.identity[0] == 'windows' and first.identity[2] > 0
    renamed = tmp_path / 'renamed.mp4'
    original.rename(renamed)
    moved = current_signature(renamed)
    assert moved.identity == first.identity
    replacement = tmp_path / 'replacement.mp4'; replacement.write_bytes(b'other')
    os.utime(replacement, ns=(moved.mtime_ns, moved.mtime_ns))
    os.replace(replacement, renamed)
    changed = current_signature(renamed)
    assert changed.bytes == moved.bytes and changed.mtime_ns == moved.mtime_ns
    assert changed.identity != moved.identity
    with pytest.raises(VideoError, match='身份|替换'):
        wait_stable(renamed, expected=moved)
    with pytest.raises(VideoError, match='身份|替换'):
        probe_video(renamed, expected=moved, runner=lambda *a, **kw: _result())


def test_locked_probe_blocks_real_file_write_rename_and_parent_rename(tmp_path):
    folder = tmp_path / 'Counter-strike 2'; folder.mkdir()
    path = folder / 'clip.mp4'; path.write_bytes(b'video')
    signature = current_signature(path)
    attempted = []
    def runner(*args, **kwargs):
        for action in (lambda: path.write_bytes(b'other'),
                       lambda: path.rename(folder / 'moved.mp4'),
                       lambda: folder.rename(tmp_path / 'moved-game')):
            with pytest.raises(OSError) as error:
                action()
            # Python's CRT-backed open() exposes EACCES without winerror;
            # Windows rename exposes the native sharing violation number.
            assert isinstance(error.value, PermissionError)
            assert error.value.errno in (errno.EACCES, errno.EPERM)
            assert error.value.winerror in (None, 5, 32, 33)
            attempted.append(True)
        assert path.read_bytes() == b'video'
        return _result()
    metadata = probe_video(path, expected=signature, runner=runner)
    assert len(attempted) == 3 and metadata.signature == signature
    # The probe releases all handles; later ordinary file operations still work.
    path.write_bytes(b'newer')
    path.rename(folder / 'after.mp4')
    folder.rename(tmp_path / 'after-game')


def test_probe_rejects_real_existing_writer_even_when_metadata_looks_stable(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'video')
    signature = current_signature(path)
    calls = []
    with path.open('ab'):
        with pytest.raises(VideoError, match='写入|读取'):
            probe_video(path, expected=signature,
                        runner=lambda *a, **kw: calls.append(1) or _result())
    assert calls == [] and path.read_bytes() == b'video'


def test_real_change_time_detects_same_size_write_with_restored_mtime(tmp_path):
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'first')
    signature = current_signature(path)
    time.sleep(.01)
    path.write_bytes(b'other')
    os.utime(path, ns=(signature.mtime_ns, signature.mtime_ns))
    changed = current_signature(path)
    assert changed.identity == signature.identity and changed.bytes == signature.bytes
    assert changed.mtime_ns == signature.mtime_ns and changed.change_ns != signature.change_ns
    calls = []
    with pytest.raises(VideoError, match='变化|写入'):
        probe_video(path, expected=signature,
                    runner=lambda *a, **kw: calls.append(1) or _result())
    assert calls == []


def test_reparse_junction_and_ancestor_are_rejected_without_reading_target(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    secret = outside / 'clip.mp4'; secret.write_bytes(b'unchanged')
    link = root / 'game-junction'
    # mklink creates only this test-owned directory entry. Never use it to delete.
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True, timeout=5, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert link.lstat().st_file_attributes & 0x400
    with pytest.raises(VideoError, match='junction|reparse|链接'):
        snapshot_directory(root)
    with pytest.raises(VideoError, match='junction|reparse|链接'):
        snapshot_directory(link)
    with pytest.raises(VideoError, match='junction|reparse|链接'):
        current_signature(link / 'clip.mp4')
    with pytest.raises(VideoError, match='junction|reparse|链接'):
        probe_video(link / 'clip.mp4', runner=lambda *a, **kw: _result())
    assert secret.read_bytes() == b'unchanged'
    # Unlink just the junction, preserving the real test-owned target.
    os.rmdir(link)
    assert secret.read_bytes() == b'unchanged'


def test_public_executable_read_lease_blocks_write_and_replacement_then_releases(tmp_path):
    executable = tmp_path / 'fixed-probe.exe'; executable.write_bytes(b'fake executable fixture')
    with read_lease(executable) as signature:
        assert signature.identity[0] == 'windows'
        with pytest.raises(OSError):
            executable.write_bytes(b'changed')
        with pytest.raises(OSError):
            executable.rename(tmp_path / 'changed.exe')
        assert executable.read_bytes() == b'fake executable fixture'
    executable.write_bytes(b'after lease')
    executable.rename(tmp_path / 'after.exe')


def test_probe_child_timeout_cancel_and_output_limits_terminate_only_owned_child(tmp_path):
    assert os.name == 'nt'
    sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']
    started = time.monotonic()
    with pytest.raises(VideoError, match='超时'):
        video_adapter._bounded_run(sleeper, timeout=.1, cancel=None)
    assert time.monotonic() - started < 3
    started = time.monotonic()
    with pytest.raises(VideoError, match='取消'):
        video_adapter._bounded_run(sleeper, timeout=10,
                                   cancel=lambda: time.monotonic() - started > .1)
    assert time.monotonic() - started < 3
    verbose = [sys.executable, '-c', 'import sys,time; sys.stdout.write("x"*1048577);sys.stdout.flush();time.sleep(60)']
    with pytest.raises(VideoError, match='大小|限额'):
        video_adapter._bounded_run(verbose, timeout=3, cancel=None)


def test_probe_child_supports_real_utf8_json_without_shell_or_foreground(tmp_path):
    payload = '{"streams":[{"codec_type":"video","width":1,"height":1},{"codec_type":"audio"}],"format":{"duration":"1"}}'
    result = video_adapter._bounded_run([sys.executable, '-c', 'print(' + repr(payload) + ')'],
                                       timeout=3, cancel=None)
    assert result.returncode == 0 and json.loads(result.stdout)['format']['duration'] == '1'
