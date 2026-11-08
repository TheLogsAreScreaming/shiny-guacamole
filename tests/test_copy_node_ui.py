"""Exercise the Copy Node ID interaction with an offscreen Qt clipboard."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app import MainWindow


class CopyNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_copy_selected_node_id(self):
        window = MainWindow()
        try:
            window.add_packet({"time": "12:00:00", "from": "!a1b2c3d4", "type": "POSITION_APP", "rssi": "-80", "snr": "5"})
            window.node_table.selectRow(0)
            window.copy_selected_node()
            self.assertEqual(QApplication.clipboard().text(), "!a1b2c3d4")
            self.assertIn("Copied node ID", window.statusBar().currentMessage())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
