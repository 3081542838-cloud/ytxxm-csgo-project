import os
from pathlib import Path
import sys
from cs2pov.adapters.owned_process import process_identity


def test_public_process_metadata_matches_current_python_without_memory_access():
    first = process_identity(os.getpid())
    second = process_identity(os.getpid())
    assert first == second and first.created > 0
    # Windows venv python.exe is a launcher; the real process image is the
    # base interpreter, which Python exposes separately.
    assert Path(first.executable) == Path(sys._base_executable)
