import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys


def test_worker_exit_terminates_probe_child_but_preserves_unrelated_process():
    assert os.name == 'nt'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    script = ('from cs2pov.adapters.worker_job import contain_current_worker;'
              'import subprocess,sys,time;contain_current_worker();'
              'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],'
              'creationflags=subprocess.CREATE_NO_WINDOW);print(p.pid,flush=True);time.sleep(60)')
    worker = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        pid = int(worker.stdout.readline())
        handle = api.OpenProcess(0x100000, False, pid)
        assert handle
        worker.kill(); worker.wait(timeout=3)
        assert api.WaitForSingleObject(handle, 3000) == 0
        assert unrelated.poll() is None
    finally:
        if handle: api.CloseHandle(handle)
        for child in (worker, unrelated):
            if child.poll() is None: child.kill()
            child.wait(timeout=3)
        worker.stdout.close(); worker.stderr.close()
