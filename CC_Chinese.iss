; CC_Chinese 安装包脚本 — Inno Setup 7
; 用法: cargo build --release 后执行
;   "D:\software1\Innosetup\Inno Setup 7\ISCC.exe" CC_Chinese.iss
; 产物: release\CC_Chinese_Setup.exe
; 说明: Tauri 版为单文件 exe（前端已嵌入二进制），双击后立即申请管理员权限

#define AppName "CC_Chinese"
#define AppVersion "1.0.0"
#define AppPublisher "CC_Chinese"

[Setup]
; AppId 是卸载识别用的唯一 ID，不要改（与 QT 旧版不同，避免卸载条目冲突）
AppId={{5E9C2B41-8F3D-4A66-B7E0-2C4A9D8F1E35}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\CC_Chinese
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 安装需要管理员 (初始化 ProgramData 受保护快照根)
PrivilegesRequired=admin
; 64 位机器装到真实 Program Files
ArchitecturesInstallIn64BitMode=x64compatible
; 产物输出目录与文件名
OutputDir=release
OutputBaseFilename=CC_Chinese_Setup
; 安装器图标 + 卸载图标 都用程序自带图标
SetupIconFile=src-tauri\icons\icon.ico
UninstallDisplayIcon={app}\CC_Chinese.exe
; 压缩方式: 最大压缩, 包更小（exe 本体已嵌入前端资源，单文件即可）
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; Tauri 单文件 exe，前端资源已嵌入
Source: "src-tauri\target\release\CC_Chinese.exe"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; 安装器预先创建受保护快照根；与 Rust 端 backup_manifest::snapshot_root 一致
Name: "{commonappdata}\Claude-zh-CN-official-backup"
Name: "{commonappdata}\Claude-zh-CN-official-backup\snapshots"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\CC_Chinese.exe"; AppUserModelID: "CC_Chinese.Desktop"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CC_Chinese.exe"; Tasks: desktopicon; AppUserModelID: "CC_Chinese.Desktop"

[Run]
; 快照根仅 SYSTEM/Administrators 可写，Users 只读执行
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\Claude-zh-CN-official-backup"" /setowner ""*S-1-5-32-544"" /q"; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化安全快照目录..."
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\Claude-zh-CN-official-backup\snapshots"" /setowner ""*S-1-5-32-544"" /q"; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化安全快照目录..."
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\Claude-zh-CN-official-backup"" /inheritance:r /grant:r ""*S-1-5-18:(OI)(CI)F"" ""*S-1-5-32-544:(OI)(CI)F"" ""*S-1-5-32-545:(OI)(CI)RX"""; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化安全快照目录..."
; 安装完成后可选立即启动
Filename: "{app}\CC_Chinese.exe"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
// 卸载 CC_Chinese 不会恢复 Claude；快照会被保留；卸载前明确告知用户
function InitializeUninstall(): Boolean;
begin
  Result := MsgBox(
    '卸载 CC_Chinese 不会恢复 Claude；如需恢复请先点击"恢复原样"。'+#13#10+#13#10+
    '卸载后本地安全快照将保留，可在重装 CC_Chinese 后继续使用。'+#13#10+#13#10+
    '确定继续卸载吗？',
    mbConfirmation, MB_YESNO) = idYES;
end;
