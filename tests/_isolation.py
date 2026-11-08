"""Shared test setup: offscreen Qt and throwaway preferences.

Without this, every MainWindow a test closes would save its radio address
into the real app's macOS preferences.
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "MESHTASTIC_SNIFFER_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="sniffer-tests-"), "settings.ini"),
)
