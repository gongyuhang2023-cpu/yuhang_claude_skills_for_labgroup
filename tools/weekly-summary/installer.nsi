; WeeklySummary NSIS Installer Script
; Requires NSIS 3.x

!include "MUI2.nsh"

; General
Name "Weekly Summary"
OutFile "dist\WeeklySummary_Setup.exe"
InstallDir "$PROGRAMFILES\WeeklySummary"
RequestExecutionLevel admin

; Interface Settings
!define MUI_ABORTWARNING
!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Language
!insertmacro MUI_LANGUAGE "SimpChinese"

; Installer Section
Section "Install"
    SetOutPath "$INSTDIR"

    ; Copy main executable
    File "dist\WeeklySummary.exe"
    File "icon.ico"

    ; Create uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut
    CreateShortCut "$DESKTOP\Weekly Summary.lnk" "$INSTDIR\WeeklySummary.exe" "" "$INSTDIR\icon.ico"

    ; Start menu
    CreateDirectory "$SMPROGRAMS\WeeklySummary"
    CreateShortCut "$SMPROGRAMS\WeeklySummary\Weekly Summary.lnk" "$INSTDIR\WeeklySummary.exe" "" "$INSTDIR\icon.ico"
    CreateShortCut "$SMPROGRAMS\WeeklySummary\卸载.lnk" "$INSTDIR\Uninstall.exe"

    ; Registry for Add/Remove Programs
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\WeeklySummary" "DisplayName" "Weekly Summary"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\WeeklySummary" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\WeeklySummary" "DisplayIcon" "$INSTDIR\icon.ico"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\WeeklySummary" "Publisher" "Lab Weekly Summary Tool"
SectionEnd

; Uninstaller Section
Section "Uninstall"
    Delete "$INSTDIR\WeeklySummary.exe"
    Delete "$INSTDIR\icon.ico"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$DESKTOP\Weekly Summary.lnk"
    Delete "$SMPROGRAMS\WeeklySummary\Weekly Summary.lnk"
    Delete "$SMPROGRAMS\WeeklySummary\卸载.lnk"
    RMDir "$SMPROGRAMS\WeeklySummary"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\WeeklySummary"

    ; Note: config in %APPDATA%\WeeklySummary is preserved on uninstall
SectionEnd
