"""These subprocess tests exercise only this project's worker, never NVIDIA UI."""
import json
import subprocess
import sys
import time

import pytest

from cs2pov.adapters.nvidia_status import observe_nvidia_status
from cs2pov.adapters.owned_process import ProcessIdentity


EXE = r'C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe'


def fake_worker(code, owned):
    def create(_args, **kwargs):
        process = subprocess.Popen([sys.executable, '-c', code], **kwargs)
        owned.append(process)
        return process
    return create


def test_hung_com_worker_is_killed_at_hard_deadline_without_touching_external_apps():
    owned = []
    started = time.monotonic()
    value = observe_nvidia_status(EXE, timeout=.3,
        popen=fake_worker('import time; time.sleep(30)', owned))
    assert value.state == 'unknown' and value.reason == 'worker_timeout'
    assert time.monotonic() - started < 3
    assert len(owned) == 1 and owned[0].poll() is not None


def test_crashed_or_malformed_worker_has_no_status_and_no_retry():
    for code in ('raise SystemExit(4)', "print('{malformed')",
                 "print('{}')", "print('x' * 100_000)"):
        owned = []
        value = observe_nvidia_status(EXE, timeout=2, popen=fake_worker(code, owned))
        assert value.state == 'unknown' and len(owned) == 1
        assert owned[0].poll() is not None


def test_replayed_worker_nonce_and_old_observation_cannot_supply_idle():
    payload = {
        'request_id': 'a' * 32, 'started_at': 1.0, 'observed_at': 1.1,
        'complete': True, 'windows': [], 'reason': '',
    }
    owned = []
    code = 'import json; print(' + repr(json.dumps(payload)) + ')'
    value = observe_nvidia_status(EXE, timeout=2, popen=fake_worker(code, owned))
    assert value.state == 'unknown' and len(owned) == 1


@pytest.mark.parametrize('timeout', [0, -.1, float('nan'), float('inf'), True, 11])
def test_invalid_deadline_never_starts_worker(timeout):
    owned = []
    value = observe_nvidia_status(EXE, timeout=timeout, popen=fake_worker('pass', owned))
    assert value.state == 'unknown' and not owned


def test_current_worker_sample_is_nonce_bound_and_rechecked_in_parent():
    # Synthetic child proves the real pipe/deadline/protocol path only. It does
    # not touch or establish compatibility with a real NVIDIA Overlay provider.
    code = '''import json, sys, time
r = json.loads(sys.stdin.buffer.read())
v = {'request_id': r['request_id'], 'started_at': r['requested_at'],
     'observed_at': time.monotonic(), 'complete': True, 'reason': '',
     'windows': [{'hwnd': 123, 'visible': True,
       'identity': {'pid': 20616, 'created': 123456, 'executable': r['executable']},
       'nodes': [
         {'parent': None, 'name': 'NVIDIA Overlay', 'control_type': 50032,
          'offscreen': False, 'enabled': True, 'process_id': 20616},
         {'parent': 0, 'name': 'Record', 'control_type': 50026,
          'offscreen': False, 'enabled': True, 'process_id': 20616},
         {'parent': 1, 'name': 'Start', 'control_type': 50000,
          'offscreen': False, 'enabled': True, 'process_id': 20616}]}]}
sys.stdout.buffer.write(json.dumps(v).encode())
'''
    for actual, expected in ((123456, 'idle'), (123457, 'unknown')):
        owned = []
        value = observe_nvidia_status(EXE, timeout=2, popen=fake_worker(code, owned),
            identity_query=lambda _: ProcessIdentity(20616, actual, EXE))
        assert value.state == expected and len(owned) == 1 and owned[0].poll() is not None


def test_pipe_error_reaps_only_the_held_worker_and_does_not_retry():
    class Worker:
        returncode = None
        def __init__(self): self.kills = 0; self.calls = 0
        def communicate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1: raise OSError('pipe failed')
            return b'', None
        def poll(self): return self.returncode
        def kill(self): self.kills += 1; self.returncode = -1
    worker = Worker(); spawned = []
    def create(*_args, **_kwargs): spawned.append(worker); return worker
    value = observe_nvidia_status(EXE, popen=create)
    assert value.state == 'unknown' and spawned == [worker]
    assert worker.kills == 1 and worker.poll() is not None
