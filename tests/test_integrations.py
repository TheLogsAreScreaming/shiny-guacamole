"""Integrations (webhooks, localhost API, metrics, InfluxDB) without a radio."""
import http.client
import json
import pathlib
import tempfile
import unittest

import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import gc
import mesh_integrations as mi

TEXT = {"from": 0x1234ABCD, "to": 0xFFFFFFFF, "channel": 0, "rxSnr": 6.5, "rxRssi": -88, "rxTime": 1700000000,
        "hopStart": 3, "hopLimit": 1, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello mesh"}}
INFO = {"from": 0x1234ABCD, "rxTime": 1700000001,
        "decoded": {"portnum": "NODEINFO_APP", "user": {"longName": "Ridge Top", "shortName": "RT", "hwModel": "RAK4631"}}}
TELEM = {"from": 0x1234ABCD, "rxTime": 1700000002, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {
    "deviceMetrics": {"batteryLevel": 87, "voltage": 4.01, "channelUtilization": 12.5},
    "environmentMetrics": {"temperature": 21.5, "relativeHumidity": 40.0, "barometricPressure": 1013.2}}}}
POSITION = {"from": 0x1234ABCD, "rxTime": 1700000003,
            "decoded": {"portnum": "POSITION_APP", "position": {"latitudeI": 413000000, "longitudeI": -730000000}}}



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

class FakeDeliverer(mi.Deliverer):
    def __init__(self):
        super().__init__()
        self.sent = []

    def submit(self, url, body, headers):
        self.sent.append((url, body, dict(headers)))


def make_hub(**settings):
    hub = mi.IntegrationHub(mi.IntegrationSettings(**settings), "test-app", tempfile.mkdtemp(), FakeDeliverer())
    return hub


class SettingsTests(unittest.TestCase):
    def test_missing_section_means_everything_off(self):
        s = mi.IntegrationSettings.from_mapping({})
        self.assertFalse(s.webhooks_enabled)
        self.assertFalse(s.local_api_enabled)
        self.assertFalse(s.assistant_send_enabled)
        self.assertFalse(s.any_enabled)
        self.assertEqual(s.local_api_port, mi.DEFAULT_PORT)

    def test_parses_config_values_and_drops_bad_urls(self):
        s = mi.IntegrationSettings.from_mapping({
            "webhooks_enabled": "True", "webhook_urls": "https://a.example/hook, ftp://nope, not a url,http://b:8080/x",
            "webhook_events": "message, telemetry, bogus", "local_api_port": "80"})
        self.assertTrue(s.webhooks_enabled)
        self.assertEqual(s.webhook_urls, ["https://a.example/hook", "http://b:8080/x"])
        self.assertEqual(s.webhook_events, {"message", "telemetry"})
        self.assertEqual(s.local_api_port, mi.DEFAULT_PORT)  # privileged ports refused

    def test_hub_from_configparser(self):
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read_string("[integrations]\nlocal_api_enabled = yes\nlocal_api_port = 9500\n")
        hub = mi.hub_from_config(cfg, "x", tempfile.mkdtemp())
        self.assertTrue(hub.settings.local_api_enabled)
        self.assertEqual(hub.settings.local_api_port, 9500)
        self.assertFalse(mi.hub_from_config(configparser.ConfigParser(), "x", ".").settings.any_enabled)


class NormaliseTests(unittest.TestCase):
    def test_text_message(self):
        events = dict(mi.normalize_packet(TEXT))
        self.assertEqual(events["message"]["text"], "hello mesh")
        self.assertEqual(events["message"]["node_id"], "!1234abcd")
        self.assertFalse(events["message"]["direct"])
        self.assertEqual(events["node"]["hops_away"], 2)

    def test_telemetry_fields(self):
        events = dict(mi.normalize_packet(TELEM))
        self.assertEqual(events["telemetry"]["metrics"]["battery_percent"], 87)
        self.assertEqual(events["telemetry"]["metrics"]["temperature_celsius"], 21.5)

    def test_garbage_is_ignored(self):
        self.assertEqual(mi.normalize_packet("nope"), [])
        hub = make_hub()
        self.assertEqual(hub.observe({"decoded": "broken"})[0][0], "node")
        self.assertEqual(hub.observe(None), [])


class WebhookTests(PausedGc, unittest.TestCase):
    def test_off_by_default_sends_nothing(self):
        hub = make_hub(webhook_urls=["http://127.0.0.1:1/x"])
        hub.observe(TEXT)
        self.assertEqual(hub.deliverer.sent, [])

    def test_selected_events_only_and_signed(self):
        hub = make_hub(webhooks_enabled=True, webhook_urls=["http://h/1"], webhook_events={"message"},
                       webhook_secret="s3cret")
        for packet in (INFO, TEXT, TELEM):
            hub.observe(packet)
        self.assertEqual(len(hub.deliverer.sent), 1)
        url, body, headers = hub.deliverer.sent[0]
        doc = json.loads(body)
        self.assertEqual(doc["event"], "message")
        self.assertEqual(doc["data"]["text"], "hello mesh")
        self.assertEqual(headers["X-Mesh-Signature"], mi.sign("s3cret", body))

    def test_text_can_be_left_out(self):
        hub = make_hub(webhooks_enabled=True, webhook_urls=["http://h/1"], webhook_events={"message"},
                       webhook_include_text=False)
        hub.observe(TEXT)
        self.assertNotIn("text", json.loads(hub.deliverer.sent[0][1])["data"])

    def test_deliverer_retries_then_counts_failure(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(url)
            raise OSError("down")
        d = mi.Deliverer(post=post, retries=1, retry_delay=0)
        self.assertFalse(d.deliver_now("http://h/x", b"{}", {}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(d.stats["failed"], 1)
        self.assertIn("OSError", d.stats["last_error"])

    def test_deliverer_queue_is_bounded(self):
        d = mi.Deliverer(post=lambda *a: 200, max_queue=1)
        d._ensure_started = lambda: None  # keep the worker off so the queue fills
        d.submit("http://h", b"1", {})
        d.submit("http://h", b"2", {})
        self.assertEqual(d.stats["dropped"], 1)

    def test_background_delivery(self):
        got = []
        d = mi.Deliverer(post=lambda url, body, headers, timeout: got.append(body) or 204)
        d.submit("http://h", b"x", {})
        d.flush()
        d.stop()
        self.assertEqual(got, [b"x"])
        self.assertEqual(d.stats["sent"], 1)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.hub = make_hub()
        for packet in (INFO, TEXT, TELEM, POSITION):
            self.hub.observe(packet)

    def test_state(self):
        node = self.hub.state.list_nodes()[0]
        self.assertEqual(node["long_name"], "Ridge Top")
        self.assertAlmostEqual(node["latitude"], 41.3)
        self.assertEqual(self.hub.state.find_node("rt"), "!1234abcd")
        self.assertEqual(self.hub.state.recent_messages()[0]["from_name"], "Ridge Top")
        self.assertEqual(self.hub.state.status()["packets_total"], 4)

    def test_prometheus(self):
        text = mi.render_prometheus(self.hub.state)
        self.assertIn('mesh_node_battery_percent{node="!1234abcd",name="Ridge Top"} 87', text)
        self.assertIn('mesh_packets_total{portnum="TEXT_MESSAGE_APP"} 1', text)
        self.assertIn("mesh_radio_connected 0", text)

    def test_prometheus_label_escaping(self):
        self.assertEqual(mi._prom_label('a"b\\c\n'), 'a\\"b\\\\c\\n')

    def test_influx_line_protocol(self):
        line = mi.influx_lines("!1234abcd", {"battery_percent": 87, "voltage_volts": 4.01}, 1700000002, "Ridge Top")
        self.assertEqual(line, "mesh_telemetry,node=!1234abcd,name=Ridge\\ Top battery_percent=87.0,voltage_volts=4.01 1700000002")
        self.assertEqual(mi.influx_lines("!x", {}, 1), "")
        self.assertIn("battery_percent=87.0", mi.export_influx(self.hub.state))

    def test_influx_file(self):
        hub = make_hub(influx_file="telemetry.lp")
        hub.observe(INFO)
        hub.observe(TELEM)
        written = (hub.data_dir / "telemetry.lp").read_text()
        self.assertTrue(written.startswith("mesh_telemetry,node=!1234abcd,name=Ridge\\ Top "))

    def test_influx_push_uses_token(self):
        hub = make_hub(influx_url="http://127.0.0.1:8086/api/v2/write?org=o&bucket=b&precision=s", influx_token="tok")
        hub.observe(TELEM)
        url, body, headers = hub.deliverer.sent[0]
        self.assertEqual(headers["Authorization"], "Token tok")
        self.assertTrue(body.startswith(b"mesh_telemetry,node=!1234abcd"))


class MessageTextTests(PausedGc, unittest.TestCase):
    def test_monitor_hub_drops_text_everywhere(self):
        hub = mi.IntegrationHub(mi.IntegrationSettings(webhooks_enabled=True, webhook_urls=["http://h/1"]),
                                "x", tempfile.mkdtemp(), FakeDeliverer(), message_text=False)
        hub.observe(TEXT)
        self.assertNotIn("text", hub.state.recent_messages()[0])
        self.assertNotIn(b"hello mesh", b"".join(body for _, body, _ in hub.deliverer.sent))

    def test_port_file_written_and_removed(self):
        hub = make_hub(local_api_enabled=True, local_api_port=0)
        hub.settings.local_api_port = 0
        hub.start()
        try:
            port = json.loads(mi.port_file(hub.data_dir).read_text())["port"]
            self.assertEqual(port, hub.server.port)
            self.assertEqual(mi.port_file(hub.data_dir).stat().st_mode & 0o777, 0o600)
        finally:
            hub.stop()
        self.assertFalse(mi.port_file(hub.data_dir).exists())


class SendTests(unittest.TestCase):
    def test_send_off_by_default(self):
        hub = make_hub()
        hub.send_text = lambda *a: None
        with self.assertRaises(PermissionError):
            hub.send_message("hi")

    def test_send_validation_and_dm_lookup(self):
        hub = make_hub(assistant_send_enabled=True)
        sent = []
        hub.send_text = lambda text, ch, dest: sent.append((text, ch, dest))
        hub.observe(INFO)
        with self.assertRaises(ValueError):
            hub.send_message("   ")
        with self.assertRaises(ValueError):
            hub.send_message("x" * 201)
        with self.assertRaises(ValueError):
            hub.send_message("hi", destination="nobody")
        self.assertTrue(hub.send_message("hi", 1, "Ridge Top")["sent"])
        self.assertEqual(sent, [("hi", 1, "!1234abcd")])
        self.assertEqual(hub.state.status()["messages_sent_by_assistant"], 1)


class LocalApiTests(PausedGc, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.hub = make_hub(local_api_enabled=True, local_api_port=0)
        self.hub.observe(INFO)
        self.hub.observe(TEXT)
        self.hub.observe(TELEM)
        self.server = mi.LocalApiServer(self.hub, 0, self.hub.data_dir)
        self.server.start()
        self.token = self.server.token

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def request(self, method, path, body=None, token=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {self.token}"
        if host:
            headers["Host"] = host
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.httpd.server_address[0], "127.0.0.1")

    def test_token_file_is_private(self):
        mode = mi.token_path(self.hub.data_dir).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(mi.load_or_create_token(self.hub.data_dir), self.token)

    def test_metrics_need_no_token(self):
        status, data = self.request("GET", "/metrics", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"mesh_node_battery_percent", data)
        self.assertEqual(self.request("GET", "/influx", token=False)[0], 200)

    def test_api_requires_token(self):
        self.assertEqual(self.request("GET", "/api/nodes", token=False)[0], 401)
        status, data = self.request("GET", "/api/nodes")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["nodes"][0]["short_name"], "RT")

    def test_rebinding_host_refused(self):
        self.assertEqual(self.request("GET", "/metrics", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/status", host="evil.example:9478")[0], 403)
        self.assertEqual(self.request("GET", "/healthz", host="localhost:9478")[0], 200)

    def test_messages_telemetry_status(self):
        msgs = json.loads(self.request("GET", "/api/messages?limit=5")[1])["messages"]
        self.assertEqual(msgs[0]["text"], "hello mesh")
        tel = json.loads(self.request("GET", "/api/telemetry?node=RT")[1])
        self.assertEqual(tel["points"][0]["battery_percent"], 87)
        self.assertEqual(self.request("GET", "/api/telemetry?node=zzz")[0], 404)
        status = json.loads(self.request("GET", "/api/status")[1])
        self.assertFalse(status["integrations"]["assistant_send_enabled"])

    def test_send_refused_when_off(self):
        self.assertEqual(self.request("POST", "/api/send", {"text": "hi"})[0], 403)

    def test_send_when_enabled(self):
        self.hub.settings.assistant_send_enabled = True
        sent = []
        self.hub.send_text = lambda text, ch, dest: sent.append(text)
        self.assertEqual(self.request("POST", "/api/send", {"text": "hi", "channel": 0})[0], 200)
        self.assertEqual(self.request("POST", "/api/send", {"text": ""})[0], 400)
        self.assertEqual(self.request("POST", "/api/send", {"text": "hi"}, token=False)[0], 401)
        self.assertEqual(sent, ["hi"])


class GrafanaDashboardTests(unittest.TestCase):
    def test_dashboard_queries_exported_metrics(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "docs" / "grafana" / "mesh-telemetry-dashboard.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        exprs = " ".join(t.get("expr", "") for p in doc["panels"] for t in p.get("targets", []))
        for metric in ("mesh_node_battery_percent", "mesh_node_temperature_celsius", "mesh_nodes_heard_last_hour"):
            self.assertIn(metric, exprs)


if __name__ == "__main__":
    unittest.main()
