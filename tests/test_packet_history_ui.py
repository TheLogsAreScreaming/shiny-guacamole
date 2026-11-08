"""Offscreen UI regression tests for the bounded packet window."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from app import MainWindow


class PacketHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_history_rollover_keeps_stats_and_table_consistent(self):
        window = MainWindow()
        try:
            window.max_packets = 3
            for node, kind in [
                ("!a", "TEXT_MESSAGE_APP"),
                ("!b", "POSITION_APP"),
                ("!b", "POSITION_APP"),
                ("!c", "NODEINFO_APP"),
            ]:
                window.add_packet({"time": "12:00:00", "from": node, "type": kind, "rssi": "-80", "snr": "5"})
            self.assertEqual(len(window.rows), 3)
            self.assertEqual(window.table.rowCount(), 3)
            self.assertEqual(window.seen_nodes, {"!b", "!c"})
            self.assertEqual(window.port_counts, {"POSITION_APP": 2, "NODEINFO_APP": 1})
            self.assertIn("Packets: 3", window.stats.text())
        finally:
            window.close()

    def test_clear_resets_node_summary(self):
        window = MainWindow()
        try:
            window.add_packet({"time": "12:00:00", "from": "!a", "type": "POSITION_APP", "rssi": "", "snr": ""})
            window.clear_log()
            self.assertEqual(window.table.rowCount(), 0)
            self.assertEqual(window.node_table.rowCount(), 0)
            self.assertEqual(window.rows, [])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
