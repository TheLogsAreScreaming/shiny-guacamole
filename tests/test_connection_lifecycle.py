"""Regression tests for dropped-connection detection and reconnect lifecycle.

These use a fake pubsub bus and a fake Meshtastic interface, so they run in CI
without the meshtastic package or a physical radio. They prove the app-side
lifecycle only; network loss on real hardware still needs a hands-on test.
"""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import os
import socket
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from app import (
    LOST_TOPIC,
    RECEIVE_TOPIC,
    MainWindow,
    RadioThread,
    enable_tcp_keepalive,
    reconnect_delay_ms,
)


class FakeBus:
    """Minimal stand-in for pypubsub's ``pub`` module."""

    def __init__(self):
        self.listeners = {}
        self.lock = threading.Lock()

    def subscribe(self, listener, topic):
        with self.lock:
            self.listeners.setdefault(topic, []).append(listener)

    def unsubscribe(self, listener, topic):
        with self.lock:
            self.listeners.get(topic, []).remove(listener)

    def count(self, topic):
        with self.lock:
            return len(self.listeners.get(topic, []))

    def sendMessage(self, topic, **kwargs):
        with self.lock:
            listeners = list(self.listeners.get(topic, []))
        for listener in listeners:
            listener(**kwargs)


class FakeInterface:
    def __init__(self):
        self.closed = False
        self.socket = None
        self._release_reader = threading.Event()
        self._rxThread = threading.Thread(target=self._release_reader.wait, daemon=True)
        self._rxThread.start()

    def kill_reader(self):
        self._release_reader.set()
        self._rxThread.join(2)

    def close(self):
        self.closed = True
        self._release_reader.set()


class Radio:
    """Factory that records every interface and can be told to fail."""

    def __init__(self, bus):
        self.bus = bus
        self.interfaces = []
        self.fail_next = 0

    def make_interface(self, host):
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionRefusedError("radio unreachable")
        interface = FakeInterface()
        self.interfaces.append(interface)
        return interface

    def thread(self, host, parent=None):
        return RadioThread(host, parent, interface_factory=self.make_interface, pubsub=self.bus, poll_interval=0.01)


def pump_until(app, condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    app.processEvents()
    return condition()


class RadioThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.bus = FakeBus()
        self.radio = Radio(self.bus)

    def start(self):
        thread = self.radio.thread("192.0.2.10")
        thread.start()
        self.assertTrue(pump_until(self.app, lambda: thread._interface is not None))
        return thread

    def assert_cleaned_up(self, thread):
        self.assertTrue(thread.wait(3000))
        self.assertEqual(self.bus.count(RECEIVE_TOPIC), 0)
        self.assertEqual(self.bus.count(LOST_TOPIC), 0)
        self.assertTrue(self.radio.interfaces[-1].closed)

    def test_connection_lost_event_ends_session_as_lost(self):
        thread = self.start()
        self.bus.sendMessage(LOST_TOPIC, interface=thread._interface)
        self.assert_cleaned_up(thread)
        self.assertEqual(thread.end_reason, "lost")
        self.assertIn("closed", thread.end_detail)

    def test_lost_event_for_another_interface_is_ignored(self):
        thread = self.start()
        self.bus.sendMessage(LOST_TOPIC, interface=object())
        time.sleep(0.1)
        self.assertTrue(thread.isRunning())
        thread.stop()
        self.assert_cleaned_up(thread)
        self.assertEqual(thread.end_reason, "stopped")

    def test_dead_reader_thread_is_detected_without_an_event(self):
        thread = self.start()
        self.radio.interfaces[-1].kill_reader()
        self.assert_cleaned_up(thread)
        self.assertEqual(thread.end_reason, "lost")
        self.assertIn("stopped responding", thread.end_detail)

    def test_user_stop_wins_over_loss(self):
        thread = self.start()
        thread.stop()
        self.bus.sendMessage(LOST_TOPIC, interface=thread._interface)
        self.assert_cleaned_up(thread)
        self.assertEqual(thread.end_reason, "stopped")

    def test_initial_failure_unsubscribes(self):
        self.radio.fail_next = 1
        thread = self.radio.thread("192.0.2.10")
        thread.start()
        self.assertTrue(thread.wait(3000))
        self.assertEqual(thread.end_reason, "failed")
        self.assertIn("unreachable", thread.end_detail)
        self.assertEqual(self.bus.count(RECEIVE_TOPIC), 0)
        self.assertEqual(self.bus.count(LOST_TOPIC), 0)

    def test_packets_from_a_stale_interface_are_dropped(self):
        thread = self.start()
        received = []
        thread.packet.connect(received.append)
        self.bus.sendMessage(RECEIVE_TOPIC, packet={"fromId": "!old"}, interface=object())
        self.bus.sendMessage(RECEIVE_TOPIC, packet={"fromId": "!new"}, interface=thread._interface)
        self.assertTrue(pump_until(self.app, lambda: len(received) >= 1))
        thread.stop()
        self.assert_cleaned_up(thread)
        self.app.processEvents()
        self.assertEqual([row["from"] for row in received], ["!new"])


class ReconnectLifecycleTests(unittest.TestCase):
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

    def connect_and_wait(self):
        self.window.connect_radio()
        radio = self.window.radio
        self.assertTrue(pump_until(self.app, lambda: radio._interface is not None))
        self.app.processEvents()
        return radio

    def drop(self, radio):
        self.bus.sendMessage(LOST_TOPIC, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))

    def test_dropped_session_schedules_a_reconnect(self):
        self.drop(self.connect_and_wait())
        self.assertTrue(self.window.retry_timer.isActive())
        self.assertEqual(self.window.retry_count, 1)
        self.assertIn("Connection lost", self.window.status.text())
        self.assertIn("attempt 1/5", self.window.status.text())

    def test_reconnect_restores_a_single_subscription_and_resets_backoff(self):
        self.drop(self.connect_and_wait())
        self.window.retry_timer.stop()
        self.window._retry_connect()
        radio = self.window.radio
        self.assertIsNotNone(radio)
        self.assertTrue(pump_until(self.app, lambda: radio._interface is not None))
        self.assertTrue(pump_until(self.app, lambda: self.window.retry_count == 0))
        self.assertEqual(self.bus.count(RECEIVE_TOPIC), 1)
        self.assertEqual(self.bus.count(LOST_TOPIC), 1)
        self.assertEqual(len(self.radio.interfaces), 2)
        self.assertTrue(self.radio.interfaces[0].closed)

    def test_packet_log_survives_a_reconnect(self):
        radio = self.connect_and_wait()
        self.bus.sendMessage(RECEIVE_TOPIC, packet={"fromId": "!a1"}, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: len(self.window.rows) == 1))
        self.drop(radio)
        self.window.retry_timer.stop()
        self.window._retry_connect()
        radio = self.window.radio
        self.assertTrue(pump_until(self.app, lambda: radio._interface is not None))
        self.bus.sendMessage(RECEIVE_TOPIC, packet={"fromId": "!a1"}, interface=radio._interface)
        self.assertTrue(pump_until(self.app, lambda: len(self.window.rows) == 2))

    def test_gives_up_after_max_attempts(self):
        self.radio.fail_next = 99
        self.window.connect_radio()
        for attempt in range(1, self.window.max_retries + 1):
            self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
            self.assertEqual(self.window.retry_count, attempt)
            self.assertTrue(self.window.retry_timer.isActive())
            self.window.retry_timer.stop()
            self.window._retry_connect()
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        self.assertFalse(self.window.retry_timer.isActive())
        self.assertFalse(self.window.reconnect_enabled)
        self.assertIn("gave up", self.window.status.text())
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_manual_connect_resets_the_attempt_counter(self):
        self.radio.fail_next = 1
        self.window.connect_radio()
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        self.assertEqual(self.window.retry_count, 1)
        self.window.connect_radio()
        self.assertEqual(self.window.retry_count, 0)

    def test_disconnect_cancels_a_pending_reconnect(self):
        self.drop(self.connect_and_wait())
        self.assertTrue(self.window.retry_timer.isActive())
        self.window.disconnect_radio()
        self.assertFalse(self.window.retry_timer.isActive())
        self.window._retry_connect()
        self.assertIsNone(self.window.radio)
        self.assertIn("Disconnected", self.window.status.text())

    def test_user_disconnect_does_not_reconnect(self):
        self.connect_and_wait()
        self.window.disconnect_radio()
        self.assertTrue(pump_until(self.app, lambda: self.window.radio is None))
        self.assertFalse(self.window.retry_timer.isActive())
        self.assertIn("Disconnected", self.window.status.text())

    def test_reconnect_uses_the_original_host(self):
        self.drop(self.connect_and_wait())
        self.window.host.setText("192.0.2.99")
        self.window.retry_timer.stop()
        self.window._retry_connect()
        self.assertEqual(self.window.radio.host, "192.0.2.10")


class HelperTests(unittest.TestCase):
    def test_backoff_is_bounded(self):
        self.assertEqual([reconnect_delay_ms(n) for n in range(1, 7)], [2000, 4000, 8000, 16000, 30000, 30000])

    def test_keepalive_is_enabled_on_tcp_sockets(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.assertTrue(enable_tcp_keepalive(sock))
            self.assertTrue(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        finally:
            sock.close()

    def test_keepalive_tolerates_unsupported_objects(self):
        self.assertFalse(enable_tcp_keepalive(object()))


if __name__ == "__main__":
    unittest.main()
