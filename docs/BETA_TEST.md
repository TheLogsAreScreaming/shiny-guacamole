# Beta test sheet (0.1.0)

Needs a physical Intel Mac and a Meshtastic radio on the same network. Automated builds cannot cover any of this. Write the result next to each line and paste **Copy Diagnostics** output for any failure.

## Install
- [ ] DMG checksum matches: `shasum -a 256 -c Meshtastic-Sniffer-TCP-0.1.0-Intel.dmg.sha256`
- [ ] Drag to Applications; first open works with Control-click → Open
- [ ] Icon is right; Help → About shows 0.1.0

## Without a radio
- [ ] Demo Mode: packets, nodes and stats update
- [ ] ⌘F filter shows "Showing N of M"; double-clicking a node filters by it
- [ ] Export CSV and Export Filtered; open both in Numbers or Excel
- [ ] Clear Log asks first; every shortcut in the README works

## With a radio
Enter the radio's IP again if the field shows `192.0.2.10` (a test address that older test runs saved).

- [ ] First connect shows the macOS Local Network prompt (macOS 15+); after allowing, status turns green
- [ ] Live packets appear with plausible RSSI and SNR; no duplicates
- [ ] Disconnect is clean; Connect works again

| Drop test | Time until app reports loss | Reconnected by itself? |
| --- | --- | --- |
| Radio powered off, then on | | |
| Radio's Wi-Fi off, then on | | |
| Router restarted | | |
| Radio off for longer than ~60 s (retries should give up) | | |

After each: no duplicate packets, packet log kept, diagnostics list the events. Expected loss detection is within about 35 s.

## Long run
- [ ] 30 minutes on live traffic with the app in the background: no multi-second stalls, memory levels off
- [ ] Quit while connected: exits within 5 s, no crash

Passing all of this is the gate for publishing a beta to `shiny-guacamole-releases`.
