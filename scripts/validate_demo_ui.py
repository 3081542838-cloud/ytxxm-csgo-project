"""Real Demo + real QProcess + offscreen Qt review, without game control."""
import argparse
import json
import os
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from cs2pov.services.workspace import Workspace
from cs2pov.ui.window import MainWindow

parser = argparse.ArgumentParser()
parser.add_argument("--demo", type=Path, required=True)
parser.add_argument("--data", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
app = QApplication([])
QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"))
model = Workspace(args.data)
window = MainWindow(model)
window.show()
model.import_demo(args.demo)
loop = QEventLoop()
model.start_parse()
model.changed.connect(lambda: loop.quit() if not model.busy else None)
timer = QTimer(); timer.setSingleShot(True); timer.timeout.connect(loop.quit); timer.start(45000)
loop.exec()
if model.busy:
    model.cancel()
    raise RuntimeError("Background parse timed out")
if model.analysis is None:
    raise RuntimeError(model.parse_error)
own = "76561199198478034"
index = window.player_choice.findData(own)
if index < 0:
    raise RuntimeError("Expected sample player absent")
window.player_choice.setCurrentIndex(index)
window.range_mode.setCurrentIndex(window.range_mode.findData("time"))
window.range_start.setValue(12602 / 64)
window.range_end.setValue(13400 / 64)
window.save_clip.click()
draft = model.library.draft()
assert draft["selection"]["server_start_tick"] == 15934
assert draft["selection"]["server_end_tick"] == 16732
args.output.mkdir(parents=True, exist_ok=True)
for index, name in ((0, "home"), (1, "demo")):
    window.navigate(index)
    app.processEvents()
    assert window.grab().save(str(args.output / (name + ".png")))
(args.output / "result.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"map": model.analysis["map"], "players": len(model.analysis["players"]),
                  "rounds": len(model.analysis["rounds"]), "clip": draft["selection"]}, ensure_ascii=True))
window.close()
model.library.close()
