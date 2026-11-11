#!/bin/bash
# Builds and installs /Applications/MUSICXX.app from musicxx.py + logo. Rerun after editing musicxx.py.
set -e
cd "$(dirname "$0")"
APP="/Applications/MUSICXX.app"
CODE="$HOME/Library/Application Support/MUSICXX/app"  # outside the signed bundle, so self-updates don't break the signature
mkdir -p "$CODE"
cp musicxx.py analyze.py logo.png "$CODE/"
rm -rf "$APP"
# AppleScript applet: macOS shows it the normal "allow Desktop access" prompt (a bare script .app gets silently denied)
osacompile -o "$APP" \
  -e "set py to quoted form of \"$CODE/musicxx.py\"" \
  -e 'do shell script "/usr/bin/curl -s -o /dev/null http://127.0.0.1:8765/jobs && open http://localhost:8765 || (/usr/bin/python3 " & py & " >/dev/null 2>&1 &)"'
/usr/bin/python3 make_icon.py "$APP/Contents/Resources/applet.icns" logo.png
codesign --force --deep -s - "$APP"   # re-sign: bundle contents changed
touch "$APP"                          # make Finder pick up the new icon
echo "Built $APP"
