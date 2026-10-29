#!/bin/sh
# Build YouChatBar.app (macOS only). Needs swiftc from Xcode Command Line Tools; no Xcode project.
#   ./build.sh            -> macos/build/YouChatBar.app
#   ./build.sh --install  -> also copy to ~/Applications and start at login (launchd)
set -eu
cd "$(dirname "$0")"
[ "$(uname)" = "Darwin" ] || { echo "YouChat Bar is macOS only"; exit 1; }

APP=build/YouChatBar.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp YouChatBar/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
swiftc -O -swift-version 5 -parse-as-library -target "$(uname -m)-apple-macos14" \
  YouChatBar/Sources/*.swift -o "$APP/Contents/MacOS/YouChatBar"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.youchat.youchatbar</string>
  <key>CFBundleName</key><string>YouChat Bar</string>
  <key>CFBundleExecutable</key><string>YouChatBar</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
codesign -s - --force "$APP" >/dev/null 2>&1 || true
echo "built $APP"

if [ "${1:-}" = "--install" ]; then
  DEST="$HOME/Applications/YouChatBar.app"
  mkdir -p "$HOME/Applications"
  pkill -x YouChatBar 2>/dev/null || true
  rm -rf "$DEST" && cp -R "$APP" "$DEST"
  PLIST="$HOME/Library/LaunchAgents/com.youchat.bar.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.youchat.bar</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>$DEST</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/com.youchat.bar" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "installed $DEST (starts at login)"
fi
