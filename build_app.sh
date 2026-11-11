#!/bin/bash
# The developer's own Mac install: the same layout install.command makes (~/.musicxx), with the app in /Applications.
# Rerun after editing musicxx.py. Needs install.command to have run once (it creates ~/.musicxx/venv).
set -e
cd "$(dirname "$0")"
DIR="$HOME/.musicxx"; V="$DIR/venv/bin"
[ -x "$V/python" ] || { echo "Run install.command once first."; exit 1; }
"$V/python" -m pip install -q -r requirements.txt pywebview
mkdir -p "$DIR/app"; cp musicxx.py analyze.py logo.png "$DIR/app/"
APP="/Applications/MUSICXX.app"
rm -rf "$APP"
# AppleScript applet: macOS shows it the normal "allow Desktop / microphone" prompts (a bare script .app gets silently denied)
osacompile -o "$APP" -e "do shell script \"'$V/python' '$DIR/app/musicxx.py' >/dev/null 2>&1 &\""
cp MUSICXX.icns "$APP/Contents/Resources/applet.icns"
plutil -replace NSMicrophoneUsageDescription -string "MUSICXX listens to identify the song that's playing." "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP"   # re-sign: bundle contents changed
touch "$APP"                          # make Finder pick up the new icon
echo "Built $APP"
