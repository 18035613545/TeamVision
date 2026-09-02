; 队友视野 SakuraVision v1.2.0 安装脚本 (Inno Setup 6)
; 使用方法: 运行 build_installer.bat，或手动执行 ISCC.exe installer.iss

[Setup]
AppId={{A7D3F9E1-5B2C-4E8A-9D1F-6C4B0A2E8F11}
AppName=队友视野 SakuraVision
AppVersion=1.2.0
AppPublisher=SakuraVision Team
DefaultDirName={autopf}\SakuraVision
DefaultGroupName=队友视野
OutputDir=dist
OutputBaseFilename=SakuraVision-Setup-1.2.0
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\fps-viewer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: desktopicon; Description: 创建桌面快捷方式; GroupDescription: 附加任务

[Files]
Source: "dist\fps-host.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\fps-viewer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\frpc.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "assets\app.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\队友视野-共享端"; Filename: "{app}\fps-host.exe"; IconFilename: "{app}\assets\app.ico"
Name: "{group}\队友视野-观看端"; Filename: "{app}\fps-viewer.exe"
Name: "{autodesktop}\队友视野-观看端"; Filename: "{app}\fps-viewer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\fps-viewer.exe"; Description: 运行队友视野观看端; Flags: nowait postinstall skipifsilent
