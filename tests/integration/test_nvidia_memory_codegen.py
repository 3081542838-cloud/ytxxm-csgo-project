"""Use the installed comtypes loader under the real read-only observer guard."""
import json
from pathlib import Path
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[2]


def test_nonexistent_frozen_comtypes_directory_never_creates_cache(tmp_path):
    nonexistent = tmp_path / 'frozen' / '_internal' / 'comtypes' / '__init__.py'
    script = r'''
import json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import comtypes
comtypes.__file__ = sys.argv[2]
# Read the real Windows typelib and exercise real memory code generation,
# while replacing object creation so no UI Automation object reads a desktop.
comtypes.CoCreateInstance = lambda *a, **kw: SimpleNamespace(
    RawViewWalker='readonly-test-walker', ControlViewWalker='readonly-test-walker')
from cs2pov.adapters.nvidia_status import _NativeUIA, _readonly_audit
sys.frozen = True
# Point to this test's existing temporary directory without tempfile's initial
# writability probe, so the regression reaches comtypes' cache mkdir itself.
tempfile.tempdir = str(Path(sys.argv[2]).parents[3])
writes = []
def guard(event, args):
    if event == 'os.mkdir':
        writes.append({'event': event, 'cache': 'comtypes_cache' in str(args[0])})
    _readonly_audit(event, args)
sys.addaudithook(guard)
backend = object.__new__(_NativeUIA)
backend.uia = backend.walker = None
backend.raw_view = True
try:
    backend._load()
    result = {'ok': backend.uia is not None,
              'walker': backend.walker,
              'memory_only': comtypes.client.gen_dir is None,
              'writes': writes,
              'fake_package_created': Path(sys.argv[2]).parent.exists()}
except Exception as error:
    result = {'ok': False, 'error_type': type(error).__name__, 'writes': writes,
              'fake_package_created': Path(sys.argv[2]).parent.exists()}
print(json.dumps(result), flush=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(PROJECT / 'src'), str(nonexistent)],
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['ok'] is True, evidence
    assert evidence['walker'] == 'readonly-test-walker'
    assert evidence['memory_only'] is True
    assert evidence['writes'] == []
    assert evidence['fake_package_created'] is False
    assert not nonexistent.parent.exists()
