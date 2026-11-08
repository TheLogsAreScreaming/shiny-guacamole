"""Long-session stability and diagnostics regression tests (offscreen)."""
import logging
import os
import random
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from app import MainWindow, configure_logging, format_duration
from test_connection_lifecycle import FakeBus, LOST_TOPIC, RECEIVE_TOPIC, Radio, pump_until

NODES = [f"!{i:08x}" for i in range(40)]
PORTS = ["TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP", ""]


def packet(rng):
    return {"time": "12:00:00", "from": rng.choice(NODES + [""]), "type": rng.choice(PORTS), "rssi": "-80", "snr": "5"}


class LongSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()

    def test_incremental_tallies_match_a_full_recount_after_rollover(self):
        rng = random.Random(7)
        self.window.max_packets = 300
        for _ in range(1000):
            self.window.add_packet(packet(rng))
        rows = self.window.rows
        self.assertEqual(len(rows), 300)
        self.assertEqual(self.window.table.rowCount(), 300)
        expected_nodes = Counter(row["from"] for row in rows if row["from"])
        expected_ports = Counter(row["type"] or "UNKNOWN" for row in rows)
        self.assertEqual(self.window.node_counts, dict(expected_nodes))
        self.assertEqual(self.window.seen_nodes, set(expected_nodes))
        self.assertEqual(self.window.port_counts, dict(expected_ports))
        self.window.refresh_nodes()
        table = {
            self.window.node_table.item(r, 0).text(): int(self.window.node_table.item(r, 1).text())
            for r in range(self.window.node_table.rowCount())
        }
        self.assertEqual(table, dict(expected_nodes))

    def test_active_filter_applies_to_new_packets(self):
        self.window.filter_input.setText("position")
        self.window.add_packet({"time": "1", "from": "!a", "type": "TEXT_MESSAGE_APP", "rssi": "", "snr": ""})
        self.window.add_packet({"time": "1", "from": "!b", "type": "POSITION_APP", "rssi": "", "snr": ""})
        self.assertTrue(self.window.table.isRowHidden(0))
        self.assertFalse(self.window.table.isRowHidden(1))

    def test_node_table_refresh_is_throttled_but_catches_up(self):
        self.window.add_packet({"time": "1", "from": "!a", "type": "X", "rssi": "", "snr": ""})
        self.assertEqual(self.window.node_table.rowCount(), 1)
        self.window.add_packet({"time": "1", "from": "!b", "type": "X", "rssi": "", "snr": ""})
        self.assertEqual(self.window.node_table.rowCount(), 1)
        self.assertTrue(pump_until(self.app, lambda: self.window.node_table.rowCount() == 2))

    def test_clear_log_resets_incremental_counts(self):
        self.window.add_packet({"time": "1", "from": "!a", "type": "X", "rssi": "", "snr": ""})
        self.window.clear_log()
        self.assertEqual(self.window.node_counts, {})
        self.window.add_packet({"time": "1", "from": "!b", "type": "X", "rssi": "", "snr": ""})
        self.assertEqual(self.window.node_counts, {"!b": 1})

    def test_packet_cost_does_not_grow_with_full_history(self):
        # Before this change each packet at the 5,000-row cap took ~100 ms
        # (full-table rescans, unthrottled scrollToBottom, one redraw per packet).
        rng = random.Random(3)
        self.window.show()
        for index in range(self.window.max_packets):
            self.window.add_packet(packet(rng))
            if index % 250 == 0:
                self.app.processEvents()
        self.app.processEvents()
        started = time.perf_counter()
        for _ in range(300):
            self.window.queue_packet(packet(rng))
            self.app.processEvents()
        self.assertTrue(pump_until(self.app, lambda: not self.window.pending_packets))
        per_packet_ms = (time.perf_counter() - started) / 300 * 1000
        self.assertEqual(len(self.window.rows), self.window.max_packets)
        self.assertLess(per_packet_ms, 10, f"{per_packet_ms:.2f} ms per packet at the history cap")

    def test_live_packets_are_applied_in_batches(self):
        for index in range(5):
            self.window.queue_packet({"time": "1", "from": f"!{index}", "type": "X", "rssi": "", "snr": ""})
        self.assertEqual(self.window.rows, [])
        self.assertTrue(pump_until(self.app, lambda: len(self.window.rows) == 5))
        self.assertEqual(self.window.table.rowCount(), 5)

    def test_clear_log_drops_buffered_packets(self):
        self.window.queue_packet({"time": "1", "from": "!a", "type": "X", "rssi": "", "snr": ""})
        self.window.clear_log()
        pump_until(self.app, lambda: False, timeout=0.4)
        self.assertEqual(self.window.rows, [])

    def test_packet_details_show_one_field_per_line(self):
        self.window.add_packet({"time": "12:00:00", "from": "!a", "type": "POSITION_APP", "rssi": "-80", "snr": "5"})
        self.window.table.selectRow(0)
        self.window.show_packet_details()
        text = self.window.packet_details.toPlainText()
        self.assertEqual(text.splitlines()[:2], ["time: 12:00:00", "from: !a"])
        self.assertNotIn("\\n", text)


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bus = FakeBus()
        self.radio = Radio(self.bus)
        self.window = MainWindow(radio_factory=self.radio.thread)
        self.window.host.setText("192.0.2.10")

    def tearDown(self):
        self.window.disconnect_radio()
        if self.window.radio is not None:
            self.window.radio.wait(3000)
        self.window.close()

    def connect(self):
        self.window.connect_radio()
        radio = self.window.radio
        self.assertTrue(pump_until(self.app, lambda: self.window.session_started is not None))
        return radio

    def test_session_summary_tracks_packets_and_reconnects(self):
        radio = self.connect()
        self.assertIn("no packets yet", self.window.session_summary())
        self.bus.sendMessage(RECEIVE_TOPIC, packet={"fromId": "!a"}, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: len(self.window.rows) == 1))
        self.assertIn("1 packets this session", self.window.session_summary())
        self.bus.sendMessage(LOST_TOPIC, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        self.assertIn("Waiting to reconnect", self.window.session_summary())
        self.assertIn("last drop", self.window.session_summary())
        self.window.retry_timer.stop()
        self.window._retry_connect()
        self.assertTrue(pump_until(self.app, lambda: self.window.session_started is not None))
        self.assertEqual(self.window.reconnects, 1)
        self.assertIn("reconnects: 1", self.window.session_summary())

    def test_manual_connect_resets_reconnect_history(self):
        radio = self.connect()
        self.bus.sendMessage(LOST_TOPIC, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        self.window.disconnect_radio()
        self.connect()
        self.assertEqual(self.window.reconnects, 0)
        self.assertEqual(self.window.last_drop, "")

    def test_diagnostics_report_lists_connection_events(self):
        radio = self.connect()
        self.bus.sendMessage(LOST_TOPIC, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        report = self.window.diagnostics_report()
        for expected in ("Connecting to 192.0.2.10", "Connected to 192.0.2.10", "Session ended (lost)", "Reconnect attempt 1/5", "PySide6:"):
            self.assertIn(expected, report)
        self.window.copy_diagnostics()
        copied = QApplication.clipboard().text()
        # The report carries a "Generated" timestamp, so compare content only.
        self.assertTrue(copied.startswith("Meshtastic Sniffer TCP diagnostics"))
        self.assertIn("Session ended (lost)", copied)


class LoggingTests(unittest.TestCase):
    def test_configure_logging_writes_and_rotates(self):
        root = logging.getLogger()
        before = list(root.handlers)
        level = root.level
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = configure_logging(Path(tmp) / "logs", max_bytes=2000, backups=2)
                self.assertIsNotNone(path)
                for index in range(200):
                    logging.getLogger("sniffer").info("event %d", index)
                for handler in root.handlers:
                    handler.flush()
                self.assertIn("event 199", path.read_text(encoding="utf-8"))
                self.assertTrue((path.parent / "app.log.1").exists())
                self.assertFalse((path.parent / "app.log.3").exists())
            finally:
                for handler in root.handlers[:]:
                    if handler not in before:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(level)

    def test_configure_logging_tolerates_unwritable_dir(self):
        with tempfile.NamedTemporaryFile() as blocker:
            self.assertIsNone(configure_logging(Path(blocker.name) / "logs"))

    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0:00:00")
        self.assertEqual(format_duration(3725.9), "1:02:05")


if __name__ == "__main__":
    unittest.main()
