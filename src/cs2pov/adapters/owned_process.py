"""Owned CS2 identity from public process metadata, never process memory."""
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
from cs2pov.adapters.processes import cs2_closed, ProcessError
from cs2pov.storage.transaction import atomic_write, safe_path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: int
    executable: str


def identity_from_handle(api, handle, pid):
    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not api.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                               ctypes.byref(kernel), ctypes.byref(user)):
        raise ProcessError("无法读取进程创建时间，不能确认游戏身份。")
    path = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(path))
    if not api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
        raise ProcessError("无法读取进程路径，不能确认游戏身份。")
    return ProcessIdentity(pid, (creation.dwHighDateTime << 32) | creation.dwLowDateTime, str(Path(path.value)))


def kernel_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    api.GetProcessTimes.restype = wintypes.BOOL
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    return api


def process_identity(pid):
    api = kernel_api()
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ProcessError("进程不存在或权限不足，停止操作。")
    try:
        return identity_from_handle(api, handle, pid)
    finally:
        api.CloseHandle(handle)


def terminate_matching(expected):
    """Recheck birth/path on the same handle used to terminate, avoiding PID reuse."""
    api = kernel_api()
    handle = api.OpenProcess(0x1001, False, expected.pid)
    if not handle:
        raise ProcessError("无法确认待关闭游戏，未发送关闭操作。")
    try:
        if identity_from_handle(api, handle, expected.pid) != expected:
            raise ProcessError("进程身份变化，拒绝关闭。")
        if not api.TerminateProcess(handle, 1):
            raise ProcessError("游戏关闭失败；保留恢复事务。")
    finally:
        api.CloseHandle(handle)


class OwnedGame:
    def __init__(self, *, query=process_identity, closed=cs2_closed, popen=subprocess.Popen,
                 terminate=terminate_matching):
        self.query, self.closed, self.popen, self.terminate = query, closed, popen, terminate
        self.process = None
        self.identity = None
        self.argv = None

    def launch(self, installation: Path, session: Path, *, telemetry=False, command_pipes=None):
        if self.process is not None or not self.closed():
            raise ProcessError("已有 CS2 或进程状态未知，不能启动第二个游戏。")
        executable = installation / "game/bin/win64/cs2.exe"
        if not executable.is_file():
            raise ProcessError("CS2 可执行文件不存在。")
        if type(telemetry) is not bool:
            raise ProcessError("回读启动选项无效。")
        pipe_argument = None
        if command_pipes is not None:
            # Import here to avoid the adapter's ProcessIdentity dependency cycle.
            from cs2pov.adapters.command_pipe import CommandPipes
            if (type(command_pipes) is not CommandPipes or command_pipes.state != 'created'
                    or command_pipes.identity is not None or command_pipes.game is not None):
                raise ProcessError("命令管道未创建或已属于其他游戏。")
            if not telemetry:
                raise ProcessError("命令管道启动必须启用真实日志回读。")
            try:
                pipe_argument = command_pipes.argument
            except Exception as error:
                raise ProcessError("两条命令管道尚未完整创建。") from error
            pattern = r'\\\\\.\\pipe\\cs2pov_([0-9a-f]{32})_cmd,\\\\\.\\pipe\\cs2pov_\1_out'
            if re.fullmatch(pattern, pipe_argument) is None:
                raise ProcessError("命令管道名称或会话标识不一致。")
        # A process-local region flag bypasses CS2's China launch chooser.
        # It is used only for this owned local Demo, not saved to Steam options.
        self.argv = [str(executable), "-insecure", "-novid", "-worldwide"]
        if pipe_argument is None:
            self.argv.append('-console')
        else:
            self.argv.extend(['-concommandpipe', pipe_argument])
        if telemetry:
            # Current build accepts an absolute launch argument, not a convar.
            # Refuse old logs so a new process cannot inherit previous evidence.
            log = safe_path(session, session / "engine.log").absolute()
            if log.exists():
                raise ProcessError("回读会话已有日志，请使用新的会话目录。")
            self.argv.extend(["-condebug", "-con_logfile", str(log)])
        atomic_write(session / "launch.json", json.dumps({"argv": self.argv}, ensure_ascii=False).encode("utf-8"))
        self.process = self.popen(self.argv, cwd=executable.parent)
        # If metadata fails, keep the process reference but do not act on it.
        identity = self.query(self.process.pid)
        if Path(identity.executable) != executable or self.process.poll() is not None:
            raise ProcessError("启动进程身份不符；不会操作或关闭这个未知进程。")
        self.identity = identity
        atomic_write(session / "process.json", json.dumps({**asdict(identity), "argv": self.argv}, ensure_ascii=False).encode("utf-8"))
        return identity

    def verify(self):
        if self.process is None or self.identity is None or self.process.poll() is not None:
            raise ProcessError("受管理游戏已经退出或身份未确认。")
        if self.query(self.identity.pid) != self.identity:
            raise ProcessError("游戏 PID、创建时间或路径变化，停止操作。")
        return self.identity

    def force_close(self):
        self.terminate(self.verify())
