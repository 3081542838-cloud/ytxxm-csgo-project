"""Owned, session-local CS2 command pipes; delivery is never replay evidence.

Both servers exist before game launch. There is no listener on the network,
window input, process memory access, reconnection, or automatic command retry.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import re
import secrets
import sys
import threading
import time

from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.storage.settings import DataError


class CommandPipeError(DataError):
    pass


def _payload(command):
    if (not isinstance(command, str) or not 0 < len(command) <= 512
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in command)):
        raise CommandPipeError('管道命令为空、过长或包含控制字符。')
    return (command+'\n').encode('utf-8')


class CommandPipes:
    """One serial owner; callers separately persist one-use input authority."""
    def __init__(self, *, backend=None, clock=time.monotonic, nonce=...):
        nonce = secrets.token_hex(16) if nonce is ... else nonce
        if not isinstance(nonce, str) or re.fullmatch(r'[0-9a-f]{32}', nonce) is None:
            raise CommandPipeError('管道会话标识无效。')
        self.paths = tuple(r'\\.\pipe\cs2pov_'+nonce+'_'+kind for kind in ('cmd', 'out'))
        self.backend = Win32PipeBackend() if backend is None else backend
        self.clock = clock
        self.state = 'new'
        self.handles = []
        self.identity = self.game = self.deadline = None
        self.cleanup_errors = ()
        self._lock = threading.RLock()

    @property
    def argument(self):
        if self.state not in ('created', 'connected') or len(self.handles) != 2:
            raise CommandPipeError('两条命令管道尚未完整创建。')
        return ','.join(self.paths)

    def create(self):
        with self._lock:
            if self.state != 'new':
                raise CommandPipeError('命令管道不能重复创建。')
            try:
                for path in self.paths:
                    handle = self.backend.create(path)
                    if type(handle) is not int or handle <= 0:
                        raise CommandPipeError('命令管道创建结果无效。')
                    self.handles.append(handle)
                self.state = 'created'
                self.deadline = self.clock()+120
                return self
            except Exception as error:
                self._fail()
                raise CommandPipeError('命令管道创建失败；未授权启动游戏。') from error

    def _guard(self, game):
        identity = game.verify()
        argv = getattr(game, 'argv', None)
        if (not isinstance(identity, ProcessIdentity) or type(identity.pid) is not int or identity.pid <= 0
                or type(identity.created) is not int or identity.created <= 0
                or not isinstance(identity.executable, str) or not identity.executable
                or not isinstance(argv, (list, tuple)) or '-insecure' not in argv):
            raise CommandPipeError('管道缺少完整的受管理 -insecure 游戏身份。')
        if self.identity is not None and (identity != self.identity or game is not self.game):
            raise CommandPipeError('命令管道所属游戏身份变化。')
        return identity

    def _connection_guard(self, game):
        identity = self._guard(game)
        for handle in self.handles:
            pid = self.backend.client_pid(handle)
            if type(pid) is not int or pid != identity.pid:
                raise CommandPipeError('管道客户端不是本次受管理 CS2。')
        self.backend.drain(self.handles[1], limit=65536)
        if self._guard(game) != identity:
            raise CommandPipeError('管道检查期间游戏身份变化。')
        return identity

    def poll_connection(self, game):
        with self._lock:
            if self.state not in ('created', 'connected'):
                raise CommandPipeError('命令管道未创建、已失败或已关闭。')
            try:
                if self.state == 'created' and self.clock() >= self.deadline:
                    raise CommandPipeError('等待游戏命令管道连接超时。')
                identity = self._guard(game)
                if self.identity is None:
                    self.identity, self.game = identity, game
                connected = [self.backend.connect(handle) for handle in self.handles]
                if self._guard(game) != identity:
                    raise CommandPipeError('连接期间游戏身份变化。')
                if any(type(value) is not bool for value in connected):
                    raise CommandPipeError('管道连接状态无效。')
                if self.state == 'created' and self.clock() >= self.deadline:
                    raise CommandPipeError('连接检查不能延长原等待期限。')
                if not all(connected):
                    return False
                self._connection_guard(game)
                if self.state == 'created' and self.clock() >= self.deadline:
                    raise CommandPipeError('连接检查不能延长原等待期限。')
                self.state = 'connected'
                return True
            except Exception:
                self._fail()
                raise

    def send_command(self, command, game, *, timeout=1, deadline=None):
        payload = _payload(command)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5):
            raise CommandPipeError('管道输入期限无效。')
        if deadline is not None and (type(deadline) not in (int,float)
                or not math.isfinite(deadline) or deadline < 0):
            raise CommandPipeError('管道输入原截止时间无效。')
        # Freeze before taking the serial lock: waiting or processing cannot
        # turn a caller's remaining budget into a later absolute deadline.
        dispatch_deadline = self.clock()+timeout
        if deadline is not None:
            dispatch_deadline = min(dispatch_deadline,deadline)
        with self._lock:
            if self.state != 'connected':
                raise CommandPipeError('命令管道没有有效连接；不发送或重发。')
            deadline = dispatch_deadline
            attempted = False
            try:
                if self.clock() >= deadline:
                    raise CommandPipeError('管道输入等待超过原期限；未发送命令。')
                self._connection_guard(game)
                if self.clock() >= deadline:
                    raise CommandPipeError('管道输入检查超时；未发送命令。')
                attempted = True
                count = self.backend.write(self.handles[0], payload, deadline=deadline, clock=self.clock)
                if type(count) is not int or count != len(payload):
                    raise CommandPipeError('管道输入未完整写入。')
                self._connection_guard(game)
                if self.clock() >= deadline:
                    raise CommandPipeError('管道输入超过原期限。')
                # A complete byte count never claims CS2 executed the command.
                return None
            except Exception as error:
                self._fail()
                if attempted:
                    raise CommandPipeError('管道命令结果未知，通路已撤销，禁止自动重试。') from error
                raise

    def _release(self):
        errors = []
        retained = []
        for handle in self.handles:
            try:
                self.backend.close(handle)
            except Exception as error:
                errors.append(error)
                retained.append(handle)
        self.handles[:] = retained
        backend_errors = getattr(self.backend, 'cleanup_errors', ())
        if isinstance(backend_errors, (tuple, list)):
            errors.extend(CommandPipeError(str(error)) for error in backend_errors)
        self.cleanup_errors = tuple(dict.fromkeys(str(error) for error in errors))
        return errors

    def _fail(self):
        self.state = 'failed'
        self._release()

    def close(self):
        with self._lock:
            self.state = 'closed'
            errors = []
            reaper = getattr(self.backend, 'reap', None)
            if callable(reaper):
                try:
                    reaper()
                except Exception as error:
                    errors.append(error)
            # Always attempt the remaining pipe handles even if a retained
            # event could not be closed. Cleanup can never restore input.
            errors.extend(self._release())
            self.cleanup_errors = tuple(dict.fromkeys(str(error) for error in errors))
            if errors:
                raise CommandPipeError('命令管道句柄清理失败；未恢复输入资格。')


_ULONG_PTR = ctypes.c_size_t


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', _ULONG_PTR), ('InternalHigh', _ULONG_PTR),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', wintypes.LPVOID),
                ('bInheritHandle', wintypes.BOOL)]


@dataclass
class _Operation:
    handle: int
    overlap: _OVERLAPPED
    buffer: object = None
    size: int = 0
    terminal: bool = False
    owner: object = None


# CloseHandle can return before cancellation has completed. Retain operation
# storage and its event independently of the facade's lifetime until signalled.
_RETIRED = []
_RETIRED_LOCK = threading.Lock()


class Win32PipeBackend:
    """Bounded local Win32 pipe I/O. No flush, reconnect or unbounded waits."""
    def __init__(self):
        if sys.platform != 'win32':
            raise CommandPipeError('命令管道仅支持 Windows。')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.security = ctypes.WinDLL('advapi32', use_last_error=True)
        self._prototypes()
        self._owned = set()
        self._connected = set()
        self._connects = {}
        self._reads = {}
        self._writes = {}
        self._cleanup_failures = {}

    @property
    def cleanup_errors(self):
        return tuple(self._cleanup_failures.values())

    def reap(self):
        """Explicit nonblocking cleanup retry; it never reconnects a pipe."""
        self._reap()

    def _prototypes(self):
        api = self.api
        declarations = {
            'CreateNamedPipeW': (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SECURITY_ATTRIBUTES)]),
            'ConnectNamedPipe': (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]),
            'GetNamedPipeClientProcessId': (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]),
            'CreateEventW': (wintypes.HANDLE, [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
            'GetOverlappedResult': (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]),
            'CancelIoEx': (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]),
            'WaitForSingleObject': (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
            'CloseHandle': (wintypes.BOOL, [wintypes.HANDLE]),
            'GetCurrentProcess': (wintypes.HANDLE, []),
            'LocalFree': (wintypes.HLOCAL, [wintypes.HLOCAL]),
        }
        for name in ('ReadFile', 'WriteFile'):
            declarations[name] = (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED)])
        for name, (result, args) in declarations.items():
            function = getattr(api, name)
            function.restype, function.argtypes = result, args
        adv = self.security
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    def _error(self, label):
        return CommandPipeError(f'{label}（Windows {ctypes.get_last_error()}）。')

    def _descriptor(self):
        token = wintypes.HANDLE()
        sid_string = wintypes.LPWSTR()
        if not self.security.OpenProcessToken(self.api.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise self._error('无法读取本用户管道权限')
        try:
            needed = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not 0 < needed.value <= 65536:
                raise self._error('用户令牌长度无效')
            storage = ctypes.create_string_buffer(needed.value)
            if not self.security.GetTokenInformation(token, 1, storage, needed, ctypes.byref(needed)):
                raise self._error('无法读取用户身份')
            sid = ctypes.cast(storage, ctypes.POINTER(wintypes.LPVOID)).contents.value
            if not self.security.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
                raise self._error('用户 SID 转换失败')
            descriptor = wintypes.LPVOID()
            sddl = 'D:P(A;;GA;;;SY)(A;;GA;;;'+sid_string.value+')'
            if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(descriptor), None):
                raise self._error('管道权限创建失败')
            return descriptor
        finally:
            if sid_string:
                self.api.LocalFree(ctypes.cast(sid_string, wintypes.HLOCAL))
            self.api.CloseHandle(token)

    def _reap(self):
        errors = []
        with _RETIRED_LOCK:
            for api, operation in list(_RETIRED):
                if operation.terminal or api.WaitForSingleObject(operation.overlap.hEvent, 0) == 0:
                    operation.terminal = True
                    if api.CloseHandle(operation.overlap.hEvent):
                        _RETIRED.remove((api, operation))
                        if operation.owner is not None:
                            operation.owner._cleanup_failures.pop(('event', operation.overlap.hEvent), None)
                    else:
                        error = self._error('管道事件关闭失败，仍保留操作存储')
                        if operation.owner is not None:
                            operation.owner._cleanup_failures[('event', operation.overlap.hEvent)] = str(error)
                        errors.append(error)
            if len(_RETIRED) >= 64:
                raise CommandPipeError('仍有未完成的管道取消，拒绝创建更多通路。')
        if errors:
            raise errors[0]

    def _retire(self, operation):
        with _RETIRED_LOCK:
            if not any(api is self.api and held is operation for api, held in _RETIRED):
                _RETIRED.append((self.api, operation))

    def create(self, path):
        self._reap()
        descriptor = self._descriptor()
        attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False)
        try:
            # DUPLEX | FIRST_PIPE_INSTANCE | OVERLAPPED; byte/wait mode |
            # REJECT_REMOTE_CLIENTS, one instance. All names are generated above.
            handle = self.api.CreateNamedPipeW(path, 0x00000003 | 0x00080000 | 0x40000000,
                0x00000008, 1, 65536, 65536, 0, ctypes.byref(attributes))
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise self._error('本地命令管道创建失败')
            self._owned.add(handle)
            return handle
        finally:
            self.api.LocalFree(descriptor)

    def _operation(self, handle, *, buffer=None, size=0):
        if handle not in self._owned:
            raise CommandPipeError('管道句柄已关闭或不属于当前通路。')
        event = self.api.CreateEventW(None, True, False, None)
        if not event:
            raise self._error('管道事件创建失败')
        return _Operation(handle, _OVERLAPPED(hEvent=event), buffer, size, owner=self)

    def _complete(self, operation):
        count = wintypes.DWORD()
        if self.api.GetOverlappedResult(operation.handle, ctypes.byref(operation.overlap),
                                        ctypes.byref(count), False):
            return count.value
        error = ctypes.get_last_error()
        if error == 996:  # ERROR_IO_INCOMPLETE
            return None
        raise self._error('管道异步操作失败')

    def _release_operation(self, operation):
        # This method is called only after OS completion or an immediate,
        # nonpending I/O result. A nonsignalled event can still be safely
        # retried after CloseHandle fails; retain the entire operation.
        operation.terminal = True
        if not self.api.CloseHandle(operation.overlap.hEvent):
            error = self._error('管道事件关闭失败，仍保留操作存储')
            self._cleanup_failures[('event', operation.overlap.hEvent)] = str(error)
            self._retire(operation)
            raise error
        self._cleanup_failures.pop(('event', operation.overlap.hEvent), None)

    def connect(self, handle):
        self._reap()
        if handle in self._connected:
            return True
        operation = self._connects.get(handle)
        if operation is None:
            operation = self._operation(handle)
            self._connects[handle] = operation
            if not self.api.ConnectNamedPipe(handle, ctypes.byref(operation.overlap)):
                error = ctypes.get_last_error()
                if error == 535:  # client connected before ConnectNamedPipe
                    self._connects.pop(handle)
                    self._release_operation(operation)
                    self._connected.add(handle)
                    return True
                if error != 997:  # ERROR_IO_PENDING
                    failure = self._error('管道连接失败')
                    self._connects.pop(handle)
                    self._release_operation(operation)
                    raise failure
        if self._complete(operation) is None:
            return False
        self._connects.pop(handle)
        self._release_operation(operation)
        self._connected.add(handle)
        return True

    def client_pid(self, handle):
        if handle not in self._connected or handle not in self._owned:
            raise CommandPipeError('管道尚未建立客户端连接。')
        pid = wintypes.ULONG()
        if not self.api.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
            raise self._error('管道客户端身份读取失败')
        return pid.value

    def drain(self, handle, *, limit):
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise CommandPipeError('管道输出读取限额无效。')
        total = 0
        # At most sixteen 4 KiB operations; pending data is kept for next poll.
        for _ in range(16):
            if total >= limit:
                break
            operation = self._reads.get(handle)
            if operation is None:
                size = min(4096, limit-total)
                operation = self._operation(handle, buffer=ctypes.create_string_buffer(size), size=size)
                self._reads[handle] = operation
                ignored = wintypes.DWORD()
                if not self.api.ReadFile(handle, operation.buffer, size, ctypes.byref(ignored),
                                          ctypes.byref(operation.overlap)) and ctypes.get_last_error() != 997:
                    failure = self._error('管道输出读取失败')
                    self._reads.pop(handle)
                    self._release_operation(operation)
                    raise failure
            count = self._complete(operation)
            if count is None:
                break
            if not 0 < count <= operation.size or total+count > limit:
                raise CommandPipeError('管道输出长度无效。')
            self._reads.pop(handle)
            self._release_operation(operation)
            total += count
        return total  # discarded, never parsed as execution evidence

    def write(self, handle, payload, *, deadline, clock):
        if handle in self._writes or clock() >= deadline:
            raise CommandPipeError('管道输入尚未结束或已经到期。')
        operation = self._operation(handle, buffer=ctypes.create_string_buffer(payload), size=len(payload))
        self._writes[handle] = operation
        ignored = wintypes.DWORD()
        if not self.api.WriteFile(handle, operation.buffer, operation.size, ctypes.byref(ignored),
                                   ctypes.byref(operation.overlap)) and ctypes.get_last_error() != 997:
            failure = self._error('管道输入写入失败')
            self._writes.pop(handle)
            self._release_operation(operation)
            raise failure
        while True:
            count = self._complete(operation)
            if count is not None:
                self._writes.pop(handle)
                self._release_operation(operation)
                return count
            remaining = deadline-clock()
            if remaining <= 0:
                self.api.CancelIoEx(handle, ctypes.byref(operation.overlap))
                # Storage stays owned until close() confirms completion or
                # transfers it to the retired operations list.
                raise CommandPipeError('管道输入超时，已请求取消，执行结果未知。')
            result = self.api.WaitForSingleObject(operation.overlap.hEvent, min(50, max(1, int(remaining*1000))))
            if result not in (0, 258):
                raise self._error('管道输入等待失败')

    def close(self, handle):
        if handle not in self._owned:
            return
        operations = []
        for table in (self._connects, self._reads, self._writes):
            operation = table.pop(handle, None)
            if operation is not None:
                operations.append(operation)
                self.api.CancelIoEx(handle, ctypes.byref(operation.overlap))
        if not self.api.CloseHandle(handle):
            # Retain storage even if the OS cannot close the pipe. No caller
            # receives reusable authority over this handle.
            error = self._error('管道句柄关闭失败')
            self._cleanup_failures[('pipe', handle)] = str(error)
            for operation in operations:
                self._retire(operation)
            self._connected.discard(handle)
            raise error
        self._owned.remove(handle)
        self._cleanup_failures.pop(('pipe', handle), None)
        self._connected.discard(handle)
        errors = []
        for operation in operations:
            if self.api.WaitForSingleObject(operation.overlap.hEvent, 0) == 0:
                try:
                    self._release_operation(operation)
                except CommandPipeError as error:
                    errors.append(error)
            else:
                self._retire(operation)
        if errors:
            raise errors[0]
