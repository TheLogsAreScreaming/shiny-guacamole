import csv
import logging
import os
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

from app_version import __version__
from PySide6.QtCore import QThread, Signal, QTimer, QSettings, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QSplitter, QTextEdit,
    QStatusBar,
)


def packet_row(packet):
    decoded = packet.get("decoded") or {}
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "from": str(packet.get("fromId") or packet.get("from") or ""),
        "type": str(decoded.get("portnum") or ""),
        "rssi": str(packet.get("rxRssi") if packet.get("rxRssi") is not None else ""),
        "snr": str(packet.get("rxSnr") if packet.get("rxSnr") is not None else ""),
    }


APP_NAME = "Meshtastic Sniffer TCP"
log = logging.getLogger("sniffer")

CSV_FIELDS = ["date", "time", "from", "type", "rssi", "snr"]
TABLE_FIELDS = ("time", "from", "type", "rssi", "snr")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Stop spreadsheet apps from running a cell as a formula.

    Text that starts with =, +, - or @ is prefixed with an apostrophe; plain
    numbers such as an RSSI of -80 are left as they are.
    """
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def write_packets_csv(path, rows):
    """Write packets to CSV via a temporary file, so a failure never leaves a partial export."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.partial")
    try:
        with open(temp, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(CSV_FIELDS)
            for row in rows:
                writer.writerow([csv_safe(row.get(key, "")) for key in CSV_FIELDS])
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return len(rows)


SETTINGS_ENV = "MESHTASTIC_SNIFFER_SETTINGS"


def resource_path(name):
    """Locate a file shipped next to app.py, or inside the packaged app."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / name


def open_settings():
    """App preferences; tests and scripts point SETTINGS_ENV at a scratch .ini file."""
    path = os.environ.get(SETTINGS_ENV)
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    return QSettings('BaconDisk22', 'MeshtasticSnifferTCP')


def default_export_name(stem, now=None):
    return f"{stem}-{(now or datetime.now()).strftime('%Y%m%d-%H%M')}.csv"


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
        self.settings = open_settings()
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

        self.visible_count = 0
        self.ask_save_path = self._ask_save_path
        self.confirm = self._confirm

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        connection_row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("Radio IP address (e.g. 192.168.1.50)")
        self.host.setText(self.settings.value("radio_host", "", type=str))
        self.host.returnPressed.connect(self.connect_radio)
        self.connect_btn = QPushButton("Connect TCP")
        self.connect_btn.clicked.connect(self.connect_radio)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_radio)
        self.demo_btn = QPushButton("Demo Mode")
        self.demo_btn.clicked.connect(self.toggle_demo)
        connection_row.addWidget(self.host, 1)
        for item in (self.connect_btn, self.disconnect_btn, self.demo_btn):
            connection_row.addWidget(item)
        layout.addLayout(connection_row)

        self.status = QLabel()
        self.status.setObjectName("connectionStatus")
        layout.addWidget(self.status)
        self.stats = QLabel("Packets: 0  |  Nodes: 0  |  Message types: 0")
        self.stats.setObjectName("stats")
        layout.addWidget(self.stats)
        self.diagnostics = QLabel()
        self.diagnostics.setObjectName("diagnostics")
        self.diagnostics.setWordWrap(True)
        layout.addWidget(self.diagnostics)

        log_row = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter packets by node ID or message type...")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self.apply_filter)
        self.pause_btn = QPushButton("Pause Auto-scroll")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.clicked.connect(self.confirm_clear_log)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export_csv)
        self.export_filtered_btn = QPushButton("Export Filtered")
        self.export_filtered_btn.clicked.connect(self.export_filtered_csv)
        log_row.addWidget(self.filter_input, 1)
        for item in (self.pause_btn, self.clear_btn, self.export_btn, self.export_filtered_btn):
            log_row.addWidget(item)
        layout.addLayout(log_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "From", "Port", "RSSI", "SNR"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in ((0, 90), (1, 120), (2, 190), (3, 70)):
            self.table.setColumnWidth(column, width)
        self.packet_details = QTextEdit()
        self.packet_details.setReadOnly(True)
        self.packet_details.setPlaceholderText("Select a packet to inspect its available metadata")
        self.packet_details.setMaximumHeight(120)
        self.table.itemSelectionChanged.connect(self.show_packet_details)
        self.node_table = QTableWidget(0, 2)
        self.node_table.setHorizontalHeaderLabels(["Node ID", "Packets"])
        self.node_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.node_table.verticalHeader().setVisible(False)
        self.node_table.horizontalHeader().setStretchLastSection(True)
        self.node_table.setColumnWidth(0, 120)
        self.node_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.node_table.setToolTip("Double-click a node to filter the packet log by it")
        self.copy_node_btn = QPushButton("Copy Selected Node ID")
        self.copy_node_btn.clicked.connect(self.copy_selected_node)
        self.node_table.cellDoubleClicked.connect(self.filter_by_node)
        splitter = QSplitter()
        splitter.addWidget(self.table)
        splitter.addWidget(self.node_table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.packet_details)
        bottom = QHBoxLayout()
        self.copy_diagnostics_btn = QPushButton("Copy Diagnostics")
        self.copy_diagnostics_btn.clicked.connect(self.copy_diagnostics)
        self.open_logs_btn = QPushButton("Open Log Folder")
        self.open_logs_btn.clicked.connect(self.open_log_folder)
        for item in (self.copy_node_btn, self.copy_diagnostics_btn, self.open_logs_btn):
            bottom.addWidget(item)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self.setStatusBar(QStatusBar())
        self._build_menus()
        self.set_status("● Disconnected", "offline")
        self.update_controls()
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
        text = f"Packets: {len(self.rows)}  |  Nodes: {len(self.seen_nodes)}  |  Message types: {len(self.port_counts)}"
        if self.filter_input.text().strip():
            text += f"  |  Showing {self.visible_count} of {len(self.rows)}"
        self.stats.setText(text)

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
            if not self.table.isRowHidden(0):
                self.visible_count -= 1
            self.table.removeRow(0)
            self.packet_details.clear()
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, key in enumerate(TABLE_FIELDS):
            self.table.setItem(row, col, QTableWidgetItem(data.get(key, "")))
        query = self.filter_input.text().strip().casefold()
        if query and not self._row_matches_filter(data, query):
            self.table.setRowHidden(row, True)
        else:
            self.visible_count += 1
        self._update_stats()
        if len(self.rows) == 1:
            self.update_controls()
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
        self.set_status(message, "connected" if message.startswith("● Connected") else self.status_state)

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
                self.set_status("✕ Connection failed: " + detail, "error")
            elif not self.demo.isActive():
                self.set_status("● Disconnected", "offline")
            self.update_controls()
        radio.deleteLater()

    def _schedule_reconnect(self, reason, detail):
        # Called only once the previous worker has fully finished, so the old
        # interface is closed and its subscriptions are gone before retrying.
        label = "Connection lost" if reason == "lost" else "Connection failed"
        self.retry_count += 1
        if self.retry_count > self.max_retries:
            self.reconnect_enabled = False
            self.record_event(f"Gave up reconnecting after {self.max_retries} attempts", logging.WARNING)
            self.set_status(
                f"✕ {label}: {detail} — gave up after {self.max_retries} attempts. "
                "Select Connect TCP to try again.",
                "error",
            )
            return
        delay = reconnect_delay_ms(self.retry_count)
        self.retry_timer.start(delay)
        self.record_event(f"Reconnect attempt {self.retry_count}/{self.max_retries} in {delay // 1000}s")
        self.set_status(
            f"◌ {label}: {detail} — reconnecting in {delay // 1000}s "
            f"(attempt {self.retry_count}/{self.max_retries})",
            "warning",
        )

    def _retry_connect(self):
        if self.reconnect_enabled and self.radio is None and self.reconnect_host:
            self._start_radio(self.reconnect_host)
            self.set_status(
                f"◌ Reconnecting to {self.reconnect_host} "
                f"(attempt {self.retry_count}/{self.max_retries})...",
                "connecting",
            )

    def connect_radio(self):
        host = self.host.text().strip()
        if not host:
            QMessageBox.warning(self, "Missing address", "Enter your radio IP address.")
            return
        if self.radio is not None:
            self.notify("Disconnect the current radio before connecting again.")
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
        self.set_status(f"◌ Connecting to {host}...", "connecting")
        self.record_event("Connecting to " + host)
        self.radio.start()
        self.update_controls()

    def disconnect_radio(self):
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.retry_count = 0
        if self.radio is not None or self.reconnect_host:
            self.record_event("Disconnect requested by user")
        if self.radio is not None:
            self.radio.stop()
            self.set_status("◌ Disconnecting...", "connecting")
        elif not self.demo.isActive():
            self.set_status("● Disconnected", "offline")
        self.update_controls()

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
            f"Version: {__version__}",
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
        self.notify("Copied diagnostics to clipboard")

    def open_log_folder(self):
        if self.log_path is None:
            QMessageBox.information(self, "Logging unavailable", "The log file could not be created on this Mac.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.log_path).parent)))

    def toggle_demo(self):
        if self.demo.isActive():
            self.demo.stop()
            self.set_status("● Disconnected", "offline")
            self.update_controls()
            return
        if self.radio is not None:
            self.notify("Disconnect the radio before starting demo mode.")
            return
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.demo.start(1200)
        self.set_status("● Demo mode — simulated packets", "demo")
        self.update_controls()

    def demo_packet(self):
        now = datetime.now()
        self.add_packet({
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "from": random.choice(self.demo_nodes),
            "type": random.choice(["TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP"]),
            "rssi": str(random.randint(-120, -45)),
            "snr": str(round(random.uniform(-15, 12), 1)),
        })

    def show_packet_details(self):
        index = self.table.currentRow()
        if 0 <= index < len(self.rows):
            packet = self.rows[index]
            keys = CSV_FIELDS if packet.get("date") else TABLE_FIELDS
            self.packet_details.setPlainText("\n".join(f"{key}: {packet.get(key, '')}" for key in keys))
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
            self.notify("Copied node ID: " + item.text())

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
        visible = 0
        self.table.setUpdatesEnabled(False)
        try:
            for index, packet in enumerate(self.rows):
                matches = self._row_matches_filter(packet, query)
                self.table.setRowHidden(index, not matches)
                visible += matches
        finally:
            self.table.setUpdatesEnabled(True)
        self.visible_count = visible
        self._update_stats()
        self.update_controls()

    def toggle_pause(self):
        self.display_paused = not self.display_paused
        self.pause_btn.setText("Resume Auto-scroll" if self.display_paused else "Pause Auto-scroll")
        self.pause_action.setChecked(self.display_paused)
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
        self.visible_count = 0
        self._update_stats()
        self.update_controls()

    def visible_rows(self):
        return [packet for index, packet in enumerate(self.rows) if not self.table.isRowHidden(index)]

    def export_filtered_csv(self):
        return self._export(self.visible_rows(), "Export filtered packet log", "meshtastic-filtered-packets", "filtered packets")

    def export_csv(self):
        return self._export(list(self.rows), "Export packet log", "meshtastic-packets", "packets")

    def _export(self, rows, title, stem, noun):
        """Ask where to save, write the CSV, and report the result. Returns the path or None."""
        if not rows:
            self.notify(f"No {noun} to export.")
            return None
        folder = self.settings.value("export_dir", "", type=str)
        if not folder or not Path(folder).is_dir():
            folder = str(Path.home())
        path = self.ask_save_path(title, str(Path(folder) / default_export_name(stem)))
        if not path:
            return None
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(".csv")
        try:
            count = write_packets_csv(path, rows)
        except OSError as exc:
            log.warning("CSV export to %s failed: %s", path, exc)
            QMessageBox.critical(self, "Export failed", f"Could not write {path.name}:\n{exc}")
            return None
        self.settings.setValue("export_dir", str(path.parent))
        log.info("Exported %d %s to %s", count, noun, path)
        self.notify(f"Exported {count} {noun} to {path.name}")
        return path

    def _ask_save_path(self, title, default):
        path, _ = QFileDialog.getSaveFileName(self, title, default, "CSV files (*.csv)")
        return path

    def _confirm(self, title, text):
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def confirm_clear_log(self):
        if self.rows and not self.confirm(
            "Clear packet log?",
            f"Remove all {len(self.rows)} packets from the log? Export first if you need them.",
        ):
            return False
        self.clear_log()
        return True

    def about_text(self):
        return (
            f"<b>{APP_NAME}</b><br>Version {__version__}<br><br>"
            "Receive-focused Meshtastic packet monitor for Intel Macs. It shows what your "
            "connected radio reports over Wi-Fi/TCP; it is not an over-the-air sniffer.<br><br>"
            "Independent community software; not affiliated with the Meshtastic project. "
            "Licensed under GPL-3.0."
        )

    def show_about(self):
        QMessageBox.about(self, "About " + APP_NAME, self.about_text())

    def focus_filter(self):
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def set_status(self, text, state):
        """Show the connection state; the stylesheet colours it by state."""
        self.status_state = state
        self.status.setText(text)
        self.status.setProperty("state", state)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def notify(self, message, timeout=6000):
        """Show a short-lived message without overwriting the connection state."""
        self.statusBar().showMessage(message, timeout)

    def update_controls(self):
        busy = self.radio is not None or self.retry_timer.isActive()
        demo = self.demo.isActive()
        self.connect_btn.setEnabled(not busy and not demo)
        self.host.setEnabled(not busy)
        self.disconnect_btn.setEnabled(busy)
        self.demo_btn.setEnabled(not busy)
        self.demo_btn.setText("Stop Demo" if demo else "Demo Mode")
        has_rows = bool(self.rows)
        self.export_btn.setEnabled(has_rows)
        self.export_filtered_btn.setEnabled(has_rows and bool(self.filter_input.text().strip()))
        self.clear_btn.setEnabled(has_rows)
        for action, enabled in (
            (self.connect_action, self.connect_btn.isEnabled()),
            (self.disconnect_action, busy),
            (self.export_action, self.export_btn.isEnabled()),
            (self.export_filtered_action, self.export_filtered_btn.isEnabled()),
        ):
            action.setEnabled(enabled)

    def _build_menus(self):
        def action(menu, text, slot, shortcut=None, checkable=False):
            item = QAction(text, self)
            if shortcut is not None:
                item.setShortcut(QKeySequence(shortcut))
            item.setCheckable(checkable)
            item.triggered.connect(slot)
            menu.addAction(item)
            return item

        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        self.export_action = action(file_menu, "Export CSV…", self.export_csv, "Ctrl+E")
        self.export_filtered_action = action(file_menu, "Export Filtered CSV…", self.export_filtered_csv, "Ctrl+Shift+E")
        file_menu.addSeparator()
        action(file_menu, "Quit", self.close, QKeySequence.StandardKey.Quit)

        edit_menu = bar.addMenu("&Edit")
        action(edit_menu, "Find Packets", self.focus_filter, QKeySequence.StandardKey.Find)
        action(edit_menu, "Copy Selected Node ID", self.copy_selected_node)
        action(edit_menu, "Copy Diagnostics", self.copy_diagnostics)
        edit_menu.addSeparator()
        action(edit_menu, "Clear Log…", self.confirm_clear_log, "Ctrl+Shift+K")

        radio_menu = bar.addMenu("&Radio")
        self.connect_action = action(radio_menu, "Connect", self.connect_radio, "Ctrl+K")
        self.disconnect_action = action(radio_menu, "Disconnect", self.disconnect_radio, "Ctrl+Shift+D")
        action(radio_menu, "Demo Mode", self.toggle_demo)

        view_menu = bar.addMenu("&View")
        self.pause_action = action(view_menu, "Pause Auto-scroll", self.toggle_pause, "Ctrl+Shift+P", checkable=True)

        help_menu = bar.addMenu("&Help")
        action(help_menu, "About Meshtastic Sniffer TCP", self.show_about)
        action(help_menu, "Open Log Folder", self.open_log_folder)

    def closeEvent(self, event):
        self.reconnect_enabled = False
        self.retry_timer.stop()
        self.settings.setValue("radio_host", self.host.text().strip())
        self.demo.stop()
        if self.radio is not None:
            self.radio.stop()
            if not self.radio.wait(5000):
                event.ignore()
                self.notify("Waiting for the TCP connection to close; please try again.")
                return
        super().closeEvent(event)


def load_theme(app):
    theme_path = resource_path("dashboard.qss")
    if not theme_path.is_file():
        log.warning("Theme file not found at %s", theme_path)
        return False
    app.setStyleSheet(theme_path.read_text(encoding="utf-8"))
    return True


def self_check():
    """Prove a packaged build has everything it needs. Opens no window and no connection."""
    import meshtastic.tcp_interface  # noqa: F401  (imported lazily in normal use)
    from pubsub import pub  # noqa: F401

    app = QApplication.instance() or QApplication(sys.argv)
    if not load_theme(app):
        print("self-check failed: theme missing", file=sys.stderr)
        return 1
    MainWindow()
    print(f"self-check ok: {APP_NAME} {__version__}, PySide6 {package_version('PySide6')}, meshtastic {package_version('meshtastic')}")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        sys.exit(self_check())
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    log_path = configure_logging()
    log.info("%s %s starting (Python %s, %s)", APP_NAME, __version__, platform.python_version(), platform.machine())
    load_theme(app)
    window = MainWindow()
    window.log_path = log_path
    window.show()
    sys.exit(app.exec())
