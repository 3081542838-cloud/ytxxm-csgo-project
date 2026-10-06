"""Convert approved PNG into a multi-size Windows ICO; preserve original."""
import io
from pathlib import Path
import struct
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

root = Path(__file__).resolve().parents[1]
source = QImage(str(root / "design/shrimp-app-icon-v2.png"))
if source.isNull():
    raise RuntimeError("Approved shrimp icon is missing or unreadable")
sizes = (16, 24, 32, 48, 64, 128, 256)
frames = []
for size in sizes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled = source.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if not scaled.save(buffer, "PNG"):
        raise RuntimeError("PNG icon conversion failed")
    frames.append(bytes(buffer.data()))
offset = 6 + 16 * len(frames)
header = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
for size, frame in zip(sizes, frames):
    header.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset))
    offset += len(frame)
target = root / "src/cs2pov/resources/shrimp.ico"
target.write_bytes(header + b"".join(frames))
print(target)
