# Architecture and operational boundaries

## Data path
Meshtastic radio (LAN Wi-Fi/TCP) → official Meshtastic Python TCPInterface → pubsub packet callback → Qt worker signal → desktop packet table and node summary.

The monitor sees only packets surfaced by the connected radio and its configured firmware, channel, and keys. It does not independently decode arbitrary LoRa RF traffic.

## Current behavior
- Radio I/O uses a QThread, separate from the GUI thread.
- Incoming packets are normalized to timestamp, source ID, port, RSSI, and SNR.
- The GUI retains at most 5,000 rows in memory; CSV export is user-initiated.
- Live packets are buffered and applied to the table every 200 ms in one batch. A full window redraw costs tens of milliseconds on macOS, so redrawing per packet capped throughput near 25 packets/s; batching keeps redraws at about 5 per second regardless of traffic.
- Per-packet work is O(1) in the size of the history: node/port tallies are updated incrementally, the filter is applied only to the new row, the node table refreshes at most every 300 ms, and auto-scroll runs at most every 150 ms.
- Connection events (connect, drop, retry, give-up, user disconnect) are kept in memory for **Copy Diagnostics** and written to a rotating log (512 KB × 4 files) at `~/Library/Logs/Meshtastic Sniffer TCP/app.log`, along with Meshtastic library warnings. The log does not contain packet contents.
- Demo mode generates synthetic traffic and must not be mistaken for live RF evidence.
- Initial connection failures and dropped sessions both get up to 5 automatic reconnect attempts (2s, 4s, 8s, 16s, 30s); a successful connection resets the count and **Disconnect** cancels any pending attempt.
- A dropped session is detected three ways: the library's `meshtastic.connection.lost` event (only for the interface the worker owns), the library's reader thread exiting, and OS-level TCP keepalive (probes after ~15s idle, gives up after ~35s) so a radio that loses power or Wi-Fi does not leave the socket blocked indefinitely.
- A reconnect is scheduled only after the previous worker has finished, unsubscribed its pubsub listeners, and closed its interface, so retries never stack duplicate packet callbacks. Packets from a stale interface are ignored.
- The packet log is kept across reconnects.

## Known limitations and risks
- Dropped-session detection and reconnect are covered by fake-interface regression tests only; they have not yet been verified against a physical radio losing power or Wi-Fi.
- The Meshtastic library can also reconnect a closed socket internally; when that succeeds the session simply continues without a status change.
- Radio firmware/library versions may expose different metadata.
- PySide6 6.12.0 is excluded: its Linux wheels release one reference to `None` per void Qt call, which aborts Python 3.11 after a few thousand calls. `tests/test_binding_sanity.py` fails fast if a future release repeats this. The macOS 6.12.0 wheel did not show the bug when measured, but the exclusion keeps CI and the Intel build on the same Qt line.
- Node identifiers and packet metadata can be sensitive; exports should be handled responsibly.
- GitHub Actions packaging and headless UI tests are not substitutes for Intel Mac and live-radio tests.

## Next reliability work
Test network loss and recovery on a physical radio (power off, Wi-Fi off, router restart) and record time-to-detect before advertising automatic recovery as verified.
