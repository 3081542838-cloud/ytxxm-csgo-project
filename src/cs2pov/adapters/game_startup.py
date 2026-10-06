"""Read current CS2 startup qualification; never command or replay evidence."""
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
import time

from cs2pov.adapters.console_log import ConsoleLog, LogCheckpoint
from cs2pov.adapters.game_compatibility import supports_patch, unsupported_patch_message
from cs2pov.adapters.owned_process import ProcessIdentity
from cs2pov.adapters.replay_log import PREFIX, ReplayLogDecoder
from cs2pov.storage.settings import DataError


MAX_BYTES = 512*1024
MAX_LINE_BYTES = 2048
FILETIME_EPOCH = 11644473600
STARTUP = (
    re.compile(r'\[STARTUP\] \{([0-9]{1,3}(?:\.[0-9]{1,3})?)\} server module init ok'),
    re.compile(r'\[STARTUP\] \{([0-9]{1,3}(?:\.[0-9]{1,3})?)\} created game rules'),
    re.compile(r'\[Client\] CL:  CGameClientConnectPrerequisite connection succeeded'),
    re.compile(r'\[Client\] CL:  \} IGameSystem::LoopActivateAllSystems done'),
)


@dataclass(frozen=True)
class GameStartupEvidence:
    process_identity: ProcessIdentity
    observed_at: float


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class StartupLogReader:
    """One fixed 120-second launch window, bounded current-file cursor.

    Earlier ordered milestones may be more than five seconds old. Only the
    actual final Client milestone grants a five-second bootstrap qualification.
    No polling, unrelated output or new markers can revive expired authority.
    """
    def __init__(self, session, game, patch_version, *, clock=time.monotonic, wall=time.time):
        self.clock, self.wall, self.game = clock, wall, game
        started = self.clock()
        if not _finite(started):
            raise DataError('启动版本或时钟未验证，不能自动查询游戏。')
        if not supports_patch(patch_version):
            raise DataError(unsupported_patch_message(patch_version))
        self.deadline = started+120
        self.state = 'waiting'
        self.proof = None
        self.stage = 0
        self.last_boot_seconds = None
        self.log = ConsoleLog(Path(session).absolute())
        self.identity = None
        self.argv = None
        self.cursor = None
        self.identity = self._guard()
        self.argv = tuple(self.game.argv)
        # FILETIME is public process creation metadata, not process memory.
        self.launch_floor = math.floor(self.identity.created/10_000_000-FILETIME_EPOCH)
        self.timestamps = ReplayLogDecoder(self.identity, '0'*32, clock=self.clock, wall=self.wall)
        if self.log.path.exists():
            self._bind_file()
        self._deadline()

    def _deadline(self):
        now = self.clock()
        if not _finite(now) or now >= self.deadline:
            raise DataError('游戏初始化核验超过原 120 秒期限。')

    def _guard(self):
        try:
            identity = self.game.verify()
            argv = getattr(self.game, 'argv', None)
            if (not isinstance(identity, ProcessIdentity) or type(identity.pid) is not int or identity.pid <= 0
                    or type(identity.created) is not int or identity.created <= 0
                    or not isinstance(identity.executable, str) or not Path(identity.executable).is_absolute()
                    or not isinstance(argv, (list, tuple)) or not argv
                    or any(not isinstance(value, str) for value in argv)
                    or argv.count('-insecure') != 1 or argv.count('-con_logfile') != 1
                    or Path(argv[0]) != Path(identity.executable)):
                raise DataError('缺少完整的受管理 -insecure 启动身份。')
            index = argv.index('-con_logfile')
            if (index+1 >= len(argv) or not Path(argv[index+1]).is_absolute()
                    or Path(argv[index+1]) != self.log.path):
                raise DataError('启动日志没有绑定到当前会话绝对路径。')
            if (self.identity is not None and identity != self.identity
                    or self.argv is not None and tuple(argv) != self.argv):
                raise DataError('启动进程身份或参数发生变化。')
            return identity
        except DataError:
            raise
        except Exception as error:
            raise DataError('无法核验受管理游戏启动身份。') from error

    def _bind_file(self):
        checkpoint = self.log.checkpoint()
        stat = self.log.path.stat()
        birth = getattr(stat, 'st_birthtime', stat.st_ctime)
        if (stat.st_ino != checkpoint.inode or checkpoint.offset > MAX_BYTES
                or not _finite(birth) or birth < self.launch_floor):
            raise DataError('启动日志不是当前新进程的有界文件。')
        # OwnedGame rejected a pre-existing launch log. Read its current
        # startup from byte zero; never join records from a rotated log.
        self.cursor = LogCheckpoint(checkpoint.inode, 0)

    def _source(self, stamp, *, final):
        now = self.wall()
        if not _finite(now):
            raise ValueError('Invalid wall clock')
        current = datetime.fromtimestamp(now)
        candidates = []
        for year in (current.year-1, current.year, current.year+1):
            try:
                candidates.append(datetime.strptime(f'{year}/{stamp}', '%Y/%m/%d %H:%M:%S').timestamp())
            except ValueError:
                continue
        if not candidates:
            raise ValueError('Invalid startup source time')
        source = min(candidates, key=lambda value: abs(value-now))
        age = now-source
        last = self.timestamps.last_source_time
        if not 0 <= age <= 120 or source < self.launch_floor or last is not None and source < last:
            raise ValueError('Startup source is stale, future, prelaunch or reversed')
        if final:
            # Reuse the actual engine source-time guard, including its year,
            # strict five-second freshness and monotonic observation mapping.
            return self.timestamps.source_time(stamp)
        self.timestamps.last_source_time = source
        return self.clock()-age

    def _consume(self, line):
        if (not isinstance(line, str) or not line.endswith('\n')
                or len(line.encode('utf-8')) > MAX_LINE_BYTES or '\x00' in line):
            raise ValueError('Incomplete or oversized startup line')
        body_line = line[:-1]
        if body_line.endswith('\r'):
            body_line = body_line[:-1]
        if '\r' in body_line or '\n' in body_line:
            raise ValueError('Malformed startup line')
        match = PREFIX.fullmatch(body_line)
        if match is None:
            return
        stamp, body = match.groups()
        for index, pattern in enumerate(STARTUP):
            marker = pattern.fullmatch(body)
            if marker is None:
                continue
            if index != self.stage:
                raise ValueError('Startup milestones out of order or replayed')
            observed_at = self._source(stamp, final=index == len(STARTUP)-1)
            if marker.lastindex:
                seconds = float(marker[1])
                if not 0 <= seconds < 120 or self.last_boot_seconds is not None and seconds < self.last_boot_seconds:
                    raise ValueError('Invalid or reversed startup duration')
                self.last_boot_seconds = seconds
            self.stage += 1
            if self.stage == len(STARTUP):
                self.proof = GameStartupEvidence(self.identity, observed_at)
            return
        # Host/Server LoopActivate and command echoes are not Client evidence.

    def poll(self):
        if self.state == 'failed':
            raise DataError('本次启动日志核验已失效，不能重新授权。')
        try:
            self._deadline()
            self._guard()
            if self.cursor is None:
                if not self.log.path.exists():
                    self._guard()
                    self._deadline()
                    return None
                self._bind_file()
            before = self.log.path.stat()
            if before.st_size > MAX_BYTES:
                raise DataError('启动日志超过有界读取限制。')
            content, cursor = self.log.read_after(self.cursor)
            if cursor.offset > MAX_BYTES or self.log.path.stat().st_size > MAX_BYTES:
                raise DataError('读取期间启动日志超过有界限制。')
            for line in content.splitlines(keepends=True):
                self._consume(line)
            self.cursor = cursor
            self._guard()
            self._deadline()
            if self.proof is not None:
                now = self.clock()
                if not _finite(now) or not 0 <= now-self.proof.observed_at <= 5:
                    raise DataError('实际 Client 初始化来源已超过五秒，不能刷新资格。')
                self.state = 'ready'
            return self.proof
        except Exception as error:
            self.state, self.proof = 'failed', None
            if isinstance(error, DataError):
                raise
            raise DataError('启动日志缺失、变化或来源不可靠，停止自动查询。') from error
