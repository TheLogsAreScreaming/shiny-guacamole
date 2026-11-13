"""Optional integrations: webhooks, a localhost-only API, Prometheus metrics
and InfluxDB line protocol for mesh telemetry.

Everything here is OFF by default and uses only the Python standard library,
so it adds nothing to the packaged app. Nothing in this module talks to the
radio directly: the host app feeds packets in with ``IntegrationHub.observe``
and (optionally) hands over a ``send_text`` callback for the assistant link.

In Meshtastic Sniffer TCP the settings are edited in Radio > Integrations…
and stored with the app's other preferences (see integrations_ui.py).
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

EVENT_TYPES = ("message", "node", "telemetry")
DEFAULT_PORT = 9481
MAX_MESSAGES = 200
MAX_TELEMETRY_PER_NODE = 500
MAX_NODES = 2000
MAX_SEND_CHARS = 200
USER_AGENT = "mesh-integrations/1"

# Telemetry fields exported to Prometheus/InfluxDB: (group, key) -> metric name
TELEMETRY_FIELDS = {
    ("deviceMetrics", "batteryLevel"): "battery_percent",
    ("deviceMetrics", "voltage"): "voltage_volts",
    ("deviceMetrics", "channelUtilization"): "channel_utilization_percent",
    ("deviceMetrics", "airUtilTx"): "air_util_tx_percent",
    ("deviceMetrics", "uptimeSeconds"): "uptime_seconds",
    ("environmentMetrics", "temperature"): "temperature_celsius",
    ("environmentMetrics", "relativeHumidity"): "relative_humidity_percent",
    ("environmentMetrics", "barometricPressure"): "barometric_pressure_hpa",
    ("environmentMetrics", "gasResistance"): "gas_resistance_ohms",
    ("environmentMetrics", "iaq"): "iaq",
    ("environmentMetrics", "lux"): "lux",
    ("environmentMetrics", "windSpeed"): "wind_speed_mps",
    ("environmentMetrics", "windDirection"): "wind_direction_degrees",
    ("environmentMetrics", "rainfall1h"): "rainfall_1h_mm",
    ("powerMetrics", "ch1Voltage"): "ch1_voltage_volts",
    ("powerMetrics", "ch1Current"): "ch1_current_ma",
}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _split(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()]


def valid_webhook_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and not any(c.isspace() for c in url)


@dataclass
class IntegrationSettings:
    webhooks_enabled: bool = False
    webhook_urls: list[str] = field(default_factory=list)
    webhook_events: set[str] = field(default_factory=lambda: set(EVENT_TYPES))
    webhook_include_text: bool = True
    webhook_secret: str = ""
    local_api_enabled: bool = False
    local_api_port: int = DEFAULT_PORT
    metrics_enabled: bool = True
    assistant_send_enabled: bool = False
    influx_file: str = ""
    influx_url: str = ""
    influx_token: str = ""

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any] | None) -> "IntegrationSettings":
        s = {str(k).lower(): v for k, v in (section or {}).items()}
        events = {e.lower() for e in _split(s.get("webhook_events"))} & set(EVENT_TYPES)
        try:
            port = int(str(s.get("local_api_port") or DEFAULT_PORT).strip())
        except ValueError:
            port = DEFAULT_PORT
        if not 1024 <= port <= 65535:
            port = DEFAULT_PORT
        return cls(
            webhooks_enabled=_truthy(s.get("webhooks_enabled")),
            webhook_urls=[u for u in _split(s.get("webhook_urls")) if valid_webhook_url(u)],
            webhook_events=events if s.get("webhook_events") not in (None, "") else set(EVENT_TYPES),
            webhook_include_text=_truthy(s.get("webhook_include_text"), True),
            webhook_secret=str(s.get("webhook_secret") or "").strip(),
            local_api_enabled=_truthy(s.get("local_api_enabled")),
            local_api_port=port,
            metrics_enabled=_truthy(s.get("metrics_enabled"), True),
            assistant_send_enabled=_truthy(s.get("assistant_send_enabled")),
            influx_file=str(s.get("influx_file") or "").strip(),
            influx_url=str(s.get("influx_url") or "").strip(),
            influx_token=str(s.get("influx_token") or "").strip(),
        )

    @property
    def any_enabled(self) -> bool:
        return bool(self.webhooks_enabled or self.local_api_enabled or self.influx_file or self.influx_url)


# --------------------------------------------------------------------------
# Packet normalisation
# --------------------------------------------------------------------------

def node_id_str(num: Any, fallback: Any = None) -> str:
    if isinstance(fallback, str) and fallback.startswith("!"):
        return fallback
    try:
        return f"!{int(num) & 0xFFFFFFFF:08x}"
    except (TypeError, ValueError):
        return str(fallback or num or "unknown")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def normalize_packet(packet: Mapping[str, Any], now: float | None = None) -> list[tuple[str, dict]]:
    """Turn a meshtastic-python packet dict into (event_type, payload) pairs."""
    if not isinstance(packet, Mapping):
        return []
    now = time.time() if now is None else now
    decoded = packet.get("decoded") if isinstance(packet.get("decoded"), Mapping) else {}
    portnum = str(decoded.get("portnum") or "")
    node = node_id_str(packet.get("from"), packet.get("fromId"))
    base = {
        "node_id": node,
        "time": int(_num(packet.get("rxTime")) or now),
        "channel": packet.get("channel", 0) if isinstance(packet.get("channel", 0), int) else 0,
    }
    snr, rssi = _num(packet.get("rxSnr")), _num(packet.get("rxRssi"))
    hop_start, hop_limit = packet.get("hopStart"), packet.get("hopLimit")
    hops = hop_start - hop_limit if isinstance(hop_start, int) and isinstance(hop_limit, int) else None
    seen = dict(base, portnum=portnum or "UNKNOWN", snr=snr, rssi=rssi, hops_away=hops)
    events: list[tuple[str, dict]] = [("node", seen)]
    if portnum == "TEXT_MESSAGE_APP":
        to = packet.get("to")
        text = decoded.get("text")
        if not isinstance(text, str):
            payload = decoded.get("payload")
            text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else ""
        events.append(("message", dict(
            base, text=text, to=node_id_str(to, packet.get("toId")),
            direct=isinstance(to, int) and to != 0xFFFFFFFF, snr=snr, rssi=rssi)))
    elif portnum == "NODEINFO_APP" and isinstance(decoded.get("user"), Mapping):
        user = decoded["user"]
        seen.update(long_name=str(user.get("longName") or ""), short_name=str(user.get("shortName") or ""),
                    hw_model=str(user.get("hwModel") or ""))
    elif portnum == "POSITION_APP" and isinstance(decoded.get("position"), Mapping):
        pos = decoded["position"]
        lat, lon = _num(pos.get("latitude")), _num(pos.get("longitude"))
        if lat is None and _num(pos.get("latitudeI")) is not None:
            lat = pos["latitudeI"] / 1e7
        if lon is None and _num(pos.get("longitudeI")) is not None:
            lon = pos["longitudeI"] / 1e7
        if lat is not None and lon is not None:
            seen.update(latitude=lat, longitude=lon, altitude=_num(pos.get("altitude")))
    elif portnum == "TELEMETRY_APP" and isinstance(decoded.get("telemetry"), Mapping):
        values = telemetry_values(decoded["telemetry"])
        if values:
            events.append(("telemetry", dict(base, metrics=values)))
    return events


def telemetry_values(telemetry: Mapping[str, Any]) -> dict[str, float]:
    values = {}
    for (group, key), name in TELEMETRY_FIELDS.items():
        block = telemetry.get(group)
        if isinstance(block, Mapping):
            value = _num(block.get(key))
            if value is not None:
                values[name] = value
    return values


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class MeshState:
    """Thread-safe in-memory view of what the app has heard."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self.started = clock()
        self.nodes: dict[str, dict] = {}
        self.messages: collections.deque = collections.deque(maxlen=MAX_MESSAGES)
        self.telemetry: dict[str, collections.deque] = {}
        self.packets_by_port: collections.Counter = collections.Counter()
        self.radio: dict[str, Any] = {"connected": False}
        self.messages_sent = 0

    def apply(self, kind: str, payload: dict) -> None:
        with self._lock:
            node_id = payload.get("node_id", "unknown")
            if kind == "node":
                self.packets_by_port[payload.get("portnum", "UNKNOWN")] += 1
                node = self.nodes.setdefault(node_id, {"node_id": node_id, "first_heard": payload["time"], "packets": 0})
                node["packets"] += 1
                node["last_heard"] = payload["time"]
                for key in ("snr", "rssi", "hops_away", "long_name", "short_name", "hw_model",
                            "latitude", "longitude", "altitude"):
                    if payload.get(key) not in (None, ""):
                        node[key] = payload[key]
                if len(self.nodes) > MAX_NODES:
                    oldest = min(self.nodes.values(), key=lambda n: n.get("last_heard", 0))
                    self.nodes.pop(oldest["node_id"], None)
            elif kind == "message":
                self.messages.append(dict(payload))
            elif kind == "telemetry":
                series = self.telemetry.setdefault(node_id, collections.deque(maxlen=MAX_TELEMETRY_PER_NODE))
                series.append({"time": payload["time"], **payload["metrics"]})
                node = self.nodes.setdefault(node_id, {"node_id": node_id, "first_heard": payload["time"], "packets": 0})
                node.setdefault("telemetry", {}).update(payload["metrics"])

    def set_radio(self, **info: Any) -> None:
        with self._lock:
            self.radio.update(info)

    def note_sent(self) -> None:
        with self._lock:
            self.messages_sent += 1

    def node_name(self, node_id: str) -> str:
        node = self.nodes.get(node_id) or {}
        return node.get("long_name") or node.get("short_name") or node_id

    # Snapshots (copies, safe to serialise outside the lock)
    def list_nodes(self, limit: int = 100) -> list[dict]:
        with self._lock:
            nodes = sorted(self.nodes.values(), key=lambda n: n.get("last_heard", 0), reverse=True)
            return [json.loads(json.dumps(n)) for n in nodes[:max(1, min(limit, MAX_NODES))]]

    def recent_messages(self, limit: int = 20, channel: int | None = None) -> list[dict]:
        with self._lock:
            items = [m for m in self.messages if channel is None or m.get("channel") == channel]
            out = [dict(m) for m in items[-max(1, min(limit, MAX_MESSAGES)):]]
        for message in out:
            message["from_name"] = self.node_name(message["node_id"])
        return out

    def find_node(self, query: str) -> str | None:
        q = (query or "").strip().lower()
        if not q:
            return None
        with self._lock:
            for node_id, node in self.nodes.items():
                names = (node_id.lower(), node_id.lower().lstrip("!"), str(node.get("short_name", "")).lower(),
                         str(node.get("long_name", "")).lower())
                if q in names:
                    return node_id
        return None

    def node_telemetry(self, node_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(p) for p in list(self.telemetry.get(node_id, ()))[-max(1, min(limit, MAX_TELEMETRY_PER_NODE)):]]

    def all_telemetry(self) -> dict[str, list[dict]]:
        with self._lock:
            return {k: [dict(p) for p in v] for k, v in self.telemetry.items()}

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            active = sum(1 for n in self.nodes.values() if now - n.get("last_heard", 0) <= 3600)
            return {
                "radio": dict(self.radio),
                "uptime_seconds": int(now - self.started),
                "nodes_known": len(self.nodes),
                "nodes_heard_last_hour": active,
                "messages_buffered": len(self.messages),
                "messages_sent_by_assistant": self.messages_sent,
                "packets_total": sum(self.packets_by_port.values()),
                "packets_by_port": dict(self.packets_by_port),
            }


# --------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------

def _prom_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus(state: MeshState, prefix: str = "mesh") -> str:
    status = state.status()
    lines = [
        f"# HELP {prefix}_up 1 while the app is running.", f"# TYPE {prefix}_up gauge", f"{prefix}_up 1",
        f"# HELP {prefix}_radio_connected 1 when the radio link is up.", f"# TYPE {prefix}_radio_connected gauge",
        f"{prefix}_radio_connected {1 if status['radio'].get('connected') else 0}",
        f"# TYPE {prefix}_nodes_known gauge", f"{prefix}_nodes_known {status['nodes_known']}",
        f"# TYPE {prefix}_nodes_heard_last_hour gauge", f"{prefix}_nodes_heard_last_hour {status['nodes_heard_last_hour']}",
        f"# TYPE {prefix}_packets_total counter",
    ]
    for port, count in sorted(status["packets_by_port"].items()):
        lines.append(f'{prefix}_packets_total{{portnum="{_prom_label(port)}"}} {count}')
    nodes = state.list_nodes(MAX_NODES)
    per_metric: dict[str, list[str]] = collections.defaultdict(list)
    for node in nodes:
        labels = f'node="{_prom_label(node["node_id"])}",name="{_prom_label(node.get("long_name") or node.get("short_name") or "")}"'
        if node.get("last_heard"):
            per_metric["node_last_heard_timestamp_seconds"].append(f"{{{labels}}} {int(node['last_heard'])}")
        for key in ("snr", "rssi"):
            if node.get(key) is not None:
                per_metric[f"node_{key}"].append(f"{{{labels}}} {node[key]:g}")
        for name, value in sorted((node.get("telemetry") or {}).items()):
            per_metric[f"node_{name}"].append(f"{{{labels}}} {value:g}")
    for metric, samples in sorted(per_metric.items()):
        lines.append(f"# TYPE {prefix}_{metric} gauge")
        lines.extend(f"{prefix}_{metric}{sample}" for sample in samples)
    return "\n".join(lines) + "\n"


def _influx_tag(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ").replace("\n", "\\n")


def influx_lines(node_id: str, metrics: Mapping[str, float], timestamp: int, name: str = "",
                 measurement: str = "mesh_telemetry") -> str:
    """One line of InfluxDB line protocol (precision = seconds)."""
    fields = ",".join(f"{_influx_tag(k)}={float(v)!r}" for k, v in sorted(metrics.items())
                      if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not fields:
        return ""
    tags = f"node={_influx_tag(node_id)}" + (f",name={_influx_tag(name)}" if name else "")
    return f"{measurement},{tags} {fields} {int(timestamp)}"


def export_influx(state: MeshState) -> str:
    """All buffered telemetry as line protocol, oldest first."""
    out = []
    for node_id, points in sorted(state.all_telemetry().items()):
        name = state.node_name(node_id)
        name = "" if name == node_id else name
        for point in points:
            metrics = {k: v for k, v in point.items() if k != "time"}
            line = influx_lines(node_id, metrics, point["time"], name)
            if line:
                out.append(line)
    return "\n".join(out) + ("\n" if out else "")


# --------------------------------------------------------------------------
# Background HTTP delivery (webhooks + Influx push)
# --------------------------------------------------------------------------

def _default_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (user-configured URL)
        return response.status


class Deliverer:
    """Bounded queue + one worker thread so slow endpoints never block the radio."""

    def __init__(self, post: Callable[[str, bytes, Mapping[str, str], float], int] = _default_post,
                 max_queue: int = 500, timeout: float = 5.0, retries: int = 1, retry_delay: float = 2.0):
        self._post = post
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._timeout, self._retries, self._retry_delay = timeout, retries, retry_delay
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "last_error": ""}

    def submit(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait((url, body, dict(headers)))
        except queue.Full:
            self.stats["dropped"] += 1

    def _ensure_started(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mesh-integrations-delivery", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            self.deliver_now(*item)
            self._queue.task_done()

    def deliver_now(self, url: str, body: bytes, headers: Mapping[str, str]) -> bool:
        for attempt in range(self._retries + 1):
            try:
                status = self._post(url, body, headers, self._timeout)
                if 200 <= int(status) < 300:
                    self.stats["sent"] += 1
                    return True
                error = f"HTTP {status}"
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
            except Exception as exc:  # network errors must never reach the radio thread
                error = type(exc).__name__
            self.stats["last_error"] = f"{urllib.parse.urlparse(url).netloc}: {error}"
            if attempt < self._retries and not self._stop.wait(self._retry_delay):
                continue
        self.stats["failed"] += 1
        return False

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Hub
# --------------------------------------------------------------------------

class IntegrationHub:
    def __init__(self, settings: IntegrationSettings | None = None, app_name: str = "mesh-app",
                 data_dir: str | os.PathLike | None = None, deliverer: Deliverer | None = None,
                 clock: Callable[[], float] = time.time, message_text: bool = True):
        self.settings = settings or IntegrationSettings()
        self.app_name = app_name
        self.data_dir = Path(data_dir) if data_dir else Path.cwd()
        self.state = MeshState(clock)
        self.deliverer = deliverer or Deliverer()
        self.send_text: Callable[[str, int, str | None], Any] | None = None
        self.server: LocalApiServer | None = None
        self._influx_lock = threading.Lock()
        # False for apps that never show message content: text is dropped on arrival.
        self.message_text = message_text

    # Feeding -------------------------------------------------------------
    def observe(self, packet: Mapping[str, Any]) -> list[tuple[str, dict]]:
        """Record a received packet and fan it out. Never raises."""
        try:
            events = normalize_packet(packet)
            for kind, payload in events:
                if kind == "message" and not self.message_text:
                    payload.pop("text", None)
                self.state.apply(kind, payload)
                self._fan_out(kind, payload)
            return events
        except Exception:
            return []

    def _fan_out(self, kind: str, payload: dict) -> None:
        s = self.settings
        if s.webhooks_enabled and s.webhook_urls and kind in s.webhook_events:
            body_payload = dict(payload)
            if kind == "message" and not s.webhook_include_text:
                body_payload.pop("text", None)
            if kind == "node":
                body_payload["name"] = self.state.node_name(payload["node_id"])
            body = json.dumps({"app": self.app_name, "event": kind, "data": body_payload},
                              separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "X-Mesh-Event": kind}
            if s.webhook_secret:
                headers["X-Mesh-Signature"] = sign(s.webhook_secret, body)
            for url in s.webhook_urls:
                self.deliverer.submit(url, body, headers)
        if kind == "telemetry" and (s.influx_file or s.influx_url):
            name = self.state.node_name(payload["node_id"])
            line = influx_lines(payload["node_id"], payload["metrics"], payload["time"],
                                "" if name == payload["node_id"] else name)
            if line:
                self._write_influx(line)

    def _write_influx(self, line: str) -> None:
        s = self.settings
        if s.influx_file:
            path = Path(os.path.expanduser(s.influx_file))
            if not path.is_absolute():
                path = self.data_dir / path
            try:
                with self._influx_lock:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
            except OSError:
                pass
        if s.influx_url and valid_webhook_url(s.influx_url):
            headers = {"Content-Type": "text/plain; charset=utf-8", "User-Agent": USER_AGENT}
            if s.influx_token:
                headers["Authorization"] = f"Token {s.influx_token}"
            self.deliverer.submit(s.influx_url, (line + "\n").encode("utf-8"), headers)

    # Assistant actions -------------------------------------------------------
    def send_message(self, text: str, channel: int = 0, destination: str | None = None) -> dict:
        if not self.settings.assistant_send_enabled:
            raise PermissionError("Sending is turned off. Set assistant_send_enabled = True in [integrations] to allow it.")
        if self.send_text is None:
            raise RuntimeError("The radio is not ready to send.")
        text = (text or "").strip()
        if not text:
            raise ValueError("Message text is empty.")
        if len(text) > MAX_SEND_CHARS:
            raise ValueError(f"Message is longer than {MAX_SEND_CHARS} characters.")
        if not isinstance(channel, int) or not 0 <= channel <= 7:
            raise ValueError("Channel must be 0-7.")
        dest = None
        if destination:
            dest = self.state.find_node(destination)
            if dest is None:
                raise ValueError(f"Unknown node: {destination}")
        self.send_text(text, channel, dest)
        self.state.note_sent()
        return {"sent": True, "channel": channel, "destination": dest, "chars": len(text)}

    # Lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self.settings.local_api_enabled and self.server is None:
            self.server = LocalApiServer(self, self.settings.local_api_port, self.data_dir)
            self.server.start()
            write_port_file(self.data_dir, self.server.port)

    def stop(self) -> None:
        if self.server:
            self.server.stop()
            self.server = None
            try:
                port_file(self.data_dir).unlink()
            except OSError:
                pass
        self.deliverer.stop()

    def describe(self) -> dict:
        s = self.settings
        return {
            "webhooks": {"enabled": s.webhooks_enabled, "endpoints": len(s.webhook_urls),
                         "events": sorted(s.webhook_events), **self.deliverer.stats},
            "local_api": {"enabled": s.local_api_enabled, "port": s.local_api_port,
                          "metrics": s.local_api_enabled and s.metrics_enabled},
            "assistant_send_enabled": s.assistant_send_enabled,
            "influx": {"file": bool(s.influx_file), "push": bool(s.influx_url)},
        }


# --------------------------------------------------------------------------
# Localhost API (assistant link + /metrics)
# --------------------------------------------------------------------------

TOKEN_FILE = "assistant-token"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


PORT_FILE = "local-api.json"


def port_file(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / PORT_FILE


def write_port_file(data_dir: str | os.PathLike, port: int) -> None:
    """Tell the assistant link which port the running app listens on."""
    path = port_file(data_dir)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"port": int(port)}, handle)


def token_path(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / TOKEN_FILE


def load_or_create_token(data_dir: str | os.PathLike) -> str:
    path = token_path(data_dir)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def _host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "MeshIntegrations/1"
    hub: IntegrationHub
    token: str

    def log_message(self, *_args: Any) -> None:  # keep the app log quiet
        return

    def _reply(self, status: int, body: Any, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, sort_keys=True).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self) -> bool:
        # Refuse DNS-rebinding style requests and anything not addressed to loopback.
        if not _host_is_loopback(self.headers.get("Host")):
            self._reply(403, {"error": "forbidden host"})
            return False
        return True

    def _authorised(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self._reply(401, {"error": "missing or wrong token"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        hub = self.hub

        def qint(name: str, default: int) -> int:
            try:
                return int(query.get(name, [default])[0])
            except (TypeError, ValueError):
                return default

        if parsed.path == "/metrics":
            if not hub.settings.metrics_enabled:
                return self._reply(404, {"error": "metrics disabled"})
            return self._reply(200, render_prometheus(hub.state), "text/plain; version=0.0.4; charset=utf-8")
        if parsed.path == "/influx":
            if not hub.settings.metrics_enabled:
                return self._reply(404, {"error": "metrics disabled"})
            return self._reply(200, export_influx(hub.state), "text/plain; charset=utf-8")
        if parsed.path == "/healthz":
            return self._reply(200, {"ok": True})
        if not parsed.path.startswith("/api/"):
            return self._reply(404, {"error": "not found"})
        if not self._authorised():
            return
        if parsed.path == "/api/status":
            return self._reply(200, {"app": hub.app_name, **hub.state.status(), "integrations": hub.describe()})
        if parsed.path == "/api/nodes":
            return self._reply(200, {"nodes": hub.state.list_nodes(qint("limit", 50))})
        if parsed.path == "/api/messages":
            channel = query.get("channel", [None])[0]
            try:
                channel_num = int(channel) if channel not in (None, "") else None
            except ValueError:
                channel_num = None
            return self._reply(200, {"messages": hub.state.recent_messages(qint("limit", 20), channel_num)})
        if parsed.path == "/api/telemetry":
            node = hub.state.find_node(query.get("node", [""])[0])
            if not node:
                return self._reply(404, {"error": "unknown node"})
            return self._reply(200, {"node_id": node, "name": hub.state.node_name(node),
                                     "points": hub.state.node_telemetry(node, qint("limit", 50))})
        return self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard() or not self._authorised():
            return
        if urllib.parse.urlparse(self.path).path != "/api/send":
            return self._reply(404, {"error": "not found"})
        if not self.hub.settings.assistant_send_enabled:
            return self._reply(403, {"error": "sending is turned off in settings"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
            body = json.loads(self.rfile.read(length) or b"{}")
            result = self.hub.send_message(str(body.get("text", "")), int(body.get("channel", 0)),
                                           body.get("destination") or None)
        except PermissionError as exc:
            return self._reply(403, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._reply(400, {"error": str(exc)})
        except Exception as exc:
            return self._reply(503, {"error": str(exc) or type(exc).__name__})
        return self._reply(200, result)


class LocalApiServer:
    """HTTP server bound to 127.0.0.1 only. Never listens on other interfaces."""

    def __init__(self, hub: IntegrationHub, port: int, data_dir: str | os.PathLike):
        self.token = load_or_create_token(data_dir)
        handler = type("BoundHandler", (_Handler,), {"hub": hub, "token": self.token})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="mesh-local-api", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def hub_from_config(config: Any, app_name: str, data_dir: str | os.PathLike) -> IntegrationHub:
    """Build a hub from a ConfigParser (or dict of dicts); missing section = all off."""
    section: Mapping[str, Any] = {}
    try:
        if config is not None and "integrations" in config:
            section = dict(config["integrations"])
    except Exception:
        section = {}
    return IntegrationHub(IntegrationSettings.from_mapping(section), app_name, data_dir)


def iter_events(hub: IntegrationHub, packets: Iterable[Mapping[str, Any]]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for packet in packets:
        out.extend(hub.observe(packet))
    return out
