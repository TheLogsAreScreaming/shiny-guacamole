"""Version and packaged-resource checks that do not need a build."""
import _isolation  # noqa: F401  (offscreen Qt + throwaway settings; keep first)
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app import MainWindow, __version__, load_theme, resource_path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_version_suits_a_mac_bundle(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_changelog_mentions_the_version(self):
        self.assertTrue(re.search(rf"^## {re.escape(__version__)}\b", (ROOT / "CHANGELOG.md").read_text(), re.M))

    def test_resource_path_from_source(self):
        self.assertTrue(resource_path("dashboard.qss").is_file())

    def test_resource_path_inside_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertEqual(resource_path("dashboard.qss"), Path(tmp) / "dashboard.qss")

    def test_missing_theme_is_reported_not_fatal(self):
        with patch("app.resource_path", return_value=Path("/nonexistent/dashboard.qss")):
            self.assertFalse(load_theme(self.app))

    def test_about_and_diagnostics_show_the_version(self):
        window = MainWindow()
        self.assertIn(__version__, window.about_text())
        self.assertIn("Version: " + __version__, window.diagnostics_report())
        window.close()

    def test_icon_and_build_files_exist(self):
        for name in ("packaging/icon.icns", "packaging/MeshtasticSniffer.spec", "scripts/build_mac.sh"):
            self.assertTrue((ROOT / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
