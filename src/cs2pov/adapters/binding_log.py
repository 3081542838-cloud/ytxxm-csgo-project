"""One-request, read-only evidence for the temporary F8 playback binding.

The reader owns its log checkpoint and never consumes ReplayLogReader's cursor.
Reported commands are compared as inert text; this module never executes them.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import time

from cs2pov.adapters.console_log import ConsoleLog
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import PREFIX, ReplayLogDecoder
from cs2pov.storage.settings import DataError


NONCE = re.compile(r"[0-9a-fA-F]{32}")
MARKER = re.compile(r"POV_BIND_(BEGIN|END)_([0-9a-fA-F]{32})")
BINDING = re.compile(r'bind \[player 0\]: "F8" = "([^"\r\n]*)"')
PLAYBACK = re.compile(r"unbind F8; demo_resume; demo_pauseatservertick ([1-9][0-9]{0,9})")
MAX_LINE_BYTES = 2048


def _request(key, nonce):
    if key != "F8" or not isinstance(key, str):
        raise DataError("绑定查询只允许 F8。")
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise DataError("绑定查询必须使用唯一的 32 位十六进制请求标识。")


def _expected(value):
    if value is None:
        return
    if value == 'unbind F8; demo_resume':
        return  # Exact owned-pipe resume; endpoint is separately authorized.
    if not isinstance(value, str):
        raise DataError("预期绑定必须是受限的自解绑播放命令。")
    match = PLAYBACK.fullmatch(value)
    if match is None or int(match[1]) > 2_147_483_647:
        raise DataError("预期绑定必须是受限的自解绑播放命令。")


def binding_query_command(key, nonce):
    """Produce a fixed query, without accepting arbitrary keys or commands."""
    _request(key, nonce)
    return f'echo POV_BIND_BEGIN_{nonce}; bind "F8"; echo POV_BIND_END_{nonce}'


@dataclass(frozen=True)
class BindingEvidence:
    process_identity: ProcessIdentity
    key: str
    value: str
    observed_at: float
    request_nonce: str


class BindingLogDecoder:
    """A terminal state machine: failed or completed requests cannot restart.

    Only an empty binding or the caller's validated exact playback command is
    accepted. Non-Console telemetry may interleave, but cannot supply evidence.
    """

    def __init__(self, identity, nonce, *, expected=None, key="F8",
                 clock=time.monotonic, wall=time.time):
        _request(key, nonce)
        _expected(expected)
        if (not isinstance(identity, ProcessIdentity) or type(identity.pid) is not int
                or identity.pid <= 0 or type(identity.created) is not int
                or identity.created <= 0 or not isinstance(identity.executable, str)
                or not identity.executable):
            raise DataError("绑定查询缺少有效的自有游戏进程身份。")
        self.identity, self.nonce, self.key = identity, nonce, key
        self.expected, self.clock = expected, clock
        self.timestamps = ReplayLogDecoder(identity, nonce, clock=clock, wall=wall)
        self.state = "waiting"
        self.source_observations = []
        self.value = None
        self.proof = None

    def invalidate(self):
        self.state = "invalid"
        self.value = self.proof = None

    def consume(self, line):
        if self.state == "invalid":
            return
        try:
            # ConsoleLog only releases newline-terminated UTF-8 lines. Keep the
            # same contract for direct decoder callers and reject split lines.
            if not isinstance(line, str) or not line.endswith("\n"):
                raise ValueError("Incomplete log line")
            raw = line[:-1]
            if raw.endswith("\r"):
                raw = raw[:-1]
            if ("\r" in raw or "\n" in raw or "\x00" in raw
                    or len(line.encode("utf-8")) > MAX_LINE_BYTES):
                raise ValueError("Unbounded or malformed log line")
            match = PREFIX.fullmatch(raw)
            if match is None:
                return
            stamp, body = match.groups()
            if not body.startswith("[Console] "):
                return
            text = body[len("[Console] "):]
            marker = MARKER.fullmatch(text)
            binding = BINDING.fullmatch(text)
            query_related = (text.startswith("POV_BIND_") or text.startswith("bind "))
            if self.state == "complete":
                # A second response, including another nonce, cannot be merged
                # with or replace this completed request's evidence.
                if query_related:
                    raise ValueError("Duplicate binding response")
                return
            if self.state == "waiting" and not query_related:
                return  # Ordinary console echoes before BEGIN are not evidence.
            observed_at = self.timestamps.source_time(stamp)
            if marker is not None:
                if marker[2] != self.nonce:
                    raise ValueError("Wrong binding request nonce")
                if marker[1] == "BEGIN" and self.state == "waiting":
                    self.state = "binding"
                elif marker[1] == "END" and self.state == "ending":
                    self.state = "complete"
                else:
                    raise ValueError("Duplicate or out-of-order binding marker")
            elif binding is not None and self.state == "binding":
                value = binding[1]
                if value != "" and (self.expected is None or value != self.expected):
                    raise ValueError("Unknown or different binding value")
                self.value = value
                self.state = "ending"
            else:
                raise ValueError("Unknown or out-of-order Console response")
            self.source_observations.append(observed_at)
            if self.state == "complete":
                self.proof = BindingEvidence(self.identity, self.key, self.value,
                                             min(self.source_observations), self.nonce)
        except (ValueError, TypeError, UnicodeError):
            self.invalidate()

    def readback(self):
        if self.proof is None:
            return None
        if not 0 <= self.clock() - self.proof.observed_at <= 5:
            self.invalidate()
            return None
        return self.proof


class BindingLogReader:
    """Read one new query independently; file failures permanently invalidate it."""

    def __init__(self, session: Path, identity, nonce, *, expected=None, key="F8",
                 clock=time.monotonic, wall=time.time):
        self.decoder = BindingLogDecoder(identity, nonce, expected=expected, key=key,
                                         clock=clock, wall=wall)
        self.log = ConsoleLog(session)
        self.cursor = self.log.checkpoint()

    def readback(self):
        try:
            content, cursor = self.log.read_after(self.cursor)
            for line in content.splitlines(keepends=True):
                self.decoder.consume(line)
            self.cursor = cursor
            return self.decoder.readback()
        except (OSError, DataError):
            self.decoder.invalidate()
            raise
