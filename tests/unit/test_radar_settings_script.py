import os
from pathlib import Path
import shutil
import subprocess


def test_engine_settings_reader_without_game_or_configuration_side_effects():
    root = Path(__file__).resolve().parents[2]
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    executable = os.environ.get('CS2POV_NODE_PATH') or shutil.which('node') or (str(bundled) if bundled.is_file() else None)
    assert executable, 'Node is required for radar JavaScript behavior tests.'
    result = subprocess.run([executable, str(root / 'tests/js/test_radar_settings.cjs'),
                             str(root / 'src/cs2pov/resources/radar_settings.js')],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS' in result.stdout
