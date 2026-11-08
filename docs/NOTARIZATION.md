# Signing and notarizing the Mac app

Builds are ad-hoc signed until the secrets below exist. Once they do, `scripts/build_mac.sh` signs with hardened runtime, notarizes and staples the DMG, and fails if Apple does not accept it. This path has not run yet because no Apple credentials exist.

## What you need
A paid Apple Developer Program membership (US$99/year).

## Steps
1. In Apple Developer → Certificates, create a **Developer ID Application** certificate, install it in Keychain Access, and export it as a password-protected `.p12`.
2. At account.apple.com → Sign-In and Security, create an **app-specific password**.
3. Add these repository secrets. `gh secret set NAME -R TheLogsAreScreaming/shiny-guacamole` prompts for each value, so nothing lands in shell history. For the certificate: `base64 -i cert.p12 | gh secret set MACOS_CERT_P12_BASE64 -R TheLogsAreScreaming/shiny-guacamole`.

| Secret | Value |
| --- | --- |
| `MACOS_CERT_P12_BASE64` | the exported `.p12`, base64-encoded |
| `MACOS_CERT_PASSWORD` | the `.p12` password |
| `MACOS_SIGN_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID` | your Apple account email |
| `APPLE_TEAM_ID` | your 10-character team ID |
| `APPLE_APP_PASSWORD` | the app-specific password |

4. Run **Actions → Intel Mac app build → Run workflow**. The log should end the notarization step with `status: Accepted`.
