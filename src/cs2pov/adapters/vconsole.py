"""Bounded Source 2 VConsole framing. A log message is not replay evidence.

Protocol reference: https://github.com/oxijoined/vconsole-python
This module does not launch a listener or send commands to a running game.
"""
import struct


class VConsoleError(ValueError):
    pass


VERSION = 0x00D40000
HEADER = struct.Struct(">4sIHH")


def command_packet(command):
    if (not isinstance(command, str) or not command or len(command) > 4096
            or any(ord(c) < 32 for c in command)):
        raise VConsoleError("控制台命令为空、过长或包含控制字符。")
    try:
        body = command.encode("utf-8") + b"\0"
    except UnicodeError as error:
        raise VConsoleError("命令编码无效。") from error
    return HEADER.pack(b"CMND", VERSION, HEADER.size + len(body), 0) + body


class FrameReader:
    """Incremental bounded framing; never search garbage for a magic signature."""
    def __init__(self):
        self.pending = bytearray()
        self.failed = False

    def feed(self, chunk):
        if self.failed:
            raise VConsoleError("控制台通路已失效。")
        try:
            if len(self.pending) + len(chunk) > 131072:
                raise VConsoleError("控制台缓冲超限。")
            self.pending.extend(chunk)
            result = []
            while len(self.pending) >= HEADER.size:
                kind, version, length, handle = HEADER.unpack_from(self.pending)
                if any(c < 65 or c > 90 for c in kind) or length < HEADER.size:
                    raise VConsoleError("控制台包头无效。")
                if len(self.pending) < length:
                    break
                result.append((kind, version, handle, bytes(self.pending[HEADER.size:length])))
                del self.pending[:length]
            return result
        except Exception:
            self.failed = True
            self.pending.clear()
            raise

    def finish(self):
        if self.pending:
            self.failed = True
            self.pending.clear()
            raise VConsoleError("控制台连接在包中间断开。")


def print_text(frame):
    kind, version, handle, body = frame
    if kind != b"PRNT":
        return None
    if len(body) < 29 or not body.endswith(b"\0"):
        raise VConsoleError("控制台文本包截断。")
    try:
        return body[28:].split(b"\0", 1)[0].decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise VConsoleError("控制台文本编码无效。") from error
