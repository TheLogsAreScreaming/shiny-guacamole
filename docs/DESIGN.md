# Dashboard design

The app uses a high-contrast dark field-instrument theme from `dashboard.qss`, applied at launch by `app.py` (and by the screenshot and soak scripts). Check contrast and font rendering on an Intel Mac before release.

## Layout

1. **Menu bar:** File (exports, quit), Edit (find, copy, clear), Radio (connect, disconnect, demo), View (pause auto-scroll), Help (log folder).
2. **Connection row:** radio address, Connect TCP, Disconnect, Demo Mode / Stop Demo.
3. **Connection status:** one line with a coloured left edge, styled through the label's `state` property:

   | state | meaning | edge colour |
   | --- | --- | --- |
   | `offline` | not connected | grey |
   | `connecting` | connecting, reconnecting, disconnecting | blue |
   | `connected` | live session | green |
   | `warning` | dropped; waiting to retry | amber |
   | `error` | failed, or gave up retrying | red |
   | `demo` | simulated packets | violet |

   The text always carries the meaning too (●, ◌, ✕ plus words), so colour is never the only signal.
4. **Stats and session health lines.** When a filter is active the stats line adds "Showing N of M".
5. **Log row:** filter field, Pause Auto-scroll, Clear Log, Export CSV, Export Filtered.
6. **Packet table and node list** in a splitter. Double-click a node to filter by it.
7. **Packet inspector** for the selected row.
8. **Tools row and status bar.** Copy and export confirmations appear in the status bar for a few seconds and never replace the connection status.

Buttons are disabled when they cannot do anything: Connect while connected, retrying or in demo; Disconnect when idle; exports and Clear Log with an empty log; Export Filtered without a filter.
