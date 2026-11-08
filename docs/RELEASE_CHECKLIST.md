# Intel Mac release verification checklist

This app monitors packet information exposed by a Meshtastic radio over Wi-Fi/TCP. It is not an SDR sniffer.

## Automated gate
- [ ] Python checks pass on the target commit
- [ ] Intel Mac x86_64 packaging workflow passes
- [ ] Download the actual .app and .dmg artifacts from the same commit
- [ ] Run demo screenshot workflow and review its generated PNG
- [ ] Confirm README matches the current UI and features

## Manual Intel Mac gate (not verified by CI)
- [ ] Launch the downloaded .app on a physical Intel Mac
- [ ] Confirm macOS unsigned-app first-open instructions work
- [ ] Run demo mode; verify packet counts, node activity, search, details, and CSV exports
- [ ] Verify clear-log, selection, copy-node and filtered CSV behavior
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
- [ ] Decide signing/notarization status and clearly disclose if unsigned
- [ ] Commit approved screenshots and embed them in README
- [ ] Publish a tagged GitHub release only after explicit approval

No automated workflow run proves real radio connectivity or physical Mac compatibility.
