"""Guard against a broken PySide6 build that over-releases Python's None.

PySide6 6.12.0's Linux wheels drop one reference to None on every Qt call
that returns void. On Python 3.11 that eventually aborts the interpreter
("Fatal Python error: none_dealloc") partway through the suite. This test
runs first and fails with a readable message instead. Python 3.12+ makes
None immortal, so the check only means something on older interpreters.
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import PySide6
from PySide6.QtWidgets import QApplication, QTableWidget


@unittest.skipIf(sys.version_info >= (3, 12), "None is immortal on Python 3.12+")
class BindingSanityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_void_qt_calls_do_not_release_none(self):
        table = QTableWidget(0, 1)
        before = sys.getrefcount(None)
        for _ in range(500):
            table.insertRow(0)
            table.removeRow(0)
        drift = sys.getrefcount(None) - before
        self.assertGreater(
            drift, -100,
            f"PySide6 {PySide6.__version__} released None {-drift} times in 1000 void calls; "
            "this build will crash long sessions. Pin a different PySide6 version.",
        )


if __name__ == "__main__":
    unittest.main()
