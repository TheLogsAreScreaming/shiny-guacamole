"""Capture genuine offscreen renderings of the app in demo mode.

Writes docs/screenshots/dashboard-demo.png (live log) and
docs/screenshots/dashboard-filtered.png (node filter applied). Packet data is
seeded, so reruns produce the same content apart from timestamps. Preferences
go to a throwaway file, never to the installed app's settings.
"""
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MESHTASTIC_SNIFFER_SETTINGS", os.path.join(tempfile.mkdtemp(), "settings.ini"))

from PySide6.QtWidgets import QApplication  # noqa: E402
from app import MainWindow  # noqa: E402

app = QApplication(sys.argv)
theme = ROOT / "dashboard.qss"
if theme.exists():
    app.setStyleSheet(theme.read_text(encoding="utf-8"))
random.seed(42)
window = MainWindow()
window.resize(1200, 760)
window.host.setText("192.168.1.50")
window.show()
window.toggle_demo()
for _ in range(40):
    window.demo_packet()
window.table.selectRow(window.table.rowCount() - 1)
app.processEvents()

out_dir = ROOT / "docs" / "screenshots"
out_dir.mkdir(parents=True, exist_ok=True)
shots = []


def capture(name):
    window.refresh_nodes()  # the live view refreshes this on a 300 ms timer
    window.table.setFocus()
    window.table.scrollToBottom()
    app.processEvents()
    path = out_dir / name
    if not window.grab().save(str(path), "PNG"):
        raise RuntimeError(f"Failed to save {path}")
    shots.append(path)


capture("dashboard-demo.png")
node = window.node_table.item(0, 0).text()
window.filter_by_node(0, 0)
window.table.clearSelection()
window.packet_details.clear()
capture("dashboard-filtered.png")
window.demo.stop()
window.close()
for path in shots:
    print(path)
print(f"(filtered by {node})")
