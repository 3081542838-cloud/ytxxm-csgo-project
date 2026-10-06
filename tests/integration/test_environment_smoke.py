"""Stage 0: verify selected native dependencies work on this computer."""

import ctypes
from ctypes import wintypes
import importlib.metadata
import sqlite3
import sys

import pytest
from PySide6.QtWidgets import QPushButton
from demoparser2 import DemoParser


def test_supported_python_and_native_dependency_versions():
    assert sys.version_info[:2] == (3, 12)
    assert sys.platform == "win32", "This release targets Windows only"
    for package, version in {
        "PySide6-Essentials": "6.11.2",
        "demoparser2": "0.42.0",
        "pytest": "9.1.1",
        "pytest-qt": "4.5.0",
        "pyinstaller": "6.22.3",
    }.items():
        assert importlib.metadata.version(package) == version


def test_qt_window_and_signal_delivery(qtbot):
    button = QPushButton("环境测试")
    qtbot.addWidget(button)
    button.show()
    with qtbot.waitSignal(button.clicked, timeout=2000):
        button.click()
    assert button.isVisible()
    button.close()
    assert not button.isVisible()


def test_parser_rejects_corrupt_file_without_modifying_it(tmp_path):
    demo = tmp_path / "无效 demo.dem"
    payload = b"not a source 2 demo"
    demo.write_bytes(payload)
    with pytest.raises(Exception):
        DemoParser(str(demo)).parse_header()
    assert demo.read_bytes() == payload


def test_sqlite_transaction_survives_reopen_and_rolls_back(tmp_path):
    path = tmp_path / "test.sqlite"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT)")
        database.execute("INSERT INTO sessions VALUES (?, ?)", ("one", "needs_restore"))
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT state FROM sessions").fetchone() == ("needs_restore",)
        with pytest.raises(RuntimeError):
            with database:
                database.execute("UPDATE sessions SET state = 'complete'")
                raise RuntimeError("simulated interruption")
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT state FROM sessions").fetchone() == ("needs_restore",)


def test_windows_disk_query_reads_actual_target_volume(tmp_path):
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetDiskFreeSpaceExW
    query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                      ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
    query.restype = wintypes.BOOL
    available, total, free = (ctypes.c_ulonglong() for _ in range(3))
    assert query(str(tmp_path), ctypes.byref(available), ctypes.byref(total), ctypes.byref(free))
    assert total.value > 0
    assert 0 <= available.value <= free.value <= total.value
