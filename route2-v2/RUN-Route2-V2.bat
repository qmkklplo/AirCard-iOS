@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Route 2 V2 - iOS 27 Mercury Configuration Extractor

echo ============================================================
echo Route 2 V2 - iOS 27 Mercury Configuration Extractor
echo GoldenNugget-style targeted MobileBackup2 read
echo.
echo READ-ONLY: NO restore / NO rebuild / NO PosterBoard write-back
echo ============================================================
echo.
echo Before running:
echo   1. Connect iPhone by USB.
echo   2. Unlock it and keep the screen awake.
echo   3. Tap "Trust This Computer" if iOS asks.
echo   4. Close GoldenNugget / iTunes / Apple Devices if they hold the phone.
echo.
echo V2 targets the two already-confirmed Mercury configuration candidates:
echo   0AEA1C8E-3EA7-4E6E-81E4-511972087A54
echo   769BEBC4-9C5E-4A57-84C6-939C9C90C521
echo.
echo It intentionally excludes the old experimental C901 configuration.
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYLAUNCH=py -3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found.
        echo Install Python 3.10 or newer, then run this file again.
        echo.
        pause
        exit /b 1
    )
    set "PYLAUNCH=python"
)

if not exist ".route2-mb2-venv\Scripts\python.exe" (
    echo [SETUP] Creating isolated Python environment...
    %PYLAUNCH% -m venv ".route2-mb2-venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the Python virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=.route2-mb2-venv\Scripts\python.exe"
set "READY=.route2-mb2-venv\.pymobiledevice3-10.7.1-ready"

if not exist "%READY%" (
    echo [SETUP] Installing pymobiledevice3 10.7.1...
    "%VENV_PY%" -m pip install --disable-pip-version-check "pymobiledevice3==10.7.1"
    if errorlevel 1 (
        echo [ERROR] pymobiledevice3 installation failed.
        pause
        exit /b 1
    )
    >"%READY%" echo ready
)

echo.
echo [RUN] Starting V2 read-only extraction...
echo.
"%VENV_PY%" "route2_v2_extract.py"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo [DONE] V2 extraction completed successfully.
) else if "%RC%"=="2" (
    echo [DONE] V2 extracted data, but the Core-7 candidate set was partial.
) else (
    echo [DONE] V2 ended with an error. A diagnostic ZIP may still have been created.
)
echo.
echo Look inside:
echo   Route2-V2-Captures
echo.
pause
exit /b %RC%
