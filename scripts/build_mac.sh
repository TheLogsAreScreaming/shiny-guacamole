#!/bin/bash
# Build the Intel .app and .dmg, then check what was actually built.
# Run from anywhere on an Intel Mac (or under Rosetta) with requirements.txt installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
NAME="Meshtastic Sniffer TCP"
VERSION="$(python -c 'from app_version import __version__; print(__version__)')"
DMG="dist/Meshtastic-Sniffer-TCP-${VERSION}-Intel.dmg"
APP="dist/$NAME.app"
EXE="$APP/Contents/MacOS/$NAME"
PLIST="$APP/Contents/Info.plist"

fail() { echo "BUILD CHECK FAILED: $*" >&2; exit 1; }

rm -rf build dist
python -m PyInstaller --noconfirm packaging/MeshtasticSniffer.spec

echo "--- checking the bundle"
[ "$(lipo -archs "$EXE")" = "x86_64" ] || fail "executable is not x86_64: $(lipo -archs "$EXE")"
[ "$(plutil -extract CFBundleShortVersionString raw "$PLIST")" = "$VERSION" ] || fail "bundle version mismatch"
plutil -extract NSLocalNetworkUsageDescription raw "$PLIST" >/dev/null || fail "local network description missing"
codesign --verify --deep "$APP" || fail "code signature does not verify"
QT_QPA_PLATFORM=offscreen MESHTASTIC_SNIFFER_SETTINGS="$(mktemp -d)/settings.ini" "$EXE" --self-check \
    || fail "bundled app failed its self-check"
MCP_OUT="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_nodes","arguments":{}}}' \
    | MESHTASTIC_SNIFFER_INTEGRATIONS_DIR="$(mktemp -d)" "$EXE" --assistant-mcp)"
echo "$MCP_OUT" | grep -q '"node_telemetry"' || fail "assistant link (MCP) did not list its tools"
echo "$MCP_OUT" | grep -q 'send_message' && fail "assistant link must not offer sending"
echo "$MCP_OUT" | grep -q 'assistant link is off' || fail "assistant link did not report the app as off"

echo "--- building the disk image"
STAGE="$(mktemp -d)"
ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

if [ -n "${MACOS_SIGN_IDENTITY:-}" ]; then
    echo "--- signing and notarizing the disk image"
    codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
    xcrun notarytool submit "$DMG" --wait \
        --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" \
        || fail "notarization was not accepted"
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
    spctl -a -t open --context context:primary-signature -v "$DMG" || fail "Gatekeeper rejected the disk image"
else
    echo "--- no MACOS_SIGN_IDENTITY: ad-hoc signed, not notarized"
fi
hdiutil verify "$DMG"
(cd dist && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
cat "$DMG.sha256"
