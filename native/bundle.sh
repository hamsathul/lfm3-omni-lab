#!/usr/bin/env bash
# Build LFM3-Omni.app: a release build of the native window, wrapped as a macOS app with its
# icon and microphone permission text, signed ad hoc so macOS remembers the mic permission.
#   ./bundle.sh            -> dist/LFM3-Omni.app
#   ./bundle.sh --install  -> also copies it to ~/Applications
#   ./bundle.sh --dist     -> a build for the GitHub release, zipped as
#                             dist/LFM3-Omni-<version>-macOS-<arch>.zip
# The app talks to the lab server on port 8765 and can start that server itself. A normal
# build finds the lab in the folder it was built from; a --dist build looks in the usual
# places, or offers to download the lab (see src/lab.rs and src/setup.rs). --dist also keeps
# this computer's folder names out of the program.
# The icon is the app's own "icon" scene: to redraw assets/icon-1024.png after changing it,
#   cargo run -- --snap assets/icon-1024.png --scene icon --size 512x512 --scale 2
# then make AppIcon.icns from it with sips and iconutil.
set -euo pipefail
cd "$(dirname "$0")"

APP=dist/LFM3-Omni.app
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)"

INSTALL=0
DIST=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --dist) DIST=1 ;;
    *) echo "Unknown option: $arg (use --install or --dist)"; exit 1 ;;
  esac
done

if ! command -v cargo >/dev/null && [ -f "$HOME/.cargo/env" ]; then
  . "$HOME/.cargo/env"
fi
if [ "$DIST" = 1 ]; then
  # Its own target folder, since the changed flags rebuild everything.
  export CARGO_TARGET_DIR=target/dist
  export CARGO_ENCODED_RUSTFLAGS="--remap-path-prefix=$HOME=~"
  cargo build --release --features dist
  BIN=target/dist/release/lfm3-omni
else
  cargo build --release
  BIN=target/release/lfm3-omni
fi
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/lfm3-omni"
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

if [ "$DIST" = 1 ]; then
  if strings "$BIN" | grep -qF "$HOME"; then
    echo "The release build still names $HOME; not zipping it."
    exit 1
  fi
  ZIP="dist/LFM3-Omni-$VERSION-macOS-$(uname -m).zip"
  rm -f "$ZIP"
  ditto -c -k --keepParent "$APP" "$ZIP"
  echo "Release zip: native/$ZIP ($(du -h "$ZIP" | cut -f1 | tr -d ' '))"
fi

if [ "$INSTALL" = 1 ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/LFM3-Omni.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Installed ~/Applications/LFM3-Omni.app"
fi
