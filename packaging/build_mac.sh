#!/bin/bash
# Builds dist/MUSICXX.app and dist/MUSICXX-mac-<arm64|intel>.dmg: a self-contained app with its own Python,
# ffmpeg and download tools, so friends just drag it to Applications. Runs on a Mac (locally or GitHub Actions).
set -euo pipefail
cd "$(dirname "$0")/.."
PBS="https://github.com/astral-sh/python-build-standalone/releases/download/20261026"
case "$(uname -m)" in
  arm64) PY_TGZ="cpython-3.12.16+20261026-aarch64-apple-darwin-install_only.tar.gz"; NAME=arm64 ;;
  *)     PY_TGZ="cpython-3.12.16+20261026-x86_64-apple-darwin-install_only.tar.gz"; NAME=intel ;;
esac
APP=dist/MUSICXX.app; R="$APP/Contents/Resources"
rm -rf dist; mkdir -p "$APP/Contents/MacOS" "$R/app"

echo "== Python"
curl -fsSL "$PBS/$PY_TGZ" | tar -xz -C "$R"          # -> Resources/python
PY="$R/python/bin/python3"
touch "$R/python/MUSICXX_BUNDLED"                     # tells MUSICXX this Python is its own
"$PY" -m pip install -q --upgrade pip
echo "== Tools and libraries"
"$PY" -m pip install -q -r requirements.txt essentia pywebview
echo "== ffmpeg"
# it asks before replacing an ffmpeg it finds elsewhere on the machine: answer yes
TMPHOME=$(mktemp -d); echo y | HOME="$TMPHOME" "$PY" -m spotdl --download-ffmpeg >/dev/null
cp "$TMPHOME/.spotdl/ffmpeg" "$R/python/bin/ffmpeg"; rm -rf "$TMPHOME"
# pip wrote this build machine's path into each tool's first line: make them find the Python next to them instead
"$PY" - "$R/python/bin" <<'PYFIX'
import sys, pathlib
for f in pathlib.Path(sys.argv[1]).iterdir():
    if f.is_file() and not f.is_symlink():
        data = f.read_bytes()
        if data.startswith(b"#!") and b"python" in data.split(b"\n", 1)[0]:
            rest = data.split(b"\n", 1)[1]
            f.write_bytes(b"#!/bin/sh\n'''exec' \"$(dirname \"$0\")/python3\" \"$0\" \"$@\"\n' '''\n" + rest)
PYFIX

echo "== App"
cp musicxx.py analyze.py logo.png "$R/app/"
cp MUSICXX.icns "$R/MUSICXX.icns"
clang -O2 -mmacosx-version-min=11.0 -o "$APP/Contents/MacOS/MUSICXX" packaging/launcher.c \
  -I"$R/python/include/python3.12" -L"$R/python/lib" -lpython3.12 -Wl,-rpath,@executable_path/../Resources/python/lib
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>MUSICXX</string><key>CFBundleDisplayName</key><string>MUSICXX</string>
<key>CFBundleIdentifier</key><string>com.musicxx.app</string><key>CFBundleExecutable</key><string>MUSICXX</string>
<key>CFBundleIconFile</key><string>MUSICXX</string><key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>2.0</string><key>LSMinimumSystemVersion</key><string>11.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSMicrophoneUsageDescription</key><string>MUSICXX listens to identify the song that's playing.</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP"                   # ad-hoc: Apple Silicon refuses to run unsigned code
"$APP/Contents/MacOS/MUSICXX" "$R/app/musicxx.py" --test

echo "== Disk image"
STAGE=$(mktemp -d); cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname MUSICXX -srcfolder "$STAGE" -ov -format UDZO "dist/MUSICXX-mac-$NAME.dmg"
rm -rf "$STAGE"
du -sh "$APP" "dist/MUSICXX-mac-$NAME.dmg"
