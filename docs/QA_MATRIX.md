# Desktop QA matrix

The GitHub Actions Python checks and Intel packaging workflow do **not** verify real radio connectivity or launch behavior on the owner's Mac.

| Feature | Automated verification | Manual verification still needed |
| --- | --- | --- |
| Packet normalization | Unit tests | Different real Meshtastic firmware packet shapes |
| 5,000-row packet window | Headless tests: tallies vs full recount after rollover, per-packet cost at cap, batching | `scripts/soak_test.py` on the Intel Mac (CPU, memory, UI lag) |
| Diagnostics and log file | Headless tests: session summary, event list, log rotation | Copy Diagnostics paste, Open Log Folder in Finder |
| Search and node filter | Headless UI regression tests | Keyboard and mouse behavior on Intel Mac |
| Copy Node ID | Headless clipboard test | macOS clipboard integration |
| CSV exports | Headless tests: full and filtered contents, formula escaping, atomic write (no partial file, previous file kept), unwritable folder, remembered folder | Save dialog on macOS, opening the CSV in Numbers/Excel |
| Demo mode | Offscreen screenshots in `docs/screenshots/` | Visual check on a real Intel Mac display |
| Buttons, menus, status colours | Headless tests: enable/disable through connect, retry, disconnect and demo; clear-log confirmation; pause menu sync | Keyboard shortcuts and menu bar on macOS |
| TCP connection | Fake-interface tests: loss event, dead reader, keepalive, backoff, give-up, no duplicate subscriptions | Live Wi-Fi/TCP radio, loss/recovery timing, app shutdown |
| Intel Mac packaging | `scripts/build_mac.sh` in Actions: x86_64 binary, plist version, local-network text, signature, bundled self-check (imports Meshtastic, loads theme, builds window offscreen), DMG verify | Download and launch on physical Intel Mac; Gatekeeper and local network prompts |

## Before beta
1. Run the automated test workflow and verify the target commit.
2. Re-run `scripts/capture_screenshot.py` if the UI changed and review the PNGs.
3. Validate the app and TCP radio behavior on actual hardware.
4. Fix failures and record any remaining limitations.
5. Publish a release only with explicit approval.
