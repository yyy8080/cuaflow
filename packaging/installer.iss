#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif

[Setup]
AppId={{9C3A8B2E-6F14-4C92-9A5E-7B2D1E8F4C01}
AppName=cuaflow
AppVersion={#MyAppVersion}
AppVerName=cuaflow {#MyAppVersion}
AppPublisher=cuaflow
AppPublisherURL=https://github.com/yyy8080/cuaflow
AppSupportURL=https://github.com/yyy8080/cuaflow
DefaultDirName={autopf}\cuaflow
DefaultGroupName=cuaflow
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=cuaflow-setup-{#MyAppVersion}-win64
SetupIconFile=cuaflow.ico
UninstallDisplayIcon={app}\cuaflow-desktop.exe
UninstallDisplayName=cuaflow 多智能体对话工作台
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
ChangesEnvironment=yes
CloseApplications=yes

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=欢迎安装 cuaflow 多智能体对话工作台
WelcomeLabel2=本程序将在您的电脑上安装 cuaflow {#MyAppVersion}（单窗口一体化 WebUI，支持主智能体对话派发、多子智能体并行操控浏览器与桌面、中途动态调整放行、脚本录制省 Token 与持续巡检自愈）。%n%n点击「下一步」继续安装。
FinishedHeadingLabel=cuaflow 安装完成
FinishedLabel=cuaflow 已成功安装到您的电脑，可通过桌面图标或开始菜单直接启动一体化工作台。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 (&D)"; GroupDescription: "附加选项:"
Name: "addtopath"; Description: "将 cuaflow 命令行工具加入当前用户 PATH 环境变量 (&P)"; GroupDescription: "附加选项:"
Name: "autostart"; Description: "开机自动在后台启动 cuaflow 服务与定时持续监控 (&S)"; GroupDescription: "附加选项:"; Flags: unchecked

[Files]
Source: "..\dist\cuaflow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\cuaflow 工作台"; Filename: "{app}\cuaflow-desktop.exe"; WorkingDir: "{app}"; IconFilename: "{app}\cuaflow.ico"
Name: "{group}\cuaflow 命令行终端"; Filename: "{cmd}"; Parameters: "/k """"{app}\cuaflow.exe"" --help"""; WorkingDir: "{app}"; IconFilename: "{app}\cuaflow.ico"
Name: "{group}\卸载 cuaflow"; Filename: "{uninstallexe}"
Name: "{autodesktop}\cuaflow"; Filename: "{app}\cuaflow-desktop.exe"; WorkingDir: "{app}"; IconFilename: "{app}\cuaflow.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "cuaflow"; ValueData: """{app}\cuaflow-desktop.exe"" --no-gui --no-browser"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\cuaflow-desktop.exe"; Description: "立即启动 cuaflow 多智能体工作台"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM cuaflow-desktop.exe /T"; Flags: runhidden; RunOnceId: "StopCuaflowDesktop"

[Code]
const
  EnvSubKey = 'Environment';

procedure AddToUserPath(Dir: string);
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvSubKey, 'Path', Paths) then
    Paths := '';
  if Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Paths) + ';') > 0 then
    Exit;
  if (Paths <> '') and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  Paths := Paths + Dir;
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvSubKey, 'Path', Paths);
end;

procedure RemoveFromUserPath(Dir: string);
var
  Paths, Token, Remaining, ResultPaths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvSubKey, 'Path', Paths) then
    Exit;
  Remaining := Paths;
  ResultPaths := '';
  while Remaining <> '' do
  begin
    P := Pos(';', Remaining);
    if P > 0 then
    begin
      Token := Copy(Remaining, 1, P - 1);
      Delete(Remaining, 1, P);
    end
    else
    begin
      Token := Remaining;
      Remaining := '';
    end;
    if (Token <> '') and (CompareText(Token, Dir) <> 0) then
    begin
      if ResultPaths <> '' then
        ResultPaths := ResultPaths + ';';
      ResultPaths := ResultPaths + Token;
    end;
  end;
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvSubKey, 'Path', ResultPaths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToUserPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromUserPath(ExpandConstant('{app}'));
end;
