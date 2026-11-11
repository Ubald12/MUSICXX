#!/bin/sh
# Starts MUSICXX on a Mac from inside its own small app bundle, so the Dock, menu bar and app switcher
# show "MUSICXX" with its icon instead of "Python". Falls back to plain Python if that isn't possible
# (e.g. a Python that isn't a macOS framework build).
D="$HOME/.musicxx"; V="$D/venv/bin/python"; H="$D/MUSICXX.app/Contents"
SRC="$("$V" -c 'import sys; print(sys.base_prefix)')/Resources/Python.app/Contents/MacOS/Python"
if [ -f "$SRC" ]; then
  mkdir -p "$H/MacOS" "$H/Resources"
  cmp -s "$SRC" "$H/MacOS/MUSICXX" || cp "$SRC" "$H/MacOS/MUSICXX"  # also refreshes it after a Python update
  cp "$D/app/MUSICXX.icns" "$H/Resources/MUSICXX.icns" 2>/dev/null
  cat > "$H/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>MUSICXX</string><key>CFBundleDisplayName</key><string>MUSICXX</string>
<key>CFBundleIdentifier</key><string>com.musicxx.window</string><key>CFBundleExecutable</key><string>MUSICXX</string>
<key>CFBundleIconFile</key><string>MUSICXX</string><key>CFBundlePackageType</key><string>APPL</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSMicrophoneUsageDescription</key><string>MUSICXX listens to identify the song that's playing.</string>
</dict></plist>
PLIST
  # the venv's Python normally passes this itself; it tells the copied binary which venv to use
  __PYVENV_LAUNCHER__="$V" exec "$H/MacOS/MUSICXX" "$D/app/musicxx.py"
fi
exec "$V" "$D/app/musicxx.py"
