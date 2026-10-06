"""Render the real Qt widgets offscreen for layout review, with isolated data."""
import argparse
from pathlib import Path
import tempfile
import os
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cs2pov.services.workspace import Workspace
from cs2pov.ui.window import MainWindow

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--width", type=int, default=1180)
parser.add_argument("--height", type=int, default=820)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
# Qt's offscreen platform does not enumerate Windows system fonts.
# Read the installed font for the review process; do not copy/distribute it.
font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
if QFontDatabase.addApplicationFont(str(font)) < 0:
    raise RuntimeError("Chinese font unavailable; cannot review unreadable screenshots")
with tempfile.TemporaryDirectory() as directory:
    workspace = Workspace(Path(directory))
    window = MainWindow(workspace)
    window.resize(args.width, args.height)
    window.show()
    for index, name in enumerate(("home", "demo", "hud", "records", "settings")):
        window.navigate(index)
        app.processEvents()
        QTest.qWait(220)
        if not window.grab().save(str(args.output / (name + ".png"))):
            raise RuntimeError("Screenshot write failed")
    window.close()
    workspace.library.close()
