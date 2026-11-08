"""Draw the app icon and write packaging/icon.icns (macOS only: needs iconutil)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen  # noqa: E402

OUTPUT = Path(__file__).resolve().parents[1] / "packaging" / "icon.icns"


def draw(size):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 1024, size / 1024)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#111923"))
    p.drawRoundedRect(QRectF(100, 100, 824, 824), 185, 185)
    centre = QPointF(512, 650)
    for radius, colour in ((150, "#3ecf8e"), (270, "#5cb8ff"), (390, "#5c9ac4")):
        pen = QPen(QColor(colour), 52, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2), 40 * 16, 100 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#eaf2f9"))
    p.drawEllipse(centre, 62, 62)
    p.end()
    return image


def main():
    _app = QGuiApplication(sys.argv)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            draw(base).save(str(iconset / f"icon_{base}x{base}.png"))
            draw(base * 2).save(str(iconset / f"icon_{base}x{base}@2x.png"))
        OUTPUT.parent.mkdir(exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUTPUT)], check=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
