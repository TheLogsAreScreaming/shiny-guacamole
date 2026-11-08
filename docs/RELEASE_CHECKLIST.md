# Intel Mac release verification checklist

This app monitors packet information exposed by a Meshtastic radio over Wi-Fi/TCP. It is not an SDR sniffer.

## Automated gate
- [ ] Python checks pass on the target commit
- [ ] Intel Mac x86_64 packaging workflow passes
- [ ] `app_version.py` and `CHANGELOG.md` are updated for this release
- [ ] Download the actual .dmg and .sha256 artifacts from the same commit; `shasum -a 256 -c` passes
- [ ] Re-capture screenshots if the UI changed (`python scripts/capture_screenshot.py`) and review them
- [ ] Confirm README matches the current UI and features

## Manual Intel Mac gate (not verified by CI)
- [ ] Launch the downloaded .app on a physical Intel Mac
- [ ] Confirm macOS unsigned-app first-open instructions work
- [ ] Drag to Applications from the DMG; the icon and About version are right
- [ ] On macOS 15+, the local network prompt appears on first connect and allowing it works
- [ ] Run demo mode; verify packet counts, node activity, search, details, and CSV exports
- [ ] Verify clear-log confirmation, selection, copy-node and filtered CSV behavior
- [ ] Open an exported CSV in Numbers or Excel; dates, negative RSSI/SNR and node IDs look right
- [ ] Try each keyboard shortcut in the README and the menu bar items
- [ ] Connect to a real Meshtastic radio over LAN TCP, typically port 4403
- [ ] Confirm live packets, metadata, disconnect, and reconnect behavior
- [ ] Interrupt Wi-Fi/network and verify whether an established TCP session detects loss
- [ ] Reconnect and quit the app without hangs or duplicate packets
- [ ] Test a long capture and the 5,000-row retention limit: `python scripts/soak_test.py --minutes 30 --rate 20` (memory flat after the cap, UI lag well under 100 ms)
- [ ] Leave the app in the background for several minutes during the soak and check for multi-second stalls (possible macOS App Nap)
- [ ] After a radio drop, use **Copy Diagnostics** and **Open Log Folder** and confirm the events are recorded

## Release gate
- [ ] Resolve critical bugs and document remaining limitations
- [ ] Verify app bundle architecture and contents
- [ ] Add the Apple secrets in [NOTARIZATION.md](NOTARIZATION.md) and confirm `status: Accepted`, or clearly disclose that the build is not notarized
- [ ] Replace or confirm the README screenshots against the release build
- [ ] Publish only after explicit approval, to `TheLogsAreScreaming/shiny-guacamole-releases` (not the source repo)

No automated workflow run proves real radio connectivity or physical Mac compatibility.
