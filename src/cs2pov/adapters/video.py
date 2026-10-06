"""Bounded, read-only NVIDIA output discovery and locked metadata inspection."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import ctypes
import json
import math
import os
from pathlib import Path
import stat as stat_module
import subprocess
import threading
import time

from cs2pov.storage.settings import DataError, local_path


class VideoError(DataError):
    pass


class VideoCancelled(VideoError):
    pass


VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov"})
MAX_PROBE_BYTES = 1_048_576
_REPARSE_POINT = 0x400
_FILETIME_EPOCH = 116444736000000000


@dataclass(frozen=True)
class FileSignature:
    path: Path
    bytes: int
    mtime_ns: int
    ctime_ns: int
    # Legacy serialized signatures lack identity; they cannot authorize probing.
    identity: tuple[str, int, int] | None = None
    change_ns: int | None = None


@dataclass(frozen=True)
class VideoMetadata:
    path: Path
    duration: float
    width: int
    height: int
    video_streams: int
    audio_streams: int
    signature: FileSignature | None = None


def _cancelled(cancel):
    if cancel is not None and cancel():
        raise VideoCancelled("视频检查已取消；原视频保留。")


def _positive_seconds(value, label, *, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise VideoError(f"{label}必须是有限正数，且不能超过 {maximum} 秒。")
    return float(value)


def _path(path):
    path = Path(path)
    local_path(str(path))
    # Resolving first would conceal a junction or symbolic link.
    if ".." in path.parts or (os.name == "nt" and any(":" in part for part in path.parts[1:])):
        raise VideoError("视频路径包含越界或替代数据流。")
    return path.absolute()


def _plain_stat(path, *, directory=False):
    try:
        value = path.lstat()
    except OSError as error:
        raise VideoError("视频文件或目录不可读取。") from error
    if stat_module.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & _REPARSE_POINT:
        raise VideoError("视频路径包含链接、junction 或 reparse point，不能检查。")
    valid = stat_module.S_ISDIR(value.st_mode) if directory else stat_module.S_ISREG(value.st_mode)
    if not valid:
        raise VideoError("视频目录不是文件夹。" if directory else "视频路径不是普通文件。")
    return value


def _parents(path):
    return tuple(reversed(path.parents))


def _plain_path(path):
    path = _path(path)
    for parent in _parents(path):
        _plain_stat(parent, directory=True)
    _plain_stat(path)
    return path


def _check_path(path, directory=None):
    path = _path(path)
    if path.suffix.casefold() not in VIDEO_SUFFIXES:
        raise VideoError("视频文件扩展名不受支持。")
    if directory is not None:
        root = _path(directory)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise VideoError("视频文件超出冻结的输出目录。") from error
        if len(relative.parts) not in (1, 2):
            raise VideoError("视频文件超出输出目录允许的一层子目录。")
    return _plain_path(path)


if os.name == "nt":
    from ctypes import wintypes

    class _BasicInfo(ctypes.Structure):
        _fields_ = [("creation", ctypes.c_longlong), ("access", ctypes.c_longlong),
                    ("write", ctypes.c_longlong), ("change", ctypes.c_longlong),
                    ("attributes", wintypes.DWORD)]

    class _StandardInfo(ctypes.Structure):
        _fields_ = [("allocation", ctypes.c_longlong), ("size", ctypes.c_longlong),
                    ("links", wintypes.DWORD), ("deleted", ctypes.c_ubyte),
                    ("directory", ctypes.c_ubyte)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _handle_info(handle, kind, structure):
        result = structure()
        if not _kernel32.GetFileInformationByHandleEx(handle, kind, ctypes.byref(result), ctypes.sizeof(result)):
            raise VideoError("无法获取视频文件身份或元数据；不能按未知身份通过检查。")
        return result

    def _windows_signature(handle, path, *, directory=False):
        basic = _handle_info(handle, 0, _BasicInfo)
        standard = _handle_info(handle, 1, _StandardInfo)
        identity = _handle_info(handle, 18, _FileIdInfo)
        file_id = int.from_bytes(bytes(identity.identifier), "little")
        if basic.attributes & _REPARSE_POINT or bool(standard.directory) != directory or standard.deleted:
            raise VideoError("视频文件或目录包含重定向、类型变化或已删除身份。")
        if not file_id:
            raise VideoError("视频文件身份未知，不能通过检查。")
        return FileSignature(path, standard.size, (basic.write - _FILETIME_EPOCH) * 100,
                             (basic.creation - _FILETIME_EPOCH) * 100,
                             ("windows", identity.volume, file_id),
                             (basic.change - _FILETIME_EPOCH) * 100)

    @contextmanager
    def _windows_handle(path, *, directory=False, locked=False):
        # Attribute scans allow recording. Probes omit SHARE_WRITE/DELETE.
        access = 0x80 if directory or not locked else 0x80000000
        share = 3 if directory and locked else (1 if locked else 7)
        flags = 0x00200000 | (0x02000000 if directory else 0)
        handle = _kernel32.CreateFileW(str(path), access, share, None, 3, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            raise VideoError("视频文件仍在写入、被替换或无法安全读取。")
        try:
            yield handle
        finally:
            if not _kernel32.CloseHandle(handle):
                raise VideoError("视频只读检查句柄未能关闭；未确认检查成功。")


@contextmanager
def _locked_signature(path, *, lock=False):
    """Hold file and ancestors against replacement while ffprobe uses the path."""
    path = _plain_path(path)
    with ExitStack() as stack:
        if os.name == "nt":
            parents = []
            if lock:
                for parent in _parents(path):
                    handle = stack.enter_context(_windows_handle(parent, directory=True, locked=True))
                    parents.append((handle, parent, _windows_signature(handle, parent, directory=True).identity))
            handle = stack.enter_context(_windows_handle(path, locked=lock))
            initial = _windows_signature(handle, path)
            def signature():
                for parent_handle, parent, identity in parents:
                    if _windows_signature(parent_handle, parent, directory=True).identity != identity:
                        raise VideoError("视频目录身份发生变化。")
                return _windows_signature(handle, path)
            yield initial, signature
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            stack.callback(os.close, fd)
            def signature():
                value = os.fstat(fd)
                if not stat_module.S_ISREG(value.st_mode) or not value.st_ino:
                    raise VideoError("视频文件身份未知或不是普通文件。")
                return FileSignature(path, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
                                     ("stat", value.st_dev, value.st_ino), value.st_ctime_ns)
            yield signature(), signature


@contextmanager
def read_lease(path, *, expected=None):
    """Lock a local regular file and its parents for a bounded read operation.

    This also protects the fixed ffprobe executable while its hash is checked
    and its own child starts. It never grants write/delete access.
    """
    path = _plain_path(path)
    if os.name != "nt":
        raise VideoError("锁定只读检查需要 Windows 文件共享保护。")
    with _locked_signature(path, lock=True) as (initial, again):
        if expected is not None:
            _same_identity(initial, expected)
            if initial != expected:
                raise VideoError("只读检查文件在锁定前发生变化。")
        yield initial
        if again() != initial:
            raise VideoError("只读检查文件在使用期间发生变化。")


@contextmanager
def read_data_lease(path):
    """Read an ordinary data snapshot without leasing all parent directories.

    Used only for bounded, strictly decoded JSON. The file itself denies
    write/delete access; path checks and a second named handle bind the read
    to one complete file version. Executables and HUD resources must keep
    using ``read_lease`` with its ancestor protection.
    """
    path = _plain_path(path)
    if os.name != 'nt':
        raise VideoError('锁定数据读取需要 Windows 文件共享保护。')
    with _windows_handle(path, locked=True) as handle:
        initial = _windows_signature(handle, path)
        _plain_path(path)
        with _windows_handle(path, locked=True) as named:
            if _windows_signature(named, path) != initial:
                raise VideoError('数据文件在读取前被替换。')
        yield initial
        _plain_path(path)
        if _windows_signature(handle, path) != initial:
            raise VideoError('数据文件在读取期间发生变化。')
        with _windows_handle(path, locked=True) as named:
            if _windows_signature(named, path) != initial:
                raise VideoError('数据文件路径不再对应读取的文件身份。')


def current_signature(path, *, directory=None):
    path = _check_path(path, directory)
    with _locked_signature(path) as (signature, again):
        if signature != again():
            raise VideoError("视频文件在读取签名时发生变化。")
        with _locked_signature(path) as (named, unused):
            if named != signature:
                raise VideoError("视频文件在读取签名时被替换或写入。")
        return signature


def _directory(directory):
    directory = _path(directory)
    for parent in (*_parents(directory), directory):
        _plain_stat(parent, directory=True)
    return directory


def snapshot_directory(directory, *, max_entries=4096, max_directories=64, cancel=None):
    directory = _directory(directory)
    if type(max_entries) is not int or not 1 <= max_entries <= 4096:
        raise VideoError("视频扫描条目数量限额无效。")
    if type(max_directories) is not int or not 0 <= max_directories <= 64:
        raise VideoError("视频扫描子目录数量限额无效。")
    result, scanned, directories = {}, 0, 0
    pending = [(directory, 0)]
    while pending:
        folder, depth = pending.pop(0)
        _plain_stat(folder, directory=True)
        try:
            for path in folder.iterdir():
                _cancelled(cancel)
                scanned += 1
                if scanned > max_entries:
                    raise VideoError("视频扫描条目数量超过限额，未返回不完整结果。")
                value = path.lstat()
                if stat_module.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & _REPARSE_POINT:
                    raise VideoError("视频目录包含链接、junction 或 reparse point。")
                if stat_module.S_ISDIR(value.st_mode):
                    if depth == 1:
                        raise VideoError("视频目录深度超过一层子目录，未返回不完整结果。")
                    directories += 1
                    if directories > max_directories:
                        raise VideoError("视频扫描子目录数量超过限额。")
                    pending.append((path, 1))
                elif path.suffix.casefold() in VIDEO_SUFFIXES:
                    result[path] = current_signature(path, directory=directory)
        except OSError as error:
            raise VideoError("视频扫描期间目录发生变化或无法读取。") from error
    _cancelled(cancel)
    return result


def candidates_since(directory, before, *, started_wall=None, stopped_wall=None,
                     wall_tolerance=0, cancel=None):
    if type(wall_tolerance) not in (int, float) or not math.isfinite(wall_tolerance) or not 0 <= wall_tolerance <= 2:
        raise VideoError("视频时间窗容差无效。")
    for value in (started_wall, stopped_wall):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise VideoError("视频检查的墙钟时间窗无效。")
    if stopped_wall is not None and (started_wall is None or stopped_wall < started_wall):
        raise VideoError("视频检查的开始和结束时间窗无效。")
    after = snapshot_directory(directory, cancel=cancel)
    result = []
    for path, signature in after.items():
        if before.get(path) == signature:
            continue
        times = (signature.mtime_ns / 1_000_000_000, signature.ctime_ns / 1_000_000_000)
        if started_wall is not None and not any(
                value >= started_wall - wall_tolerance and
                (stopped_wall is None or value <= stopped_wall + wall_tolerance) for value in times):
            continue
        result.append(signature)
    return tuple(sorted(result, key=lambda item: (item.mtime_ns, str(item.path))))


def _same_identity(actual, expected):
    if not isinstance(expected, FileSignature) or expected.identity is None:
        raise VideoError("候选视频身份未知，不能授权检查。")
    if actual.path != expected.path or actual.identity != expected.identity:
        raise VideoError("候选视频身份变化或被替换。")


def wait_stable(path, *, expected=None, timeout=30, interval=.5, clock=time.monotonic,
                sleep=time.sleep, cancel=None):
    path = _check_path(path)
    timeout = _positive_seconds(timeout, "稳定检查超时", maximum=120)
    interval = _positive_seconds(interval, "稳定检查间隔", maximum=timeout)
    deadline = clock() + timeout
    previous = None
    initial = expected
    while clock() <= deadline:
        _cancelled(cancel)
        current = current_signature(path)
        if initial is None:
            initial = current
        _same_identity(current, initial)
        if current.bytes > 0 and previous == current:
            return current
        previous = current
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    _cancelled(cancel)
    raise VideoError("视频为空、仍在写入或稳定检查超时。")


def _bounded_run(args, *, timeout, cancel):
    """Only this probe child is terminated; output and waits have hard bounds."""
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stdout, stderr = bytearray(), bytearray()
    overflow, read_failed = threading.Event(), threading.Event()
    def read(stream, output, limit):
        try:
            # read1 returns an available partial block. read(n) can wait for a
            # full block after the byte limit was already crossed by one byte.
            while chunk := stream.read1(8192):
                if len(output) + len(chunk) > limit:
                    overflow.set()
                    return
                output.extend(chunk)
        except OSError:
            read_failed.set()
    workers = [threading.Thread(target=read, args=(child.stdout, stdout, MAX_PROBE_BYTES), daemon=True),
               threading.Thread(target=read, args=(child.stderr, stderr, 262144), daemon=True)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    try:
        while child.poll() is None:
            _cancelled(cancel)
            if overflow.is_set():
                raise VideoError("ffprobe 输出大小超过限额。")
            if time.monotonic() >= deadline:
                raise VideoError("ffprobe 视频检查超时。")
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers) or read_failed.is_set():
            raise VideoError("ffprobe 输出未能完整读取。")
        if overflow.is_set():
            raise VideoError("ffprobe 输出大小超过限额。")
        _cancelled(cancel)
        return subprocess.CompletedProcess(args, child.returncode, stdout.decode("utf-8", "strict"), "")
    finally:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=1)
        finally:
            child.stdout.close()
            child.stderr.close()
            for worker in workers:
                worker.join(timeout=1)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _run_ffprobe(args, *, runner=subprocess.run, timeout=15, cancel=None):
    try:
        if runner is subprocess.run:
            result = _bounded_run(args, timeout=timeout, cancel=cancel)
        else:
            result = runner(args, capture_output=True, text=True, encoding="utf-8", errors="strict",
                            timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise VideoError("ffprobe 无法完成视频检查。") from error
    if type(getattr(result, "returncode", None)) is not int or result.returncode != 0:
        raise VideoError("ffprobe 拒绝了视频文件。")
    output = getattr(result, "stdout", None)
    try:
        size = len(output.encode("utf-8")) if isinstance(output, str) else MAX_PROBE_BYTES + 1
    except UnicodeError as error:
        raise VideoError("ffprobe 输出不是有效 UTF-8。") from error
    if size > MAX_PROBE_BYTES:
        raise VideoError("ffprobe 输出大小或类型超过限额。")
    try:
        return json.loads(output, object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (TypeError, ValueError, RecursionError) as error:
        raise VideoError("ffprobe 输出不是有效且无重复字段的 JSON。") from error


def probe_video(path, *, ffprobe="ffprobe", runner=subprocess.run, expected=None,
                timeout=15, cancel=None):
    _cancelled(cancel)
    path = _check_path(path)
    timeout = _positive_seconds(timeout, "ffprobe 检查超时", maximum=30)
    if runner is subprocess.run:
        try:
            tool = _path(ffprobe)
            for parent in _parents(tool):
                _plain_stat(parent, directory=True)
            _plain_stat(tool)
        except (OSError, DataError) as error:
            raise VideoError("请选择已核验的本地 ffprobe 工具完整路径。") from error
        ffprobe = str(tool)
    with ExitStack() as protection:
        if runner is subprocess.run:
            protection.enter_context(read_lease(ffprobe))
        initial, again = protection.enter_context(_locked_signature(path, lock=True))
        if expected is not None:
            _same_identity(initial, expected)
            if initial != expected:
                raise VideoError("视频在稳定检查后发生变化或继续写入。")
        if initial.bytes <= 0:
            raise VideoError("视频文件为空。")
        _cancelled(cancel)
        payload = _run_ffprobe([str(ffprobe), "-v", "error", "-print_format", "json",
                                "-show_entries", "stream=codec_type,width,height:format=duration",
                                "-show_streams", "-show_format", str(path)],
                               runner=runner, timeout=timeout, cancel=cancel)
        _cancelled(cancel)
        final = again()
        if final != initial or current_signature(path) != initial:
            raise VideoError("视频在 ffprobe 检查期间被替换或写入。")
        streams = payload.get("streams") if isinstance(payload, dict) else None
        fmt = payload.get("format") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or not isinstance(fmt, dict) or len(streams) > 64:
            raise VideoError("ffprobe 缺少流或格式信息，或流数量超过限额。")
        if any(not isinstance(stream, dict) for stream in streams):
            raise VideoError("ffprobe 视频流结构无效。")
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if len(videos) != 1 or not audios:
            raise VideoError("视频必须有一个视频流和至少一个音频流。")
        video = videos[0]
        width, height = video.get("width"), video.get("height")
        try:
            if type(fmt.get("duration")) not in (str, int, float):
                raise ValueError("invalid duration type")
            duration = float(fmt["duration"])
        except (TypeError, ValueError, OverflowError) as error:
            raise VideoError("视频时长无效。") from error
        if (type(width) is not int or type(height) is not int or not 0 < width <= 16384
                or not 0 < height <= 16384 or not math.isfinite(duration) or duration <= 0):
            raise VideoError("视频分辨率或时长无效。")
        return VideoMetadata(path, duration, width, height, len(videos), len(audios), final)
