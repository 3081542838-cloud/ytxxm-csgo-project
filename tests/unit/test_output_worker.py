import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cs2pov.adapters.probe_tool import trusted_probe
from cs2pov.adapters.video import VideoError, current_signature
from cs2pov.services.output_validation import OutputDiscoveryRequest, output_json_value
from cs2pov.services.output_worker import MAX_WORKER_JSON, strict_json
from cs2pov.storage.settings import DataError


@pytest.mark.parametrize('data', [b'[]', b'{"x":1,"x":2}', b'{"x":NaN}',
                                b'{"x":Infinity}', b'no-json', b' ' * (MAX_WORKER_JSON + 1)],
                         ids=['array', 'duplicate', 'nan', 'infinity', 'malformed', 'oversized'])
def test_worker_rejects_unbounded_or_ambiguous_protocol(data):
    with pytest.raises(DataError):
        strict_json(data)


def test_probe_tool_rejects_unreviewed_executable_without_running(tmp_path):
    fake = tmp_path / 'ffprobe.exe'
    fake.write_bytes(b'unreviewed')
    with pytest.raises(VideoError, match='大小'):
        with trusted_probe(fake):
            pytest.fail('unreviewed executable accepted')


def test_real_child_discovers_one_level_without_database_or_mutation(tmp_path):
    root = tmp_path / '中文 Videos'; root.mkdir()
    sub = root / 'Counter-strike 2'; sub.mkdir()
    path = sub / 'clip.mp4'; path.write_bytes(b'candidate')
    sig = current_signature(path)
    stamp = sig.mtime_ns / 1e9
    request = OutputDiscoveryRequest('a' * 32, 'b' * 32, str(root), stamp - 2, stamp + 2, ())
    request_file = tmp_path / 'request.json'
    request_file.write_text(json.dumps(output_json_value(request)), encoding='utf-8')
    before = path.read_bytes()
    child = subprocess.run([sys.executable, '-m', 'cs2pov.services.output_worker',
                            '--phase', 'discover', '--request', str(request_file)],
                           capture_output=True, timeout=15,
                           env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')})
    assert child.returncode == 0, child.stdout
    result = strict_json(child.stdout)
    assert result['task_id'] == 'a' * 32 and result['request_id'] == 'b' * 32
    assert len(result['candidates']) == 1 and Path(result['candidates'][0]['path']) == path
    assert path.read_bytes() == before
    assert not list(tmp_path.rglob('*.sqlite'))


def test_real_child_malformed_request_fails_closed(tmp_path):
    request_file = tmp_path / 'bad.json'; request_file.write_text('{"task_id":0}')
    child = subprocess.run([sys.executable, '-m', 'cs2pov.services.output_worker',
                            '--phase', 'discover', '--request', str(request_file)],
                           capture_output=True, timeout=15,
                           env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')})
    assert child.returncode == 1
    assert set(strict_json(child.stdout)) == {'error'}
