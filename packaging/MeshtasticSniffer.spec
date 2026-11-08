# PyInstaller recipe for the Intel Mac app bundle. Run through scripts/build_mac.sh.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from app_version import __version__  # noqa: E402

APP_NAME = "Meshtastic Sniffer TCP"
BUNDLE_ID = "com.thelogsarescreaming.meshtasticsniffertcp"

datas = [(str(ROOT / "dashboard.qss"), ".")] + copy_metadata("PySide6")
binaries = []
hiddenimports = []
for package in ("meshtastic", "pubsub"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

analysis = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], datas=datas, binaries=binaries, hiddenimports=hiddenimports)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name=APP_NAME, console=False, target_arch="x86_64",
)
collected = COLLECT(exe, analysis.binaries, analysis.datas, name=APP_NAME)
app = BUNDLE(
    collected,
    name=f"{APP_NAME}.app",
    icon=str(ROOT / "packaging" / "icon.icns"),
    bundle_identifier=BUNDLE_ID,
    version=__version__,
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": os.environ.get("BUILD_NUMBER", "0"),
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # macOS 15+ blocks connections to LAN devices unless the app explains why.
        "NSLocalNetworkUsageDescription": "Connects to your Meshtastic radio on your local network to show the packets it reports.",
        # A packet monitor sits in the background; App Nap would delay its timers.
        "NSAppSleepDisabled": True,
    },
)
