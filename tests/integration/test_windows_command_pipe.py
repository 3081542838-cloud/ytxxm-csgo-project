"""Real local pipe transport only; this file never starts or talks to CS2."""
import ctypes
from ctypes import wintypes
import os
import re
import time
from types import SimpleNamespace

from cs2pov.adapters.command_pipe import CommandPipes, Win32PipeBackend, _RETIRED
from cs2pov.adapters.owned_process import process_identity


def clients(pipes):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    api.ReadFile.restype = wintypes.BOOL
    api.WriteFile.argtypes = api.ReadFile.argtypes
    api.WriteFile.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handles = []
    try:
        for name in pipes.paths:
            handle = api.CreateFileW(name, 0xC0000000, 0, None, 3, 0, None)
            assert handle and handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
            handles.append(handle)
        return api, handles
    except Exception:
        for handle in handles:
            api.CloseHandle(handle)
        raise


def test_real_windows_transport_checks_own_client_pid_and_exact_unicode_lf_bytes():
    pipes = CommandPipes().create()
    api = handles = None
    try:
        api, handles = clients(pipes)
        identity = process_identity(os.getpid())
        # This fixture proves local OS transport with the pytest process. It is
        # not a CS2 identity, a product launch receipt or replay evidence.
        game = SimpleNamespace(verify=lambda: process_identity(os.getpid()), argv=['pytest', '-insecure'])
        assert pipes.poll_connection(game) is True
        assert pipes.identity == identity
        command = 'spec_player " 一条小虾米OVO"'
        pipes.send_command(command, game)
        expected = (command+'\n').encode('utf-8')
        storage = ctypes.create_string_buffer(4096)
        count = wintypes.DWORD()
        assert api.ReadFile(handles[0], storage, len(expected), ctypes.byref(count), None)
        assert count.value == len(expected) and storage.raw[:count.value] == expected
        out = b'not replay evidence\n'*200
        sent = wintypes.DWORD()
        assert api.WriteFile(handles[1], out, len(out), ctypes.byref(sent), None)
        assert sent.value == len(out)
        drained = pipes.backend.drain(pipes.handles[1], limit=65536)
        assert drained == len(out)
        assert pipes.backend.client_pid(pipes.handles[0]) == os.getpid()
        assert pipes.backend.client_pid(pipes.handles[1]) == os.getpid()
    finally:
        pipes.close()
        if handles:
            for handle in handles:
                api.CloseHandle(handle)
        pipes.backend._reap()


def test_real_pipe_acl_contains_only_local_system_and_current_user_and_pending_close_is_bounded():
    backend = Win32PipeBackend()
    pipes = CommandPipes(backend=backend).create()
    try:
        api = backend.security
        api.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
            wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPVOID)]
        api.GetSecurityInfo.restype = wintypes.DWORD
        api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG)]
        api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        for handle in pipes.handles:
            descriptor = wintypes.LPVOID()
            text = wintypes.LPWSTR()
            try:
                assert api.GetSecurityInfo(handle, 6, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
                assert api.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4,
                    ctypes.byref(text), None)
                assert text.value.startswith('D:P')
                trustees = re.findall(r'\(A;;(?:GA|FA);;;([^)]+)\)', text.value)
                assert len(trustees) == 2 and 'SY' in trustees
                assert any(value.startswith('S-1-5-21-') for value in trustees)
                assert 'WD' not in trustees and 'AN' not in trustees and 'BA' not in trustees
            finally:
                if text:
                    backend.api.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
                if descriptor:
                    backend.api.LocalFree(descriptor)
        assert backend.connect(pipes.handles[0]) is False
        assert backend.connect(pipes.handles[1]) is False
        started = time.monotonic()
        pipes.close()
        assert time.monotonic()-started < 1
        # Event/OVERLAPPED storage stays retained until cancellation completes;
        # repeated nonblocking reap cannot touch a reused pipe handle.
        for _ in range(10):
            backend._reap()
            if not _RETIRED:
                break
            time.sleep(0.01)
        assert not _RETIRED
    finally:
        pipes.close()
