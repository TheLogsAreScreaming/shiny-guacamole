# Integrations: webhooks, assistant link and sensor history

Meshtastic Sniffer TCP can share what it hears from your radio with other software.
**Everything is off until you turn it on.** The app is a monitor, so two limits apply
everywhere:

* it **never decodes message text**: message events say who sent a text, when
  and on which channel, but not what it said;
* it **never transmits**: the assistant link has no send tool.

Telemetry readings (battery, voltage, temperature, humidity, pressure, channel
use) and public node names are decoded so they can be exported.

## Settings

Choose **Radio → Integrations…**. The dialog has three sections (webhooks,
assistant link and metrics, telemetry history); **Save** applies the changes
straight away. Settings are stored with the app's other preferences. Files the
integrations create (access token, port file, line-protocol file) live in
`~/Library/Application Support/Meshtastic Sniffer TCP/integrations/`.

## 1. Webhooks

Tick **Send events to these URLs**, enter one `http://` or `https://` URL per
line, choose the events and optionally a secret.

Each event is one `POST` (`Content-Type: application/json`):

```json
{"app": "meshtastic-sniffer-tcp", "event": "telemetry",
 "data": {"node_id": "!1234abcd", "channel": 0, "time": 1700000000,
          "metrics": {"battery_percent": 87, "voltage_volts": 4.01, "temperature_celsius": 21.5}}}
```

* `node`: every packet heard (node id, name when known, SNR, RSSI, hops, packet type).
* `message`: a text message was heard (sender, recipient, direct or broadcast, channel). No text.
* `telemetry`: decoded readings in `data.metrics`.
* With a `secret`, every request carries `X-Mesh-Signature: sha256=<hex>`
  (HMAC-SHA256 of the raw body). `X-Mesh-Event` names the event.

Delivery happens in the background with a 5 second timeout and one retry; at
most 500 deliveries wait in the queue, extra ones are dropped and counted.

## 2. Assistant link (MCP)

1. In **Radio → Integrations…** tick **Turn on the local link** (port 9481 by
   default) and Save. The app starts a small server on `127.0.0.1:9481` (never
   on your network) and writes a random access token (`assistant-token`) and
   the port (`local-api.json`) into its integrations folder, readable only by you.

2. Add the server to Claude Desktop (**Settings → Developer → Edit Config**):

   ```json
   {
     "mcpServers": {
       "meshtastic-sniffer": {
         "command": "/Applications/Meshtastic Sniffer TCP.app/Contents/MacOS/Meshtastic Sniffer TCP",
         "args": ["--assistant-mcp"]
       }
     }
   }
   ```

   `--assistant-mcp` runs the assistant link without opening a window. From a
   source checkout use `"command": "python3", "args": ["/path/to/shiny-guacamole/app.py", "--assistant-mcp"]`.
   For Claude Code:
   `claude mcp add meshtastic-sniffer -- "/Applications/Meshtastic Sniffer TCP.app/Contents/MacOS/Meshtastic Sniffer TCP" --assistant-mcp`.

3. Restart Claude Desktop, keep the app connected to your radio, and ask
   e.g. *"Which nodes reported low battery today?"*

| Tool | What it returns |
| --- | --- |
| `get_network_status` | connection state, nodes known / heard in the last hour, packets by type, integration status |
| `list_nodes` | nodes heard, most recent first, with names, SNR/RSSI, hops and latest telemetry |
| `recent_messages` | text-message activity (sender, time, channel); never the text |
| `node_telemetry` | telemetry history for one node (id, short or long name) |

All tools are read-only. There is deliberately no send tool.

The app keeps the last 200 message events and 500 telemetry points per node
in memory; they are cleared when the app quits.

## 3. Sensor history: Prometheus, InfluxDB, Grafana

With the local API on (and `"metrics": true`):

* `http://127.0.0.1:9481/metrics`: Prometheus text format: `mesh_radio_connected`,
  `mesh_nodes_known`, `mesh_nodes_heard_last_hour`, `mesh_packets_total{portnum}`,
  and per node (`node`, `name` labels) `mesh_node_last_heard_timestamp_seconds`,
  `mesh_node_snr`, `mesh_node_rssi`, `mesh_node_battery_percent`,
  `mesh_node_voltage_volts`, `mesh_node_temperature_celsius`,
  `mesh_node_relative_humidity_percent`, `mesh_node_barometric_pressure_hpa`,
  `mesh_node_channel_utilization_percent` and more.
* `http://127.0.0.1:9481/influx`: everything in memory as InfluxDB line protocol.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: meshtastic-sniffer
    scrape_interval: 60s
    static_configs:
      - targets: ["127.0.0.1:9481"]
```

InfluxDB without Prometheus:

* **File** `telemetry.lp` appends each telemetry packet to that file in the
  integrations folder (`influx write --precision s --file telemetry.lp`).
* **Push URL** + **Token**, e.g. `http://127.0.0.1:8086/api/v2/write?org=home&bucket=mesh&precision=s`,
  pushes each line to InfluxDB v2.

Line format: `mesh_telemetry,node=!1234abcd,name=Ridge\ Top battery_percent=87.0,voltage_volts=4.01 1700000000`.

**Grafana:** import [`grafana/mesh-telemetry-dashboard.json`](grafana/mesh-telemetry-dashboard.json)
(**Dashboards → New → Import**) and pick your Prometheus data source.

## Security notes

* The server listens on `127.0.0.1` only and refuses requests whose `Host`
  header is not a loopback name (protects against DNS rebinding from web pages).
* `/metrics`, `/influx` and `/healthz` need no token so local Prometheus can
  scrape them; `/api/*` needs the token. Delete `assistant-token` and turn the
  link off and on to rotate it.
* Webhook bodies contain node ids and names; use `https://` for anything off
  this Mac.
