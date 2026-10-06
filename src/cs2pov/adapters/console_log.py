"""Bounded session-local console log cursor. Echo is not playback evidence."""
from dataclasses import dataclass
from pathlib import Path
from cs2pov.storage.transaction import no_redirection
from cs2pov.storage.settings import DataError


@dataclass(frozen=True)
class LogCheckpoint:
    inode: int
    offset: int


class ConsoleLog:
    def __init__(self, session: Path):
        self.path = session / "engine.log"
        no_redirection(self.path)

    def checkpoint(self):
        no_redirection(self.path)
        stat = self.path.stat()
        return LogCheckpoint(stat.st_ino, stat.st_size)

    def read_after(self, checkpoint):
        no_redirection(self.path)
        before = self.path.stat()
        if before.st_ino != checkpoint.inode or before.st_size < checkpoint.offset:
            raise DataError("控制台日志被替换或截短，原回读已失效。")
        if before.st_size - checkpoint.offset > 4 * 1024 * 1024:
            raise DataError("控制台输出超出单次读取限制，停止自动核验。")
        with self.path.open("rb") as stream:
            opened = __import__("os").fstat(stream.fileno())
            if opened.st_ino != before.st_ino:
                raise DataError("打开日志时文件身份变化。")
            stream.seek(checkpoint.offset)
            content = stream.read(before.st_size - checkpoint.offset)
        after = self.path.stat()
        if after.st_ino != before.st_ino or after.st_size < before.st_size:
            raise DataError("读取期间日志被替换或截短。")
        # Ignore an incomplete final line until the next read, preserving UTF-8.
        end = content.rfind(b"\n") + 1
        try:
            text = content[:end].decode("utf-8")
        except UnicodeError as error:
            raise DataError("控制台日志编码无法可靠读取。") from error
        return text, LogCheckpoint(before.st_ino, checkpoint.offset + end)
