@echo off
chcp 65001 >nul
echo ============================================================
echo   screen_search - Register logon autostart task
echo ============================================================
echo.

REM Requires admin rights (schtasks /RL HIGHEST)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] This script must run as administrator.
    echo [!] Right-click this file -^> "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo [+] Admin rights confirmed.
echo.

REM Priority: project .venv -> branded exe -> pythonw on PATH
set "PY_EXE="
if exist "%~dp0.venv\Scripts\pythonw.exe" set "PY_EXE=%~dp0.venv\Scripts\pythonw.exe"
if not defined PY_EXE (
    if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\OCR£¦Keyboard Record.exe" (
        set "PY_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\OCR£¦Keyboard Record.exe"
    )
)
if not defined PY_EXE (
    for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
        set "PY_EXE=%%P"
        goto :got_py
    )
)
:got_py

if not defined PY_EXE (
    echo [-] No pythonw.exe found. Run setup.bat first, or install Python 3.10+.
    pause
    exit /b 2
)

echo Creating scheduled task:
echo   Name:    ScreenSearch
echo   Trigger: ONLOGON
echo   RunAs:   HIGHEST
echo   Command: "%PY_EXE%" "%~dp0screen_search.py"
echo.

schtasks /Create /TN "ScreenSearch" ^
    /TR "\"%PY_EXE%\" \"%~dp0screen_search.py\"" ^
    /SC ONLOGON ^
    /RL HIGHEST ^
    /F

echo.
if %errorLevel% equ 0 (
    echo [+] Task created successfully.
    echo.
    echo Verify:  schtasks /Query /TN "ScreenSearch"
    echo Run:     schtasks /Run   /TN "ScreenSearch"
    echo Delete:  schtasks /Delete /TN "ScreenSearch" /F
    echo.
    echo Will auto-launch at next logon with no UAC prompt.
) else (
    echo [-] Failed. errorlevel=%errorLevel%
)
echo.
pause