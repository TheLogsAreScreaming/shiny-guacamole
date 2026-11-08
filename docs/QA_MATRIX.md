# Desktop QA matrix

The GitHub Actions Python checks and Intel packaging workflow do **not** verify real radio connectivity or launch behavior on the owner's Mac.

| Feature | Automated verification | Manual verification still needed |
| --- | --- | --- |
| Packet normalization | Unit tests | Different real Meshtastic firmware packet shapes |
| 5,000-row packet window | Headless tests: tallies vs full recount after rollover, per-packet cost at cap, batching | `scripts/soak_test.py` on the Intel Mac (CPU, memory, UI lag) |
| Diagnostics and log file | Headless tests: session summary, event list, log rotation | Copy Diagnostics paste, Open Log Folder in Finder |
| Search and node filter | Headless UI regression tests | Keyboard and mouse behavior on Intel Mac |
| Copy Node ID | Headless clipboard test | macOS clipboard integration |
| CSV exports | Syntax/GUI smoke checks | Export paths, permissions, filtered contents |
| Demo mode | Screenshot workflow available | Visual review and screenshot approval |
| TCP connection | Fake-interface tests: loss event, dead reader, keepalive, backoff, give-up, no duplicate subscriptions | Live Wi-Fi/TCP radio, loss/recovery timing, app shutdown |
| Intel Mac packaging | GitHub Actions artifact | Download and launch on physical Intel Mac |

## Before beta
1. Run the automated test workflow and verify the target commit.
2. Run the screenshot workflow; review the generated PNG.
3. Validate the app and TCP radio behavior on actual hardware.
4. Fix failures and record any remaining limitations.
5. Publish a release only with explicit approval.
