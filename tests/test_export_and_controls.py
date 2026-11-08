"""CSV export safety and window control-state tests (offscreen)."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app import CSV_FIELDS, MainWindow, csv_safe, default_export_name, write_packets_csv
from test_connection_lifecycle import FakeBus, LOST_TOPIC, Radio, pump_until


def row(node="!a", kind="POSITION_APP", **extra):
    data = {"date": "2026-11-07", "time": "12:00:00", "from": node, "type": kind, "rssi": "-80", "snr": "5.5"}
    data.update(extra)
    return data


class CsvHelperTests(unittest.TestCase):
    def test_formula_like_text_is_neutralised(self):
        for value in ("=HYPERLINK(\"x\")", "+cmd", "-cmd|' /C calc'!A0", "@SUM(A1)", "\t=1"):
            self.assertTrue(csv_safe(value).startswith("'"), value)

    def test_numbers_and_plain_text_are_unchanged(self):
        for value in ("-80", "-5.5", "+3", "0", "!a1b2c3d4", "TEXT_MESSAGE_APP", ""):
            self.assertEqual(csv_safe(value), value)
        self.assertEqual(csv_safe(None), "")

    def test_written_file_has_date_column_and_safe_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            count = write_packets_csv(path, [row(), row(node="=evil()", rssi="-101")])
            self.assertEqual(count, 2)
            with open(path, newline="", encoding="utf-8") as file:
                records = list(csv.reader(file))
            self.assertEqual(records[0], CSV_FIELDS)
            self.assertEqual(records[1], ["2026-11-07", "12:00:00", "!a", "POSITION_APP", "-80", "5.5"])
            self.assertEqual(records[2][2], "'=evil()")
            self.assertEqual(records[2][4], "-101")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["out.csv"])

    def test_failed_write_keeps_the_previous_file_and_leaves_no_partial(self):
        class Unprintable:
            def __str__(self):
                raise ValueError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            path.write_text("previous export\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_packets_csv(path, [row(), row(node=Unprintable())])
            self.assertEqual(path.read_text(encoding="utf-8"), "previous export\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["out.csv"])

    def test_missing_folder_raises_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                write_packets_csv(Path(tmp) / "missing" / "out.csv", [row()])

    def test_default_name_is_timestamped(self):
        self.assertEqual(
            default_export_name("meshtastic-packets", datetime(2026, 11, 7, 9, 5)),
            "meshtastic-packets-20261107-0905.csv",
        )


class WindowExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()
        self.tmp = tempfile.TemporaryDirectory()
        self.asked = []

    def tearDown(self):
        self.window.close()
        self.tmp.cleanup()

    def answer(self, path):
        def ask(title, default):
            self.asked.append(default)
            return str(path) if path else ""
        self.window.ask_save_path = ask

    def test_export_writes_all_rows_and_remembers_the_folder(self):
        for node in ("!a", "!b", "!c"):
            self.window.add_packet(row(node=node))
        target = Path(self.tmp.name) / "log"
        self.answer(target)
        written = self.window.export_csv()
        self.assertEqual(written, target.with_suffix(".csv"))
        self.assertEqual(len(written.read_text(encoding="utf-8").splitlines()), 4)
        self.assertIn("Exported 3 packets", self.window.statusBar().currentMessage())
        self.assertIn("Disconnected", self.window.status.text())
        self.answer(None)
        self.window.export_csv()
        self.assertEqual(Path(self.asked[-1]).parent, Path(self.tmp.name))
        self.assertRegex(Path(self.asked[-1]).name, r"^meshtastic-packets-\d{8}-\d{4}\.csv$")

    def test_filtered_export_only_writes_visible_rows(self):
        self.window.add_packet(row(node="!a", kind="TEXT_MESSAGE_APP"))
        self.window.add_packet(row(node="!b", kind="POSITION_APP"))
        self.window.filter_input.setText("position")
        self.answer(Path(self.tmp.name) / "filtered.csv")
        written = self.window.export_filtered_csv()
        lines = written.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("!b", lines[1])

    def test_nothing_to_export_skips_the_dialog(self):
        self.answer(Path(self.tmp.name) / "never.csv")
        self.assertIsNone(self.window.export_csv())
        self.assertEqual(self.asked, [])
        self.assertIn("No packets", self.window.statusBar().currentMessage())

    def test_cancelled_dialog_writes_nothing(self):
        self.window.add_packet(row())
        self.answer(None)
        self.assertIsNone(self.window.export_csv())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_unwritable_target_reports_and_keeps_running(self):
        from unittest.mock import patch
        self.window.add_packet(row())
        self.answer(Path(self.tmp.name) / "missing" / "out.csv")
        with patch("app.QMessageBox.critical") as critical:
            self.assertIsNone(self.window.export_csv())
        critical.assert_called_once()


class ControlStateTests(unittest.TestCase):
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

    def test_buttons_follow_the_connection_lifecycle(self):
        w = self.window
        self.assertTrue(w.connect_btn.isEnabled())
        self.assertFalse(w.disconnect_btn.isEnabled())
        self.assertEqual(w.status.property("state"), "offline")
        w.connect_radio()
        self.assertFalse(w.connect_btn.isEnabled())
        self.assertFalse(w.host.isEnabled())
        self.assertFalse(w.demo_btn.isEnabled())
        self.assertTrue(w.disconnect_btn.isEnabled())
        self.assertTrue(pump_until(self.app, lambda: w.status.property("state") == "connected"))
        radio = w.radio
        self.bus.sendMessage(LOST_TOPIC, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: w.radio is None))
        # Waiting to retry: still "busy", so Connect stays off and Disconnect cancels.
        self.assertEqual(w.status.property("state"), "warning")
        self.assertFalse(w.connect_btn.isEnabled())
        self.assertTrue(w.disconnect_btn.isEnabled())
        w.disconnect_radio()
        self.assertTrue(w.connect_btn.isEnabled())
        self.assertFalse(w.disconnect_btn.isEnabled())
        self.assertEqual(w.status.property("state"), "offline")

    def test_demo_mode_toggles_button_text_and_connect(self):
        w = self.window
        w.toggle_demo()
        self.assertEqual(w.demo_btn.text(), "Stop Demo")
        self.assertFalse(w.connect_btn.isEnabled())
        self.assertEqual(w.status.property("state"), "demo")
        w.toggle_demo()
        self.assertEqual(w.demo_btn.text(), "Demo Mode")
        self.assertTrue(w.connect_btn.isEnabled())

    def test_log_buttons_need_rows_and_filter(self):
        w = self.window
        self.assertFalse(w.export_btn.isEnabled())
        self.assertFalse(w.clear_btn.isEnabled())
        w.add_packet(row())
        self.assertTrue(w.export_btn.isEnabled())
        self.assertTrue(w.clear_btn.isEnabled())
        self.assertFalse(w.export_filtered_btn.isEnabled())
        w.filter_input.setText("!a")
        self.assertTrue(w.export_filtered_btn.isEnabled())
        self.assertTrue(w.export_filtered_action.isEnabled())

    def test_filter_shows_visible_count_through_rollover(self):
        w = self.window
        w.max_packets = 4
        w.filter_input.setText("position")
        for kind in ("POSITION_APP", "TEXT_MESSAGE_APP", "POSITION_APP", "TEXT_MESSAGE_APP", "POSITION_APP", "POSITION_APP"):
            w.add_packet(row(kind=kind))
        visible = sum(not w.table.isRowHidden(i) for i in range(w.table.rowCount()))
        self.assertEqual(w.visible_count, visible)
        self.assertIn(f"Showing {visible} of 4", w.stats.text())
        w.filter_input.clear()
        self.assertNotIn("Showing", w.stats.text())

    def test_clear_log_asks_first_and_respects_cancel(self):
        w = self.window
        w.add_packet(row())
        questions = []
        w.confirm = lambda title, text: questions.append(text) or False
        self.assertFalse(w.confirm_clear_log())
        self.assertEqual(len(w.rows), 1)
        self.assertIn("1 packets", questions[0])
        w.confirm = lambda title, text: True
        self.assertTrue(w.confirm_clear_log())
        self.assertEqual(w.rows, [])

    def test_transient_messages_do_not_replace_connection_state(self):
        w = self.window
        w.add_packet(row())
        w.node_table.selectRow(0)
        w.copy_selected_node()
        self.assertIn("Disconnected", w.status.text())
        self.assertIn("Copied node ID", w.statusBar().currentMessage())

    def test_pause_menu_item_tracks_button(self):
        w = self.window
        w.pause_action.trigger()
        self.assertTrue(w.display_paused)
        self.assertTrue(w.pause_action.isChecked())
        self.assertEqual(w.pause_btn.text(), "Resume Auto-scroll")
        w.toggle_pause()
        self.assertFalse(w.pause_action.isChecked())

    def test_packet_details_include_the_date(self):
        w = self.window
        w.add_packet(row())
        w.table.selectRow(0)
        self.assertEqual(w.packet_details.toPlainText().splitlines()[0], "date: 2026-11-07")


if __name__ == "__main__":
    unittest.main()
