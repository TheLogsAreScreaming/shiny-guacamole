# Meshtastic Sniffer TCP

**A receive-focused Meshtastic packet monitor for Intel Macs.** Connect to a Meshtastic radio over your local network (Wi-Fi/TCP), inspect packets and node activity, and export logs. Independent community software; not affiliated with the Meshtastic project.

> **Scope:** This is not an SDR or over-the-air LoRa sniffer. It can only display packet information exposed by the connected Meshtastic radio. Encrypted content is not automatically decrypted.

## Features

- Live packet table with timestamp, source node, message type, RSSI and SNR (when reported)
- Search/filter packets by node ID or message type
- Packet totals, unique-node count and message-type statistics
- Clear Log and CSV export with error handling
- Demo mode for exploring the interface without radio hardware
- Remembered radio IP/hostname between app launches
- Detect dropped TCP sessions and reconnect automatically with bounded backoff (up to 5 attempts); manual disconnect cancels retries
- Session health line (uptime, packets this session, time since last packet, reconnects, last drop)
- **Copy Diagnostics** for bug reports and a rotating log file (`~/Library/Logs/Meshtastic Sniffer TCP/app.log`, opened with **Open Log Folder**)
- Dark, high-contrast desktop styling and read-only packet table

**Known limitation:** Dropped-session detection and reconnect are tested with simulated interfaces only. Real-radio and Intel Mac hands-on verification are still required.

## Screenshots

The [screenshot workflow](.github/workflows/screenshots.yml) can generate a screenshot of the real app running in demo mode on GitHub Actions. Open **Actions → UI screenshots → Run workflow**, then download the `dashboard-screenshots` artifact. Once reviewed, screenshots can be committed under `docs/screenshots/` and embedded here. No screenshot is presented as verified until that step is complete.

## Download and run on an Intel Mac

1. Open [GitHub Actions](../../actions/workflows/intel-mac.yml) and select a successful **Intel Mac app build**.
2. Download the **Meshtastic-Sniffer-TCP-Intel** artifact and extract it.
3. Open the DMG and launch **Meshtastic Sniffer TCP.app**. The build is currently unsigned, so macOS may require **Control-click → Open** and approval in **System Settings → Privacy & Security**.
4. Enter the radio's reachable LAN IP address and select **Connect TCP**, or select **Demo Mode** to inspect simulated traffic.

GitHub Actions packaging success is not proof that the app connects to a physical radio. No official release is published by these workflows.

## Connect your radio

The Mac and Meshtastic radio must be able to reach each other on the network. Meshtastic TCP normally uses port **4403**. Check the address and network if connection fails. See [TCP troubleshooting](docs/CONNECTION.md).

## Run from source

Python 3.11+ is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

## Testing

GitHub Actions runs Python syntax and smoke tests and packages an Intel Mac app. To run tests locally with the required dependencies installed:

```bash
python -m unittest discover -s tests -v
```

## Privacy and safety

The app does not intentionally transmit messages. Packet logs and CSV exports may contain node identifiers or other sensitive metadata: handle and share them responsibly. Only monitor radios and networks you are authorized to access.

## Project status

**Active development / pre-release.** Planned work includes reliable detection of dropped established TCP sessions, richer node activity views, verified screenshot embeds, and physical Intel Mac/radio testing.
