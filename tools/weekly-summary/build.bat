@echo off
chcp 65001 >nul
echo ========================================
echo   Building WeeklySummary Installer
echo ========================================
echo.

echo [1/2] PyInstaller packaging...
pyinstaller --onefile --windowed --name "WeeklySummary" --icon icon.ico --clean weekly_summary_gui.py
if errorlevel 1 (
    echo ERROR: PyInstaller failed
    pause
    exit /b 1
)
echo Done.
echo.

echo [2/2] Build complete!
echo   Output: dist\WeeklySummary.exe
echo.

if exist "C:\Program Files (x86)\NSIS\makensis.exe" (
    echo NSIS found, building installer...
    "C:\Program Files (x86)\NSIS\makensis.exe" installer.nsi
    echo   Installer: dist\WeeklySummary_Setup.exe
) else (
    echo NSIS not found, skipping installer creation.
    echo   Install NSIS to build the installer package.
)

echo.
pause
