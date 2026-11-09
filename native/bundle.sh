#!/usr/bin/env bash
# Build LFM3-Omni.app: a release build of the native window, wrapped as a macOS app with its
# icon and microphone permission text, signed ad hoc so macOS remembers the mic permission.
#   ./bundle.sh            -> dist/LFM3-Omni.app
#   ./bundle.sh --install  -> also copies it to ~/Applications
# The app finds the lab folder this was built from (or $LFM3_LAB_DIR), and talks to the lab
# server on port 8765; it can start that server itself.
# The icon is the app's own "icon" scene: to redraw assets/icon-1024.png after changing it,
#   cargo run -- --snap assets/icon-1024.png --scene icon --size 512x512 --scale 2
# then make AppIcon.icns from it with sips and iconutil.
set -euo pipefail
cd "$(dirname "$0")"

APP=dist/LFM3-Omni.app
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)"

if ! command -v cargo >/dev/null && [ -f "$HOME/.cargo/env" ]; then
  . "$HOME/.cargo/env"
fi
cargo build --release
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp target/release/lfm3-omni "$APP/Contents/MacOS/lfm3-omni"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LFM3-Omni</string>
  <key>CFBundleDisplayName</key><string>LFM3-Omni</string>
  <key>CFBundleIdentifier</key><string>io.github.hamsathul.lfm3-omni</string>
  <key>CFBundleExecutable</key><string>lfm3-omni</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>LFM3-Omni listens for voice messages and live voice mode. Audio goes only to your local lab server.</string>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP"
echo "Built $APP ($VERSION)"

if [ "${1:-}" = "--install" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/LFM3-Omni.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Installed ~/Applications/LFM3-Omni.app"
fi
