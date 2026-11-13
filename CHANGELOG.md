# Changelog

## 0.1.0 (unreleased)

First planned beta. Not yet tested against a physical radio.

- Live packet table, filtering, node activity, packet inspector, and demo mode
- Dropped-session detection and automatic reconnect with bounded backoff
- Session health line, Copy Diagnostics, and a rotating log file
- Batched table updates that stay responsive with a full 5,000-row history
- CSV export of the full or filtered log, with safe cells and atomic writes
- Menu bar, keyboard shortcuts, and colour-coded connection status
- Versioned Intel Mac app bundle with icon, DMG, and SHA-256 checksum
- Optional integrations, off by default: webhooks, read-only assistant link (MCP), Prometheus/InfluxDB export and a sample Grafana dashboard
