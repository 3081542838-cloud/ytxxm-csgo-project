import os
from cs2pov.adapters.disk import check_directory
from cs2pov.adapters.processes import process_names


def test_real_process_snapshot_contains_current_python():
    processes = process_names()
    assert os.getpid() in processes
    assert processes[os.getpid()].casefold() == "python.exe"


def test_real_disk_probe_does_not_leave_files(tmp_path):
    before = list(tmp_path.iterdir())
    snapshot = check_directory(tmp_path, 1)
    assert snapshot.directory == tmp_path.resolve()
    assert snapshot.available_bytes > 0
    assert list(tmp_path.iterdir()) == before
