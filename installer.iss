; 队友视野 SakuraVision v1.3.0 安装脚本 (Inno Setup 6)
; 使用方法: 运行 build_installer.bat，或手动执行 ISCC.exe installer.iss

[Setup]
AppId={{A7D3F9E1-5B2C-4E8A-9D1F-6C4B0A2E8F11}
AppName=队友视野 SakuraVision
AppVersion=1.3.0
AppPublisher=SakuraVision Team
DefaultDirName={autopf}\SakuraVision
DefaultGroupName=队友视野
OutputDir=dist
OutputBaseFilename=SakuraVision-Setup-1.3.0
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
; 第 74 条：splash.png 是 splash.py 从 exe_dir()/assets/splash.png 读取的启动图，
; 冻结运行时 exe_dir()={app}，故必须随包安装到 {app}\assets，否则安装版静默降级为纯文字启动画面。
; 它是仓库已提交资源（构建期必然存在），故不加 skipifsourcedoesntexist——缺失就该让 ISCC 报错。
Source: "assets\splash.png"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\队友视野-共享端"; Filename: "{app}\fps-host.exe"; IconFilename: "{app}\assets\app.ico"
Name: "{group}\队友视野-观看端"; Filename: "{app}\fps-viewer.exe"
Name: "{autodesktop}\队友视野-观看端"; Filename: "{app}\fps-viewer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\fps-viewer.exe"; Description: 运行队友视野观看端; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 第 24 条：卸载清理凭据/配置残渣，避免二手或共用机器上留下 frp token 与口令哈希。
Type: files; Name: "{app}\accounts.json"
Type: files; Name: "{app}\accounts.json.corrupt-*"
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\config.json.corrupt-*"
Type: filesandordirs; Name: "{app}\logs"
; 第 13 条：安装目录不可写时 logger 回退到 %LOCALAPPDATA%\SakuraVision\logs，一并清理。
Type: filesandordirs; Name: "{localappdata}\SakuraVision"
