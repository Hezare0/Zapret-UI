"""Render original vector artwork into a multi-resolution Windows ICO."""

import os
from pathlib import Path
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


root = Path(__file__).resolve().parents[1]
app = QGuiApplication(["icon-renderer"])
renderer = QSvgRenderer(str(root / "assets/zapret-client.svg"))
assert renderer.isValid()
images = []
for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    images.append((size, bytes(data)))
header = struct.pack("<HHH", 0, 1, len(images))
offset = 6 + 16 * len(images)
directory = b""
for size, data in images:
    directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
(root / "assets/zapret-client.ico").write_bytes(header + directory + b"".join(data for _, data in images))
preview = QImage(512, 512, QImage.Format.Format_ARGB32)
preview.fill(0)
painter = QPainter(preview)
renderer.render(painter)
painter.end()
assert preview.save(str(root / "assets/zapret-client.png"))
print(root / "assets/zapret-client.ico")
