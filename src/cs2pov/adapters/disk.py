"""Fail-closed checks of the actual selected directory and volume."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

MIN_VIDEO_BYTES = 10_000_000_000


class DiskError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiskSnapshot:
    directory: Path
    available_bytes: int
    required_bytes: int


def available_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def writable_probe(directory: Path) -> None:
    # Never reuse a fixed filename or remove a user's file.
    descriptor, name = tempfile.mkstemp(prefix=".cs2pov-probe-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"probe")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        Path(name).unlink(missing_ok=True)


def check_directory(directory: Path, required_bytes: int = MIN_VIDEO_BYTES, *,
                    query: Callable[[Path], int] = available_bytes,
                    probe: Callable[[Path], None] = writable_probe) -> DiskSnapshot:
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError("required_bytes must be a nonnegative integer")
    try:
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise DiskError("存放位置必须是已经存在的文件夹。")
        free = query(directory)
        if type(free) is not int or free < 0:
            raise DiskError("磁盘空间查询返回了无效结果。")
        if free < required_bytes:
            raise DiskError(f"磁盘可用空间不足：需要 {required_bytes} 字节，可用 {free} 字节。")
        probe(directory)
        return DiskSnapshot(directory, free, required_bytes)
    except DiskError:
        raise
    except OSError as error:
        raise DiskError(f"无法检查存放位置：{error}") from error
