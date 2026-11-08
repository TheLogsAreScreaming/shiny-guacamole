"""Regression tests for Meshtastic packet metadata normalization."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import unittest

from app import packet_row


class PacketRowEdgeCases(unittest.TestCase):
    def test_missing_fields(self):
        result = packet_row({})
        self.assertEqual(result["from"], "")
        self.assertEqual(result["type"], "")
        self.assertEqual(result["rssi"], "")
        self.assertEqual(result["snr"], "")

    def test_zero_signal_values_preserved(self):
        result = packet_row({"fromId": "!12345678", "rxRssi": 0, "rxSnr": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP"}})
        self.assertEqual(result["from"], "!12345678")
        self.assertEqual(result["rssi"], "0")
        self.assertEqual(result["snr"], "0")
        self.assertEqual(result["type"], "TEXT_MESSAGE_APP")

    def test_fallback_numeric_source(self):
        result = packet_row({"from": 123, "decoded": {}})
        self.assertEqual(result["from"], "123")

    def test_empty_decoded(self):
        result = packet_row({"decoded": None})
        self.assertEqual(result["type"], "")


if __name__ == "__main__":
    unittest.main()
