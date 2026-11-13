"""Integrations settings for Meshtastic Sniffer TCP: storage and the Radio > Integrations… dialog.

Settings live with the app's other QSettings preferences under ``integrations/``.
Everything is off by default. This app is a monitor: it never shows message text
and never transmits, so message webhooks carry metadata only and the assistant
link has no send tool.
"""
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QSpinBox, QVBoxLayout,
)

from mesh_integrations import DEFAULT_PORT, EVENT_TYPES, IntegrationHub, IntegrationSettings, valid_webhook_url

INTEGRATIONS_DIR_ENV = "MESHTASTIC_SNIFFER_INTEGRATIONS_DIR"
GROUP = "integrations"
APP_ID = "meshtastic-sniffer-tcp"


def integrations_dir():
    """Folder for the assistant token, port file and line-protocol file."""
    override = os.environ.get(INTEGRATIONS_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Meshtastic Sniffer TCP" / "integrations"
    return Path.home() / ".meshtastic-sniffer-tcp" / "integrations"


def _bool(settings, key, default=False):
    value = settings.value(f"{GROUP}/{key}", default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_integration_settings(settings):
    """QSettings -> IntegrationSettings (assistant sending is never allowed here)."""
    def text(key):
        return str(settings.value(f"{GROUP}/{key}", "") or "")
    events = text("webhook_events")
    loaded = IntegrationSettings.from_mapping({
        "webhooks_enabled": _bool(settings, "webhooks_enabled"),
        "webhook_urls": text("webhook_urls"),
        "webhook_events": events if events else ",".join(EVENT_TYPES),
        "webhook_secret": text("webhook_secret"),
        "local_api_enabled": _bool(settings, "local_api_enabled"),
        "local_api_port": text("local_api_port") or str(DEFAULT_PORT),
        "metrics_enabled": _bool(settings, "metrics_enabled", True),
        "influx_file": text("influx_file"),
        "influx_url": text("influx_url"),
        "influx_token": text("influx_token"),
    })
    loaded.assistant_send_enabled = False
    loaded.webhook_include_text = False
    return loaded


def save_integration_settings(settings, values):
    for key, value in values.items():
        settings.setValue(f"{GROUP}/{key}", value)
    settings.sync()


def make_hub(settings):
    return IntegrationHub(load_integration_settings(settings), APP_ID, integrations_dir(), message_text=False)


class IntegrationsDialog(QDialog):
    """Plain form; validation happens in IntegrationSettings.from_mapping."""

    def __init__(self, settings, parent=None, status_text=""):
        super().__init__(parent)
        self.setWindowTitle("Integrations")
        self.settings = settings
        current = load_integration_settings(settings)

        layout = QVBoxLayout(self)
        intro = QLabel("Share what this monitor hears with other software. Everything here is off until you tick it. "
                       "Message text is never shared: this app does not record it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        hooks = QGroupBox("Webhooks")
        form = QFormLayout(hooks)
        self.webhooks_enabled = QCheckBox("Send events to these URLs")
        self.webhooks_enabled.setChecked(current.webhooks_enabled)
        form.addRow(self.webhooks_enabled)
        self.webhook_urls = QPlainTextEdit("\n".join(current.webhook_urls))
        self.webhook_urls.setPlaceholderText("https://example.org/hooks/mesh  (one per line)")
        self.webhook_urls.setFixedHeight(64)
        form.addRow("URLs", self.webhook_urls)
        events_row = QHBoxLayout()
        self.event_boxes = {}
        labels = {"message": "Text message activity", "node": "Node heard", "telemetry": "Telemetry"}
        for event in EVENT_TYPES:
            box = QCheckBox(labels[event])
            box.setChecked(event in current.webhook_events)
            self.event_boxes[event] = box
            events_row.addWidget(box)
        form.addRow("Events", events_row)
        self.webhook_secret = QLineEdit(current.webhook_secret)
        self.webhook_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.webhook_secret.setPlaceholderText("Optional: signs each request (X-Mesh-Signature)")
        form.addRow("Secret", self.webhook_secret)
        layout.addWidget(hooks)

        api = QGroupBox("Assistant link and metrics (this Mac only, 127.0.0.1)")
        form = QFormLayout(api)
        self.local_api_enabled = QCheckBox("Turn on the local link (Claude assistant link, /metrics)")
        self.local_api_enabled.setChecked(current.local_api_enabled)
        form.addRow(self.local_api_enabled)
        self.local_api_port = QSpinBox()
        self.local_api_port.setRange(1024, 65535)
        self.local_api_port.setValue(current.local_api_port)
        form.addRow("Port", self.local_api_port)
        self.metrics_enabled = QCheckBox("Serve Prometheus /metrics and /influx")
        self.metrics_enabled.setChecked(current.metrics_enabled)
        form.addRow(self.metrics_enabled)
        layout.addWidget(api)

        influx = QGroupBox("Telemetry history (InfluxDB line protocol)")
        form = QFormLayout(influx)
        self.influx_file = QLineEdit(current.influx_file)
        self.influx_file.setPlaceholderText("telemetry.lp (saved in the integrations folder)")
        form.addRow("File", self.influx_file)
        self.influx_url = QLineEdit(current.influx_url)
        self.influx_url.setPlaceholderText("http://127.0.0.1:8086/api/v2/write?org=home&bucket=mesh&precision=s")
        form.addRow("Push URL", self.influx_url)
        self.influx_token = QLineEdit(current.influx_token)
        self.influx_token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Token", self.influx_token)
        layout.addWidget(influx)

        self.problem = QLabel("")
        self.problem.setObjectName("integrationsProblem")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)
        if status_text:
            status = QLabel(status_text)
            status.setWordWrap(True)
            layout.addWidget(status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        urls = [line.strip() for line in self.webhook_urls.toPlainText().splitlines() if line.strip()]
        return {
            "webhooks_enabled": self.webhooks_enabled.isChecked(),
            "webhook_urls": ",".join(urls),
            "webhook_events": ",".join(e for e, box in self.event_boxes.items() if box.isChecked()) or "none",
            "webhook_secret": self.webhook_secret.text().strip(),
            "local_api_enabled": self.local_api_enabled.isChecked(),
            "local_api_port": str(self.local_api_port.value()),
            "metrics_enabled": self.metrics_enabled.isChecked(),
            "influx_file": self.influx_file.text().strip(),
            "influx_url": self.influx_url.text().strip(),
            "influx_token": self.influx_token.text().strip(),
        }

    def validation_error(self):
        values = self.values()
        bad = [u for u in values["webhook_urls"].split(",") if u and not valid_webhook_url(u)]
        if bad:
            return "Not a web address: " + bad[0]
        if values["influx_url"] and not valid_webhook_url(values["influx_url"]):
            return "The InfluxDB push URL must start with http:// or https://"
        return ""

    def save(self):
        error = self.validation_error()
        if error:
            self.problem.setText(error)
            return
        save_integration_settings(self.settings, self.values())
        self.accept()
