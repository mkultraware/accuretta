; SPDX-License-Identifier: AGPL-3.0-only
; Inno Setup 6 script for Accuretta. Build it with tools\installer\build.ps1,
; which prepares build\app, build\python and build\install.json first.
;
; Layout (per user, no admin rights):
;   {localappdata}\Programs\Accuretta\app\        Accuretta's files
;   {localappdata}\Programs\Accuretta\python\     embedded Python + packages
;   {localappdata}\Programs\Accuretta\install.json
;   {localappdata}\Accuretta\                     the user's files (launch.pyw
;                                                 sets ACCURETTA_HOME); upgrades
;                                                 never touch it

#ifndef AppVersion
  #error AppVersion is not defined. Run build.ps1.
#endif
#ifndef BuildDir
  #define BuildDir "build"
#endif
#ifndef DistDir
  #define DistDir "dist"
#endif

[Setup]
; Never change AppId: upgrades find the existing install by it.
AppId={{B83E2206-A134-4274-A99E-50A6F60E5E3B}
AppName=Accuretta
AppVersion={#AppVersion}
AppVerName=Accuretta {#AppVersion}
AppPublisher=mkultraware
AppPublisherURL=https://github.com/mkultraware/accuretta
AppSupportURL=https://github.com/mkultraware/accuretta/issues
DefaultDirName={localappdata}\Programs\Accuretta
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#DistDir}
OutputBaseFilename=Accuretta-Setup-{#AppVersion}
SetupIconFile={#BuildDir}\app\assets\icons\accuretta.ico
UninstallDisplayIcon={app}\app\assets\icons\accuretta.ico
UninstallDisplayName=Accuretta
LicenseFile={#BuildDir}\app\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the app and Python completely, so no stale files remain.
; The user's files live in {localappdata}\Accuretta and are not touched.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\install.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\Accuretta"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\icons\accuretta.ico"
Name: "{autodesktop}\Accuretta"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\assets\icons\accuretta.ico"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2..."; Check: NeedsWebView2; Flags: skipifdoesntexist waituntilterminated
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launch.pyw"""; WorkingDir: "{app}\app"; Description: "Launch Accuretta"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Code]
const
  WebView2Key = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2KeyWow = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasVersion(Root: Integer; Key: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasVersion(HKLM, WebView2KeyWow) or HasVersion(HKLM, WebView2Key) or HasVersion(HKCU, WebView2Key));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserFiles: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    UserFiles := ExpandConstant('{localappdata}\Accuretta');
    if DirExists(UserFiles) and not UninstallSilent then
      if MsgBox('Also delete your chats, settings and downloaded models setup?' + #13#10 + #13#10 +
                'Folder: ' + UserFiles + #13#10 + #13#10 +
                'Choose No to keep them for a later reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(UserFiles, True, True, True);
  end;
end;
