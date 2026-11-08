import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import unittest
from unittest.mock import patch
import sys
import types

# packet_row is pure Python, but app imports Qt; test is run only when PySide6 is installed.
from app import packet_row

class PacketRowTests(unittest.TestCase):
    def test_missing_fields(self):
        row = packet_row({})
        self.assertEqual(row["rssi"], "")
        self.assertEqual(row["type"], "")

    def test_zero_values_not_dropped(self):
        row = packet_row({"fromId": "!abc", "rxRssi": 0, "rxSnr": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP"}})
        self.assertEqual(row["rssi"], "0")
        self.assertEqual(row["snr"], "0")
        self.assertEqual(row["type"], "TEXT_MESSAGE_APP")

if __name__ == "__main__":
    unittest.main()
