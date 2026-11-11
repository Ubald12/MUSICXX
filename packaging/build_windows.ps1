# Builds dist/MUSICXX-Setup-Windows.exe: MUSICXX with its own Python, ffmpeg and download tools, installed for
# the current user (no admin rights). Runs on Windows (GitHub Actions) and needs Inno Setup (iscc).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$tag = "20261026"; $py = "cpython-3.12.16+$tag-x86_64-pc-windows-msvc-install_only.tar.gz"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory build, build/app, dist | Out-Null
Invoke-WebRequest "https://github.com/astral-sh/python-build-standalone/releases/download/$tag/$py" -OutFile build/py.tar.gz
tar -xzf build/py.tar.gz -C build          # -> build/python
Remove-Item build/py.tar.gz
New-Item build/python/MUSICXX_BUNDLED | Out-Null  # tells MUSICXX this Python is its own
$P = "build/python/python.exe"
& $P -m pip install -q --upgrade pip; if ($LASTEXITCODE) { exit 1 }
& $P -m pip install -q -r requirements.txt pywebview; if ($LASTEXITCODE) { exit 1 }
# ffmpeg: the build spotdl fetches into ~/.spotdl
$realHome = $env:USERPROFILE
$env:USERPROFILE = (New-Item -ItemType Directory -Force build/home).FullName
"y" | & $P -m spotdl --download-ffmpeg | Out-Null  # answers its "replace existing ffmpeg?" question
$env:USERPROFILE = $realHome
Copy-Item build/home/.spotdl/ffmpeg.exe build/python/
Remove-Item -Recurse -Force build/home
Copy-Item musicxx.py, analyze.py, logo.png, logo.ico build/app/
& $P build/app/musicxx.py --test; if ($LASTEXITCODE) { exit 1 }
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc packaging/musicxx.iss; if ($LASTEXITCODE) { exit 1 }
