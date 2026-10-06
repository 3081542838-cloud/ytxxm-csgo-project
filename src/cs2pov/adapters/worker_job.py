"""Contain only this offline worker and its children in an unnamed Windows job.

Microsoft: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
The handle is intentionally held until process exit. Closing it explicitly
would terminate this worker too; it is never inherited by a probe child.
"""
import ctypes
from ctypes import wintypes
import os

_JOB_HANDLE = None


def contain_current_worker():
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return
    if os.name != 'nt':
        raise RuntimeError('视频检查进程保护需要 Windows。')
    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                    ('max_working', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_count', 'write_count', 'other_count', 'read_bytes', 'write_bytes', 'other_bytes')]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimits()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.WinError(ctypes.get_last_error()); api.CloseHandle(handle); raise error
    if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
        error = ctypes.WinError(ctypes.get_last_error()); api.CloseHandle(handle); raise error
    _JOB_HANDLE = handle
