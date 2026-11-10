#!/bin/bash
# MUSICXX installer for macOS and Linux.
# macOS: double-click this file (first time: right-click > Open).  Linux / Terminal: bash install.command
set -e
cd "$(dirname "$0")"
DIR="$HOME/.musicxx"
echo "== Installing MUSICXX into $DIR"

# Python 3.10+ (spotdl needs it). Prefer versions every dependency ships ready-made packages for.
PY=""
for p in python3.12 python3.11 python3.13 python3.10 python3.14 python3.15 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "MUSICXX needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ and run this installer again."
  [ "$(uname)" = Darwin ] && open "https://www.python.org/downloads/"
  read -rp "Press Enter to close…"; exit 1
fi
echo "== Using $("$PY" --version)"

mkdir -p "$DIR/app"
cp musicxx.py analyze.py logo.png "$DIR/app/"
"$PY" -m venv "$DIR/venv"
V="$DIR/venv/bin"
"$V/python" -m pip install -q --upgrade pip
echo "== Downloading the music tools (takes a minute or two)…"
"$V/python" -m pip install -q -r requirements.txt
"$V/python" -m pip install -q essentia 2>/dev/null || echo "   (key/BPM analysis isn't available for this Python; everything else works)"
# ffmpeg converts audio to .wav; spotdl fetches a copy into ~/.spotdl, which MUSICXX looks in
command -v ffmpeg >/dev/null || [ -x "$HOME/.spotdl/ffmpeg" ] || "$V/spotdl" --download-ffmpeg

if [ "$(uname)" = Darwin ]; then
  # A real app with the logo, plus an alias on the Desktop. An AppleScript applet gets macOS's normal
  # "allow access to Desktop" prompt, which MUSICXX needs to save into Desktop/OK.
  APP="$HOME/Applications/MUSICXX.app"
  mkdir -p "$HOME/Applications"; rm -rf "$APP"
  osacompile -o "$APP" -e "do shell script \"'$V/python' '$DIR/app/musicxx.py' >/dev/null 2>&1 &\""
  cp MUSICXX.icns "$APP/Contents/Resources/applet.icns"
  codesign --force --deep -s - "$APP" 2>/dev/null || true
  touch "$APP"
  NAME="MUSICXX"; [ -e "$HOME/Desktop/$NAME" ] && NAME="MUSICXX App"  # never touch an existing file/folder
  [ -e "$HOME/Desktop/$NAME" ] || osascript -e "tell application \"Finder\" to make alias file to POSIX file \"$APP\" at POSIX file \"$HOME/Desktop\" with properties {name:\"$NAME\"}" >/dev/null
  echo "== Done! Open MUSICXX from your Desktop, Launchpad or Spotlight."
  open "$APP"
else
  LAUNCH="$HOME/.local/share/applications/musicxx.desktop"
  mkdir -p "$(dirname "$LAUNCH")"
  printf '[Desktop Entry]\nType=Application\nName=MUSICXX\nExec="%s" "%s"\nIcon=%s\nTerminal=false\n' \
    "$V/python" "$DIR/app/musicxx.py" "$DIR/app/logo.png" > "$LAUNCH"
  if [ -d "$HOME/Desktop" ]; then cp "$LAUNCH" "$HOME/Desktop/"; chmod +x "$HOME/Desktop/musicxx.desktop"; fi
  echo "== Done! Open MUSICXX from your app menu."
  nohup "$V/python" "$DIR/app/musicxx.py" >/dev/null 2>&1 &
fi
