"""Public Windows process enumeration. No process memory access."""
import ctypes
from ctypes import wintypes


class ProcessError(RuntimeError):
    pass


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def process_names() -> dict[int, str]:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(api, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        function.restype = wintypes.BOOL
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ProcessError(str(ctypes.WinError(ctypes.get_last_error())))
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not api.Process32FirstW(handle, ctypes.byref(entry)):
            raise ProcessError("进程列表读取失败。")
        result = {}
        while True:
            result[entry.th32ProcessID] = entry.szExeFile
            if not api.Process32NextW(handle, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ProcessError("进程枚举中断，不能确认游戏已关闭。")
                return result
    finally:
        api.CloseHandle(handle)


def cs2_closed() -> bool:
    try:
        return not any(name.casefold() == "cs2.exe" for name in process_names().values())
    except (OSError, ProcessError):
        return False
