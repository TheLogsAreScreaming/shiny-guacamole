import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import unittest
from app import packet_row

class SmokeTests(unittest.TestCase):
    def test_packet_row_shape(self):
        row = packet_row({"fromId": "!test", "decoded": {"portnum": "NODEINFO_APP"}})
        self.assertEqual(set(row), {"date", "time", "from", "type", "rssi", "snr"})
        self.assertEqual(row["from"], "!test")

if __name__ == "__main__":
    unittest.main()
