"""Draws the app icon: three mesh nodes on a rounded square. Writes
meshcore_desktop/icons/app.png and packaging/app.ico."""
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen

root = Path(__file__).resolve().parent.parent
app = QGuiApplication(sys.argv)
S = 256
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
g = QLinearGradient(0, 0, S, S)
g.setColorAt(0, QColor("#1f8a70"))
g.setColorAt(1, QColor("#0b4f6c"))
p.setBrush(g)
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, S - 16, S - 16), 52, 52)
nodes = [QPointF(78, 170), QPointF(178, 178), QPointF(128, 78)]
p.setPen(QPen(QColor(255, 255, 255, 210), 14, Qt.SolidLine, Qt.RoundCap))
for i in range(3):
    p.drawLine(nodes[i], nodes[(i + 1) % 3])
p.setPen(Qt.NoPen)
p.setBrush(QColor("white"))
for n in nodes:
    p.drawEllipse(n, 26, 26)
p.setBrush(QColor("#1f8a70"))
p.drawEllipse(nodes[2], 11, 11)
p.end()
png = root / "meshcore_desktop/icons/app.png"
img.save(str(png))
try:
    from PIL import Image
    Image.open(png).save(root / "packaging/app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
except ImportError:
    print("Pillow missing: no .ico")
