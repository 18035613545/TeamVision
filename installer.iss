; 队友视野 TeamVision v1.5.0 安装脚本 (Inno Setup 6)
; 使用方法: 运行 build_installer.bat，或手动执行 ISCC.exe installer.iss

[Setup]
AppId={{A7D3F9E1-5B2C-4E8A-9D1F-6C4B0A2E8F11}
AppName=队友视野 TeamVision
AppVersion=1.5.0
; 版本真源是 common.APP_VERSION：改版本时本文件的文件头注释 / AppVersion /
; OutputBaseFilename 三处必须同步（tests/test_round9_installer.py 会逐条比对）。
AppPublisher=TeamVision Team
; 第 102 条：默认改为**用户级安装**（{localappdata}\Programs，无需管理员）。
; 原来装到 {autopf}（= C:\Program Files）时，非管理员运行时既写不了 config.json
; （改设置"改了没反应"、首次启动直接 PermissionError），也写不了 accounts.json
; （注册失败）。用户级安装目录天然可写，同时免去 UAC 提权。
; 配合 common.data_dir() 的回退：即使有人手动装到 Program Files，配置/账户/日志
; 也会落到 %LOCALAPPDATA%\TeamVision 而不是静默失败。
DefaultDirName={localappdata}\Programs\TeamVision
PrivilegesRequired=lowest
DefaultGroupName=队友视野
OutputDir=dist
OutputBaseFilename=TeamVision-Setup-1.5.0
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\fps-viewer\fps-viewer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 第 9 轮（部署与更新）：用户数据（%LOCALAPPDATA%\TeamVision 下的配置/账户）在卸载时
; **逐项询问**再删。旧版本靠 [UninstallDelete] 直接整目录抹掉——而该目录由同机两端共用，
; 卸掉一端等于静默清空另一端的频道与账户。这里交给 Inno Setup 6.3+ 的标准询问机制：
; 用户选"是"才删这两份数据文件（含 frp token / 口令哈希），选"否"则原样保留。
; 安装目录内的残渣仍由下方 [UninstallDelete] 清理。
UninstallablePathsDataFiles={localappdata}\TeamVision\accounts.json,{localappdata}\TeamVision\config.json

[Tasks]
Name: desktopicon; Description: 创建桌面快捷方式; GroupDescription: 附加任务

[Files]
; 第 131 条：打包形态由 onefile 改为 onedir，两个程序各自是完整目录
; （<name>.exe + _internal\，含 Python 运行时与 FFmpeg/NVENC DLL），因此整目录安装。
; 好处：启动时不再把自身解压到 %TEMP% 再从临时目录执行，去掉杀软/EDR 的经典误报链。
Source: "dist\fps-host\*"; DestDir: "{app}\fps-host"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\fps-viewer\*"; DestDir: "{app}\fps-viewer"; Flags: ignoreversion recursesubdirs createallsubdirs
; frpc.exe 放在安装根：host/viewer 按 common.app_dirs() 搜索 exe 目录与上一级，
; 因此放在 {app} 即可被两端的「启动隧道」找到。
Source: "dist\frpc.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "assets\app.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; 启动图 splash.png 已由 spec 的 datas 内嵌进 exe（splash.py 冻结时从 sys._MEIPASS/assets 读取），
; 程序自带启动图，不再需要随包安装到 {app}\assets，故此处不再列 splash.png。
; app.ico 仍随包安装到 {app}\assets，供下方 [Icons] 快捷方式与 UninstallDisplayIcon 使用。

[Icons]
Name: "{group}\队友视野-共享端"; Filename: "{app}\fps-host\fps-host.exe"; IconFilename: "{app}\assets\app.ico"
Name: "{group}\队友视野-观看端"; Filename: "{app}\fps-viewer\fps-viewer.exe"
Name: "{autodesktop}\队友视野-观看端"; Filename: "{app}\fps-viewer\fps-viewer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\fps-viewer\fps-viewer.exe"; Description: 运行队友视野观看端; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 第 24 条：卸载清理凭据/配置残渣，避免二手或共用机器上留下 frp token 与口令哈希。
; 第 9 轮（部署与更新）：**只清本安装目录**。旧版本在这里还列了
; `{localappdata}\TeamVision`（整目录 filesandordirs）——那是 common.data_dir() 的
; 数据目录，同一台机器上的共享端与观看端**共用**它；按"拆包分别安装"的形态，
; 卸掉共享端会把观看端的频道列表、账户与日志一起抹掉，而且没有任何确认。
; 用户自己的数据交给安装向导的"删除数据"选项（UninstallablePathsDataFiles）：
; 卸载前会明确询问，选"是"才清那一份。凭据残渣的要求不变——选择保留时
; config.json 里的 frp token 仍可被用户在向导里点名删除。
Type: files; Name: "{app}\accounts.json"
Type: files; Name: "{app}\accounts.json.corrupt-*"
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\config.json.corrupt-*"
Type: filesandordirs; Name: "{app}\logs"
; dist-021（2026-09-24）：onedir 下两端的数据实际会落在 {app}\fps-host\、{app}\fps-viewer\
; （common.data_dir() 在用户目录不可写时回退 exe_dir，且 _migrate_from_exe_dir 复制不删源），
; 原清单只清 {app}\ 根，老用户升级/回退场景的凭据残渣会留在安装目录。下面按同一口径补两端
; 的子目录，仍只删数据文件/目录（config.json / accounts.json / logs），不碰 exe 与 _internal。
Type: files; Name: "{app}\fps-host\accounts.json"
Type: files; Name: "{app}\fps-host\accounts.json.corrupt-*"
Type: files; Name: "{app}\fps-host\config.json"
Type: files; Name: "{app}\fps-host\config.json.corrupt-*"
Type: filesandordirs; Name: "{app}\fps-host\logs"
Type: files; Name: "{app}\fps-viewer\accounts.json"
Type: files; Name: "{app}\fps-viewer\accounts.json.corrupt-*"
Type: files; Name: "{app}\fps-viewer\config.json"
Type: files; Name: "{app}\fps-viewer\config.json.corrupt-*"
Type: filesandordirs; Name: "{app}\fps-viewer\logs"

