import os
from pathlib import Path
import shutil
import subprocess


def test_hud_script_behavior_and_no_unrelated_side_effects():
    root = Path(__file__).resolve().parents[2]
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    executable = os.environ.get("CS2POV_NODE_PATH") or shutil.which("node") or (str(bundled) if bundled.is_file() else None)
    assert executable, "Node is needed for development-only HUD JavaScript tests; set CS2POV_NODE_PATH."
    result = subprocess.run([executable, str(root / "tests/js/test_pov_visibility.cjs"),
                             str(root / "src/cs2pov/resources/pov_visibility.js")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
