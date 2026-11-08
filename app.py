import csv
import logging
import platform
from collections import deque
from logging.handlers import RotatingFileHandler
import random
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QTimer, QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QSplitter, QTextEdit,
)


def packet_row(packet):
    decoded = packet.get("decoded") or {}
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "from": str(packet.get("fromId") or packet.get("from") or ""),
        "type": str(decoded.get("portnum") or ""),
        "rssi": str(packet.get("rxRssi") if packet.get("rxRssi") is not None else ""),
        "snr": str(packet.get("rxSnr") if packet.get("rxSnr") is not None else ""),
    }


APP_NAME = "Meshtastic Sniffer TCP"
log = logging.getLogger("sniffer")


def default_log_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    return Path.home() / ".meshtastic-sniffer-tcp" / "logs"


def configure_logging(log_dir=None, max_bytes=512 * 1024, backups=3):
    """Write app and Meshtastic library warnings to a small rotating log file.

    Returns the log file path, or None if the directory cannot be created (the
    app keeps working without a file log).
    """
    log_dir = Path(log_dir) if log_dir else default_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "app.log"
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # The library logs every reconnect and reader error; keep warnings and up.
    logging.getLogger("meshtastic").setLevel(logging.WARNING)
    return path


def package_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "not installed"


def format_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


RECEIVE_TOPIC = "meshtastic.receive"
LOST_TOPIC = "meshtastic.connection.lost"
MAX_RECONNECT_ATTEMPTS = 5


def reconnect_delay_ms(attempt):
    """Bounded exponential backoff: 2s, 4s, 8s, 16s, then capped at 30s."""
    return min(30000, 2000 * (2 ** (max(1, attempt) - 1)))


def enable_tcp_keepalive(sock, idle=15, interval=5, count=4):
    """Ask the OS to probe an idle TCP link so a vanished radio is noticed.

    Without keepalive a radio that loses power or Wi-Fi leaves recv() blocked
    indefinitely. With these values a dead link errors out after roughly
    idle + interval * count seconds (about 35s). Returns False when the socket
    or platform does not support the options.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle_option = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle_option, idle)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
        return True
    except (OSError, AttributeError, TypeError):
        return False


def _default_interface_factory(host):
    from meshtastic.tcp_interface import TCPInterface
    return TCPInterface(hostname=host)


class RadioThread(QThread):
    """Owns one TCP session from connect to close.

    When run() returns, ``end_reason`` is one of ``"stopped"`` (user asked),
    ``"failed"`` (never connected) or ``"lost"`` (connected, then dropped),
    and ``end_detail`` explains why. Subscriptions are always removed and the
    interface closed before the thread finishes, so a reconnect never stacks
    duplicate packet callbacks.
    """

    packet = Signal(dict)
    status = Signal(str)
    connected = Signal(str)

    def __init__(self, host, parent=None, interface_factory=None, pubsub=None, poll_interval=0.25):
        super().__init__(parent)
        self.host = host
        self._interface_factory = interface_factory or _default_interface_factory
        self._pubsub = pubsub
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._interface = None
        self.end_reason = "stopped"
        self.end_detail = ""

    def stop(self):
        self._stop.set()

    def on_packet(self, packet, interface=None):
        if self._stop.is_set():
            return
        if self._interface is not None and interface is not None and interface is not self._interface:
            return
        self.packet.emit(packet_row(packet))

    def on_connection_lost(self, interface=None):
        # Only trust loss events for the interface this thread owns; a late
        # event from a previous session must not tear down a new one.
        if self._interface is not None and interface is self._interface:
            self._lost.set()

    def link_problem(self, interface):
        """Return why the established link is dead, or None while it is healthy."""
        if self._lost.is_set():
            return "radio closed the connection"
        reader = getattr(interface, "_rxThread", None)
        if reader is not None and getattr(reader, "ident", None) is not None and not reader.is_alive():
            return "radio stopped responding"
        return None

    def run(self):
        interface = None
        subscribed = []
        reason, detail = "stopped", ""
        try:
            pub = self._pubsub
            if pub is None:
                from pubsub import pub
            if self._stop.is_set():
                return
            for topic, listener in ((RECEIVE_TOPIC, self.on_packet), (LOST_TOPIC, self.on_connection_lost)):
                pub.subscribe(listener, topic)
                subscribed.append((topic, listener))
            interface = self._interface_factory(self.host)
            self._interface = interface
            if self._stop.is_set():
                return
            self.connected.emit(self.host)
            self.status.emit("● Connected to " + self.host)
            keepalive_socket = None
            while not self._stop.wait(self._poll_interval):
                sock = getattr(interface, "socket", None)
                if sock is not None and sock is not keepalive_socket:
                    # The library may silently swap sockets when it reconnects
                    # internally, so re-arm keepalive on every new socket.
                    enable_tcp_keepalive(sock)
                    keepalive_socket = sock
                problem = self.link_problem(interface)
                if problem:
                    reason, detail = "lost", problem
                    break
        except Exception as exc:
            reason, detail = "failed", str(exc) or exc.__class__.__name__
        finally:
            for topic, listener in subscribed:
                try:
                    pub.unsubscribe(listener, topic)
                except Exception:
                    pass
            if interface is not None:
                try:
                    interface.close()
                except Exception:
                    pass
            if self._stop.is_set():
                reason, detail = "stopped", ""
            self.end_reason, self.end_detail = reason, detail


class MainWindow(QMainWindow):
    def __init__(self, radio_factory=None):
        super().__init__()
        self.radio_factory = radio_factory or RadioThread
        self.setWindowTitle("Meshtastic Sniffer TCP — Intel Mac")
        self.resize(900, 520)
        self.settings = QSettings('BaconDisk22', 'MeshtasticSnifferTCP')
        self.max_packets = 5000
        self.display_paused = False
        self.rows = []
        self.seen_nodes = set()
        self.port_counts = {}
        self.node_counts = {}
        self.node_refresh_pending = False
        self.node_refresh_timer = QTimer(self)
        self.node_refresh_timer.setSingleShot(True)
        self.node_refresh_timer.setInterval(300)
        self.node_refresh_timer.timeout.connect(self._node_refresh_tick)
        self.pending_packets = []
        self.flush_timer = QTimer(self)
        self.flush_timer.setSingleShot(True)
        self.flush_timer.setInterval(200)
        self.flush_timer.timeout.connect(self.flush_packets)
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setSingleShot(True)
        self.scroll_timer.setInterval(150)
        self.scroll_timer.timeout.connect(self._scroll_to_latest)
        self.last_packet_at = None
        self.session_packets = 0
        self.session_started = None
        self.has_connected = False
        self.reconnects = 0
        self.last_drop = ""
        self.connection_events = deque(maxlen=50)
        self.log_path = None
        self.diagnostics_timer = QTimer(self)
        self.diagnostics_timer.setInterval(1000)
        self.diagnostics_timer.timeout.connect(self.refresh_diagnostics)
        self.diagnostics_timer.start()
        self.radio = None
        self.retry_count = 0
        self.max_retries = MAX_RECONNECT_ATTEMPTS
        self.reconnect_host = ""
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry_connect)
        self.reconnect_enabled = False
        self.demo_nodes = ["!a1b2c3d4", "!b2c3d4e5", "!c3d4e5f6", "!d4e5f6a7"]
        self.demo = QTimer(self)
        self.demo.timeout.connect(self.demo_packet)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        controls = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("Radio IP address (e.g. 192.168.1.50)")
        self.host.setText(self.settings.value("radio_host", "", type=str))
        self.connect_btn = QPushButton("Connect TCP")
        self.connect_btn.clicked.connect(self.connect_radio)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_radio)
        self.demo_btn = QPushButton("Demo Mode")
        self.demo_btn.clicked.connect(self.toggle_demo)
        self.pause_btn = QPushButton("Pause Display")
        self.pause_btn.clicked.connect(self.toggle_pause)
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.clear_log)
        visible_export_btn = QPushButton("Export Filtered CSV")
        visible_export_btn.clicked.connect(self.export_filtered_csv)
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self.export_csv)
        for item in (self.host, self.connect_btn, self.disconnect_btn, self.demo_btn, self.pause_btn, clear_btn, export_btn, visible_export_btn):
            controls.addWidget(item)
        layout.addLayout(controls)
        self.status = QLabel("● Disconnected")
        layout.addWidget(self.status)
        self.stats = QLabel("Packets: 0  |  Nodes: 0  |  Message types: 0")
        layout.addWidget(self.stats)
        self.diagnostics = QLabel()
        self.diagnostics.setObjectName("diagnostics")
        self.diagnostics.setWordWrap(True)
        layout.addWidget(self.diagnostics)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter packets by node ID or message type...")
        self.filter_input.textChanged.connect(self.apply_filter)
        layout.addWidget(self.filter_input)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "From", "Port", "RSSI", "SNR"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.packet_details = QTextEdit()
        self.packet_details.setReadOnly(True)
        self.packet_details.setPlaceholderText("Select a packet to inspect its available metadata")
        self.packet_details.setMaximumHeight(110)
        self.table.itemSelectionChanged.connect(self.show_packet_details)
        self.node_table = QTableWidget(0, 2)
        self.node_table.setHorizontalHeaderLabels(["Node ID", "Packets"])
        self.node_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.node_table.horizontalHeader().setStretchLastSection(True)
        self.node_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.copy_node_btn = QPushButton("Copy Selected Node ID")
        self.copy_node_btn.clicked.connect(self.copy_selected_node)
        self.node_table.cellDoubleClicked.connect(self.filter_by_node)
        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(self.node_table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        layout.addWidget(self.packet_details)
        bottom = QHBoxLayout()
        self.copy_diagnostics_btn = QPushButton("Copy Diagnostics")
        self.copy_diagnostics_btn.clicked.connect(self.copy_diagnostics)
        self.open_logs_btn = QPushButton("Open Log Folder")
        self.open_logs_btn.clicked.connect(self.open_log_folder)
        for item in (self.copy_node_btn, self.copy_diagnostics_btn, self.open_logs_btn):
            bottom.addWidget(item)
        layout.addLayout(bottom)
        self.refresh_diagnostics()

    def _count_packet(self, data, delta):
        """Keep node and port tallies in step with self.rows without rescanning it."""
        node = data.get("from")
        if node:
            count = self.node_counts.get(node, 0) + delta
            if count > 0:
                self.node_counts[node] = count
                self.seen_nodes.add(node)
            else:
                self.node_counts.pop(node, None)
                self.seen_nodes.discard(node)
        kind = data.get("type") or "UNKNOWN"
        count = self.port_counts.get(kind, 0) + delta
        if count > 0:
            self.port_counts[kind] = count
        else:
            self.port_counts.pop(kind, None)

    def _update_stats(self):
        self.stats.setText(f"Packets: {len(self.rows)}  |  Nodes: {len(self.seen_nodes)}  |  Message types: {len(self.port_counts)}")

    def _row_matches_filter(self, packet, query):
        if not query:
            return True
        searchable = " ".join(str(packet.get(key, "")) for key in ("from", "type")).casefold()
        return query in searchable

    def add_packet(self, data):
        # Every step here is O(1) in the size of the log (the node table is
        # refreshed on a short timer), so a full 5,000-row history costs the
        # same per packet as an empty one.
        self.rows.append(data)
        self._count_packet(data, +1)
        self.last_packet_at = time.monotonic()
        if len(self.rows) > self.max_packets:
            self._count_packet(self.rows.pop(0), -1)
            self.table.removeRow(0)
            self.packet_details.clear()
        self._update_stats()
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, key in enumerate(("time", "from", "type", "rssi", "snr")):
            self.table.setItem(row, col, QTableWidgetItem(data.get(key, "")))
        query = self.filter_input.text().strip().casefold()
        if query:
            self.table.setRowHidden(row, not self._row_matches_filter(data, query))
        self.schedule_node_refresh()
        if self.table.currentRow() >= 0:
            self.show_packet_details()
        if not self.display_paused and not self.scroll_timer.isActive():
            # scrollToBottom forces a layout of the whole table; doing it on
            # every packet made a full history cost ~100 ms per packet.
            self.scroll_timer.start()

    def queue_packet(self, data):
        """Buffer a live packet; the UI applies buffered packets every 200 ms.

        A full window redraw costs tens of milliseconds on macOS, so redrawing
        once per packet capped throughput at ~25 packets/s and froze the UI.
        """
        self.pending_packets.append(data)
        self.session_packets += 1
        self.last_packet_at = time.monotonic()
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def flush_packets(self):
        batch, self.pending_packets = self.pending_packets, []
        if not batch:
            return
        self.table.setUpdatesEnabled(False)
        try:
            for data in batch:
                self.add_packet(data)
        finally:
            self.table.setUpdatesEnabled(True)

    def _scroll_to_latest(self):
        if not self.display_paused:
            self.table.scrollToBottom()

    def schedule_node_refresh(self):
        """Refresh the node table now, then at most every 300 ms while packets stream in."""
        if self.node_refresh_timer.isActive():
            self.node_refresh_pending = True
        else:
            self.refresh_nodes()
            self.node_refresh_timer.start()

    def _node_refresh_tick(self):
        if self.node_refresh_pending:
            self.node_refresh_pending = False
            self.refresh_nodes()
            self.node_refresh_timer.start()

    def on_radio_status(self, message):
        self.status.setText(message)

    def record_event(self, message, level=logging.INFO):
        """Remember a connection event for Copy Diagnostics and write it to the log."""
        self.connection_events.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
        log.log(level, message)

    def _on_radio_connected(self, host):
        if self.has_connected:
            self.reconnects += 1
        self.has_connected = True
        self.retry_count = 0
        self.session_started = time.monotonic()
        self.session_packets = 0
        self.record_event("Connected to " + host)
        self.refresh_diagnostics()

    def _radio_finished(self):
        radio = self.sender()
        if radio is self.radio:
            self.radio = None
            self.connect_btn.setEnabled(True)
            reason = getattr(radio, "end_reason", "stopped")
            detail = getattr(radio, "end_detail", "")
            self.session_started = None
            if reason == "lost":
                self.last_drop = f"{datetime.now().strftime('%H:%M:%S')} {detail}"
            self.record_event(
                f"Session ended ({reason}){': ' + detail if detail else ''}",
                logging.WARNING if reason != "stopped" else logging.INFO,
            )
            if reason in ("failed", "lost") and self.reconnect_enabled:
                self._schedule_reconnect(reason, detail)
            elif reason == "failed":
                self.status.setText("✕ Connection failed: " + detail)
            elif not self.demo.isActive():
                self.status.setText("● Disconnected")
        radio.deleteLater()

    def _schedule_reconnect(self, reason, detail):
        # Called only once the previous worker has fully finished, so the old
        # interface is closed and its subscriptions are gone before retrying.
        label = "Connection lost" if reason == "lost" else "Connection failed"
        self.retry_count += 1
        if self.retry_count > self.max_retries:
            self.reconnect_enabled = False
            self.record_event(f"Gave up reconnecting after {self.max_retries} attempts", logging.WARNING)
            self.status.setText(
                f"✕ {label}: {detail} — gave up after {self.max_retries} attempts. "
                "Select Connect TCP to try again."
            )
            return
        delay = reconnect_delay_ms(self.retry_count)
        self.retry_timer.start(delay)
        self.record_event(f"Reconnect attempt {self.retry_count}/{self.max_retries} in {delay // 1000}s")
        self.status.setText(
            f"◌ {label}: {detail} — reconnecting in {delay // 1000}s "
            f"(attempt {self.retry_count}/{self.max_retries})"
        )

    def _retry_connect(self):
        if self.reconnect_enabled and self.radio is None and self.reconnect_host:
            self._start_radio(self.reconnect_host)
            self.status.setText(
                f"◌ Reconnecting to {self.reconnect_host} "
                f"(attempt {self.retry_count}/{self.max_retries})..."
            )

    def connect_radio(self):
        host = self.host.text().strip()
        if not host:
            QMessageBox.warning(self, "Missing address", "Enter your radio IP address.")
            return
        if self.radio is not None:
            self.status.setText("Disconnect the current radio before connecting again.")
            return
        self.retry_count = 0
        self.has_connected = False
        self.reconnects = 0
        self.last_drop = ""
        self._start_radio(host)

    def _start_radio(self, host):
        self.demo.stop()
        self.retry_timer.stop()
        self.reconnect_enabled = True
        self.reconnect_host = host
        self.settings.setValue("radio_host", host)
        self.radio = self.radio_factory(host, self)
        self.radio.packet.connect(self.queue_packet)
        self.radio.status.connect(self.on_radio_status)
        self.radio.connected.connect(self._on_radio_connected)
        self.radio.finished.connect(self._radio_finished)
        self.connect_btn.setEnabled(False)
        self.status.setText("◌ Connecting to radio...")
        self.record_event("Connecting to " + host)
        self.radio.start()

    def disconnect_radio(self):
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.retry_count = 0
        if self.radio is not None or self.reconnect_host:
            self.record_event("Disconnect requested by user")
        if self.radio is not None:
            self.radio.stop()
            self.status.setText("◌ Disconnecting...")
        elif not self.demo.isActive():
            self.status.setText("● Disconnected")

    def session_summary(self):
        """One-line health summary for the live connection (empty when idle)."""
        now = time.monotonic()
        parts = []
        if self.session_started is not None:
            parts.append("Session up " + format_duration(now - self.session_started))
            parts.append(f"{self.session_packets} packets this session")
            if self.last_packet_at is None or self.last_packet_at < self.session_started:
                parts.append("no packets yet")
            else:
                parts.append("last packet " + format_duration(now - self.last_packet_at) + " ago")
        elif self.retry_timer.isActive():
            parts.append("Waiting to reconnect")
        if self.reconnects:
            parts.append(f"reconnects: {self.reconnects}")
        if self.last_drop:
            parts.append("last drop: " + self.last_drop)
        return "  |  ".join(parts)

    def refresh_diagnostics(self):
        text = self.session_summary()
        self.diagnostics.setText(text)
        self.diagnostics.setVisible(bool(text))

    def diagnostics_report(self):
        lines = [
            f"{APP_NAME} diagnostics",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"macOS/platform: {platform.platform()} ({platform.machine()})",
            f"Python: {platform.python_version()}",
            f"PySide6: {package_version('PySide6')}  meshtastic: {package_version('meshtastic')}",
            f"Radio host: {self.reconnect_host or self.host.text().strip() or '(none)'}",
            f"Status: {self.status.text()}",
            f"Session: {self.session_summary() or 'not connected'}",
            f"Packets in log: {len(self.rows)} (limit {self.max_packets}), nodes: {len(self.seen_nodes)}",
            f"Log file: {self.log_path or 'not enabled'}",
            "",
            "Recent connection events:",
        ]
        lines += [f"  {stamp}  {message}" for stamp, message in self.connection_events] or ["  (none)"]
        return "\n".join(lines)

    def copy_diagnostics(self):
        QApplication.clipboard().setText(self.diagnostics_report())
        self.status.setText("Copied diagnostics to clipboard")

    def open_log_folder(self):
        if self.log_path is None:
            QMessageBox.information(self, "Logging unavailable", "The log file could not be created on this Mac.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.log_path).parent)))

    def toggle_demo(self):
        if self.demo.isActive():
            self.demo.stop()
            self.status.setText("Disconnected")
            return
        if self.radio is not None:
            self.status.setText("Disconnect the radio before starting demo mode.")
            return
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.demo.start(1200)
        self.status.setText("● Demo mode — simulated packets")

    def demo_packet(self):
        self.add_packet({
            "time": datetime.now().strftime("%H:%M:%S"),
            "from": random.choice(self.demo_nodes),
            "type": random.choice(["TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP"]),
            "rssi": str(random.randint(-120, -45)),
            "snr": str(round(random.uniform(-15, 12), 1)),
        })

    def show_packet_details(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.rows):
            packet = self.rows[index]
            self.packet_details.setPlainText("\n".join(f"{key}: {packet.get(key, '')}" for key in ("time", "from", "type", "rssi", "snr")))
        else:
            self.packet_details.clear()

    def filter_by_node(self, row, column):
        item = self.node_table.item(row, 0)
        if item is not None:
            self.filter_input.setText(item.text())

    def copy_selected_node(self):
        row = self.node_table.currentRow()
        item = self.node_table.item(row, 0) if row >= 0 else None
        if item is not None:
            QApplication.clipboard().setText(item.text())
            self.status.setText("Copied node ID: " + item.text())

    def refresh_nodes(self):
        self.node_refresh_pending = False
        ordered = sorted(self.node_counts.items(), key=lambda item: -item[1])
        self.node_table.setUpdatesEnabled(False)
        try:
            self.node_table.setRowCount(len(ordered))
            for row, (node, count) in enumerate(ordered):
                for col, text in ((0, node), (1, str(count))):
                    item = self.node_table.item(row, col)
                    if item is None:
                        self.node_table.setItem(row, col, QTableWidgetItem(text))
                    elif item.text() != text:
                        item.setText(text)
        finally:
            self.node_table.setUpdatesEnabled(True)

    def apply_filter(self):
        query = self.filter_input.text().strip().casefold()
        for index, packet in enumerate(self.rows):
            self.table.setRowHidden(index, not self._row_matches_filter(packet, query))

    def toggle_pause(self):
        self.display_paused = not self.display_paused
        self.pause_btn.setText("Resume Auto-scroll" if self.display_paused else "Pause Display")
        if not self.display_paused:
            self.table.scrollToBottom()

    def clear_log(self):
        self.rows.clear()
        self.seen_nodes.clear()
        self.port_counts.clear()
        self.node_counts.clear()
        self.node_refresh_pending = False
        self.pending_packets.clear()
        self.table.setRowCount(0)
        self.packet_details.clear()
        self.node_table.setRowCount(0)
        self.stats.setText("Packets: 0  |  Nodes: 0  |  Message types: 0")

    def export_filtered_csv(self):
        visible = [packet for index, packet in enumerate(self.rows) if not self.table.isRowHidden(index)]
        path, _ = QFileDialog.getSaveFileName(self, "Export filtered packet log", str(Path.home() / "meshtastic-filtered-packets.csv"), "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=["time", "from", "type", "rssi", "snr"])
                writer.writeheader()
                writer.writerows(visible)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
        else:
            self.status.setText(f"Exported {len(visible)} filtered packets")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export packet log",
            str(Path.home() / "meshtastic-packets.csv"), "CSV files (*.csv)"
        )
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8") as file:
                    writer = csv.DictWriter(file, fieldnames=["time", "from", "type", "rssi", "snr"])
                    writer.writeheader()
                    writer.writerows(self.rows)
            except OSError as exc:
                QMessageBox.critical(self, "Export failed", str(exc))
            else:
                self.status.setText(f"Exported {len(self.rows)} packets to CSV")

    def closeEvent(self, event):
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.settings.setValue("radio_host", self.host.text().strip())
        self.demo.stop()
        if self.radio is not None:
            self.radio.stop()
            if not self.radio.wait(5000):
                event.ignore()
                self.status.setText("Waiting for TCP connection to close; please try again.")
                return
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    log_path = configure_logging()
    log.info("%s starting (Python %s, %s)", APP_NAME, platform.python_version(), platform.machine())
    theme_path = Path(__file__).resolve().parent / "dashboard.qss"
    if theme_path.is_file():
        app.setStyleSheet(theme_path.read_text(encoding="utf-8"))
    window = MainWindow()
    window.log_path = log_path
    window.table.setAlternatingRowColors(True)
    window.show()
    sys.exit(app.exec())
