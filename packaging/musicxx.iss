; Inno Setup script for the Windows installer (built by build_windows.ps1).
[Setup]
AppName=MUSICXX
AppVersion=2.0
AppPublisher=MUSICXX
DefaultDirName={localappdata}\MUSICXX-App
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\dist
OutputBaseFilename=MUSICXX-Setup-Windows
SetupIconFile=..\logo.ico
UninstallDisplayIcon={app}\app\logo.ico
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\build\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userdesktop}\MUSICXX"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\musicxx.py"""; IconFilename: "{app}\app\logo.ico"
Name: "{userprograms}\MUSICXX"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\musicxx.py"""; IconFilename: "{app}\app\logo.ico"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\musicxx.py"""; Description: "Open MUSICXX"; Flags: postinstall nowait
