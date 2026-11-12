@echo off
rem MUSICXX installer for Windows 10/11. Double-click this file.
setlocal
cd /d "%~dp0"
set "DIR=%LOCALAPPDATA%\MUSICXX"
echo == Installing MUSICXX into %DIR%

rem Python 3.10+ (spotdl needs it). The Microsoft Store "python" stub fails this check, which is what we want.
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo == Python not found. Installing Python 3.12 for your user account...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
%PY% --version || goto nopython

if not exist "%DIR%\app" mkdir "%DIR%\app"
copy /y musicxx.py "%DIR%\app\" >nul
copy /y analyze.py "%DIR%\app\" >nul
copy /y intro.py "%DIR%\app\" >nul
copy /y logo.png "%DIR%\app\" >nul
copy /y logo.ico "%DIR%\app\" >nul
%PY% -m venv "%DIR%\venv" || goto fail
set "V=%DIR%\venv\Scripts"
"%V%\python.exe" -m pip install -q --upgrade pip
echo == Downloading the music tools (takes a minute or two)...
"%V%\python.exe" -m pip install -q -r requirements.txt || goto fail
rem Own app window (needs Microsoft WebView2, built into Windows 10/11). Optional: falls back to the browser.
"%V%\python.exe" -m pip install -q pywebview || echo    (MUSICXX will open in your browser instead of its own window)
rem ffmpeg converts audio to .wav; spotdl fetches a copy into %USERPROFILE%\.spotdl, which MUSICXX looks in
where ffmpeg >nul 2>nul || if not exist "%USERPROFILE%\.spotdl\ffmpeg.exe" "%V%\spotdl.exe" --download-ffmpeg

rem Automated test run (GitHub Actions): check the install works, skip shortcuts
if defined CI goto citest

rem Desktop + Start menu shortcuts. pythonw = no console window; use the Quit button in MUSICXX to stop it.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $s = $w.CreateShortcut((Join-Path $d 'MUSICXX.lnk'));" ^
  "  $s.TargetPath = $env:V + '\pythonw.exe';" ^
  "  $s.Arguments = '\"' + $env:DIR + '\app\musicxx.py\"';" ^
  "  $s.IconLocation = $env:DIR + '\app\logo.ico';" ^
  "  $s.Save() }"

echo.
echo == Done! Open MUSICXX from the shortcut on your Desktop or the Start menu.
start "" "%V%\pythonw.exe" "%DIR%\app\musicxx.py"
pause
exit /b 0

:citest
"%V%\python.exe" "%DIR%\app\musicxx.py" --test
exit /b %errorlevel%

:nopython
echo.
echo Couldn't install Python automatically. Get Python 3.12 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during setup), then run this installer again.
start "" https://www.python.org/downloads/
if not defined CI pause
exit /b 1

:fail
echo.
echo Something went wrong (see the messages above). Check your internet connection and run the installer again.
if not defined CI pause
exit /b 1
