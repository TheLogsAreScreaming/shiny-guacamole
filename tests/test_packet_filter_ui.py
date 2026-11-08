"""Offscreen UI tests for search and node-based filtering."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from app import MainWindow


class PacketFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_filter_matches_node_and_port_case_insensitively(self):
        window = MainWindow()
        try:
            for node, kind in [("!abc", "TEXT_MESSAGE_APP"), ("!def", "POSITION_APP")]:
                window.add_packet({"time": "12:00:00", "from": node, "type": kind, "rssi": "", "snr": ""})
            window.filter_input.setText("position")
            self.assertTrue(window.table.isRowHidden(0))
            self.assertFalse(window.table.isRowHidden(1))
            window.filter_input.setText("!ABC")
            self.assertFalse(window.table.isRowHidden(0))
            self.assertTrue(window.table.isRowHidden(1))
        finally:
            window.close()

    def test_double_click_node_sets_filter(self):
        window = MainWindow()
        try:
            window.add_packet({"time": "12:00:00", "from": "!abc", "type": "TEXT_MESSAGE_APP", "rssi": "", "snr": ""})
            window.filter_by_node(0, 0)
            self.assertEqual(window.filter_input.text(), "!abc")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
