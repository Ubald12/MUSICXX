#!/bin/bash
# Builds and installs /Applications/MUSICXX.app from musicxx.py + logo. Rerun after editing musicxx.py.
set -e
cd "$(dirname "$0")"
APP="/Applications/MUSICXX.app"
rm -rf "$APP"
# AppleScript applet: macOS shows it the normal "allow Desktop access" prompt (a bare script .app gets silently denied)
osacompile -o "$APP" \
  -e 'set py to quoted form of POSIX path of (path to resource "musicxx.py")' \
  -e 'do shell script "/usr/bin/curl -s -o /dev/null http://127.0.0.1:8765/jobs && open http://localhost:8765 || (/usr/bin/python3 " & py & " >/dev/null 2>&1 &)"'
/usr/bin/python3 make_icon.py "$APP/Contents/Resources/applet.icns" logo.png
cp musicxx.py analyze.py logo.png "$APP/Contents/Resources/"
codesign --force --deep -s - "$APP"   # re-sign: bundle contents changed
touch "$APP"                          # make Finder pick up the new icon
echo "Built $APP"
