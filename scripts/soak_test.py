"""Long-running stability check: feed synthetic packets and report CPU, memory and UI lag.

Run on the target Intel Mac from the repo root, for example:

    python scripts/soak_test.py --minutes 30 --rate 20
    python scripts/soak_test.py --minutes 2 --rate 50 --offscreen

The window stays visible unless --offscreen is given. Every --report seconds a
line is printed with rows held, peak memory (RSS), CPU used and the worst
event-loop delay seen in that interval. Memory should level off once the
5,000-row history is full; a steady climb after that is a leak.
"""
import argparse
import os
import random
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def peak_rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--rate", type=float, default=20, help="packets per second")
    parser.add_argument("--nodes", type=int, default=80)
    parser.add_argument("--report", type=float, default=30, help="seconds between report lines")
    parser.add_argument("--offscreen", action="store_true")
    args = parser.parse_args()
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from app import MainWindow

    app = QApplication(sys.argv)
    theme = ROOT / "dashboard.qss"
    if theme.is_file():
        app.setStyleSheet(theme.read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()

    rng = random.Random(1)
    nodes = [f"!{rng.getrandbits(32):08x}" for _ in range(args.nodes)]
    ports = ["TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP", "ROUTING_APP"]
    interval_ms = max(1, int(1000 / args.rate))
    start = time.monotonic()
    cpu_start = time.process_time()
    state = {"sent": 0, "last_tick": time.monotonic(), "worst_lag": 0.0, "next_report": start + args.report}

    def feed():
        now = time.monotonic()
        lag = (now - state["last_tick"]) * 1000 - interval_ms
        state["worst_lag"] = max(state["worst_lag"], lag)
        state["last_tick"] = now
        window.queue_packet({  # same path as live radio packets
            "time": time.strftime("%H:%M:%S"),
            "from": rng.choice(nodes),
            "type": rng.choice(ports),
            "rssi": str(rng.randint(-125, -40)),
            "snr": f"{rng.uniform(-18, 12):.1f}",
        })
        state["sent"] += 1
        if now >= state["next_report"]:
            elapsed = now - start
            cpu = time.process_time() - cpu_start
            print(
                f"{elapsed / 60:6.1f} min | sent {state['sent']:7d} | rows {len(window.rows):5d} | "
                f"peak RSS {peak_rss_mb():7.1f} MB | CPU {cpu / elapsed * 100:5.1f}% | "
                f"worst UI lag {state['worst_lag']:6.1f} ms",
                flush=True,
            )
            state["worst_lag"] = 0.0
            state["next_report"] = now + args.report
        if now - start >= args.minutes * 60:
            timer.stop()
            window.close()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(interval_ms)
    print(f"Soak test: {args.minutes} min at {args.rate} packets/s, {args.nodes} nodes", flush=True)
    app.exec()
    print("Done. Expect rows capped at 5000, RSS flat after the cap, UI lag well under 100 ms.")


if __name__ == "__main__":
    main()
