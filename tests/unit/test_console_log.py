from dataclasses import replace
import pytest
from cs2pov.adapters.console_log import ConsoleLog
from cs2pov.storage.settings import DataError


def test_log_reads_only_new_complete_lines_and_keeps_split_unicode(tmp_path):
    path = tmp_path / "engine.log"
    path.write_bytes(b"old history\n")
    log = ConsoleLog(tmp_path); cursor = log.checkpoint()
    value = "玩家核验\n".encode("utf-8")
    with path.open("ab") as file: file.write(value[:4])
    text, partial = log.read_after(cursor)
    assert text == "" and partial == cursor
    with path.open("ab") as file: file.write(value[4:])
    text, final = log.read_after(partial)
    assert text == "玩家核验\n" and final.offset == path.stat().st_size


def test_truncated_rotated_or_oversized_log_blocks(tmp_path):
    path = tmp_path / "engine.log"; path.write_bytes(b"baseline\n")
    log = ConsoleLog(tmp_path); cursor = log.checkpoint()
    path.write_bytes(b"x")
    with pytest.raises(DataError, match="截短"): log.read_after(cursor)
    path.write_bytes(b"baseline\n")
    with pytest.raises(DataError, match="替换"): log.read_after(replace(cursor, inode=cursor.inode + 1))
    path.write_bytes(b"x" * (4 * 1024 * 1024 + 10))
    with pytest.raises(DataError, match="限制"): log.read_after(cursor)


def test_invalid_encoding_blocks_without_discarding_bytes(tmp_path):
    path = tmp_path / "engine.log"; path.write_bytes(b"")
    log = ConsoleLog(tmp_path); cursor = log.checkpoint()
    path.write_bytes(b"\xff\n")
    with pytest.raises(DataError, match="编码"): log.read_after(cursor)
    assert path.read_bytes() == b"\xff\n"
