"""Integrations settings, dialog and window wiring (offscreen, no radio)."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import gc
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

import app
import integrations_ui as iu
import mesh_integrations as mi

TEXT = {"from": 0x1234ABCD, "to": 0xFFFFFFFF, "rxTime": 1700000000,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "private words"}}


def scratch_settings():
    return QSettings(str(Path(tempfile.mkdtemp()) / "s.ini"), QSettings.Format.IniFormat)



class PausedGc:
    """Keep Python's cyclic GC off while integration worker threads run.

    The GC can run on whichever thread allocates; on a worker thread it would
    free Qt objects left by earlier tests off the GUI thread and crash PySide.
    """

    def setUp(self):
        self._gc_was_enabled = gc.isenabled()
        gc.disable()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        if self._gc_was_enabled:
            gc.enable()

class FakeRadio(QObject):
    packet = Signal(dict)
    status = Signal(str)
    connected = Signal(str)
    finished = Signal()

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.packet_observer = None

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, _ms):
        return True


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off(self):
        s = iu.load_integration_settings(scratch_settings())
        self.assertFalse(s.any_enabled)
        self.assertEqual(s.local_api_port, mi.DEFAULT_PORT)
        self.assertEqual(s.webhook_events, set(mi.EVENT_TYPES))

    def test_sending_and_text_can_never_be_enabled(self):
        settings = scratch_settings()
        iu.save_integration_settings(settings, {"assistant_send_enabled": True, "webhook_include_text": True})
        s = iu.load_integration_settings(settings)
        self.assertFalse(s.assistant_send_enabled)
        self.assertFalse(s.webhook_include_text)
        self.assertFalse(iu.make_hub(settings).message_text)

    def test_round_trip_and_no_events(self):
        settings = scratch_settings()
        iu.save_integration_settings(settings, {"webhooks_enabled": True, "webhook_urls": "https://a.example/h",
                                                "webhook_events": "none", "local_api_port": "9600"})
        s = iu.load_integration_settings(settings)
        self.assertTrue(s.webhooks_enabled)
        self.assertEqual(s.webhook_urls, ["https://a.example/h"])
        self.assertEqual(s.webhook_events, set())
        self.assertEqual(s.local_api_port, 9600)

    def test_folder_override(self):
        self.assertEqual(str(iu.integrations_dir()), app.os.environ[iu.INTEGRATIONS_DIR_ENV])


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_saves_values(self):
        settings = scratch_settings()
        dialog = iu.IntegrationsDialog(settings)
        dialog.webhooks_enabled.setChecked(True)
        dialog.webhook_urls.setPlainText("https://a.example/h\n\nhttp://127.0.0.1:1880/x")
        dialog.event_boxes["message"].setChecked(False)
        dialog.local_api_enabled.setChecked(True)
        dialog.save()
        s = iu.load_integration_settings(settings)
        self.assertEqual(s.webhook_urls, ["https://a.example/h", "http://127.0.0.1:1880/x"])
        self.assertEqual(s.webhook_events, {"node", "telemetry"})
        self.assertTrue(s.local_api_enabled)

    def test_rejects_bad_url(self):
        settings = scratch_settings()
        dialog = iu.IntegrationsDialog(settings)
        dialog.webhook_urls.setPlainText("ftp://nope")
        dialog.save()
        self.assertIn("Not a web address", dialog.problem.text())
        self.assertFalse(iu.load_integration_settings(settings).webhook_urls)


class WindowWiringTests(PausedGc, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.window = app.MainWindow(radio_factory=FakeRadio)
        self.window.settings = scratch_settings()

    def tearDown(self):
        self.window.integrations.stop()
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        self.window = None
        super().tearDown()

    def test_menu_has_integrations(self):
        labels = [a.text() for m in self.window.menuBar().actions() for a in m.menu().actions()]
        self.assertIn("Integrations…", labels)

    def test_radio_packets_feed_hub_without_text(self):
        self.window.host.setText("10.0.0.5")
        self.window.connect_radio()
        self.assertEqual(self.window.radio.packet_observer, self.window.integrations.observe)
        self.window.radio.packet_observer(TEXT)
        message = self.window.integrations.state.recent_messages()[0]
        self.assertNotIn("text", message)
        self.window.radio.connected.emit("10.0.0.5")
        self.assertTrue(self.window.integrations.state.status()["radio"]["connected"])

    def test_real_radio_thread_calls_observer(self):
        seen = []
        radio = app.RadioThread("10.0.0.5")
        radio.packet_observer = seen.append
        radio.on_packet(TEXT)
        self.assertEqual(seen, [TEXT])

    def test_restart_turns_on_local_link(self):
        iu.save_integration_settings(self.window.settings, {"local_api_enabled": True, "local_api_port": "0"})
        # port 0 is refused by validation; pick a free port instead
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        iu.save_integration_settings(self.window.settings, {"local_api_port": str(port)})
        self.assertEqual(self.window.start_integrations(), "")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertIn("/metrics", self.window.integrations_status())


if __name__ == "__main__":
    unittest.main()
