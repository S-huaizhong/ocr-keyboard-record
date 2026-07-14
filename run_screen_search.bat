@echo off
REM screen_search launcher (silent, no console window)
REM Priority: project .venv -> branded interpreter copy -> pythonw on PATH
chcp 65001 >nul
cd /d %~dp0

REM 1) Project-local .venv (recommended)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0screen_search.py"
    goto :eof
)

REM 2) Branded interpreter copy (legacy)
set "BRAND_EXE=OCR£¦Keyboard Record.exe"
set "PYCORE=%LOCALAPPDATA%\Python\pythoncore-3.14-64"
if exist "%PYCORE%\%BRAND_EXE%" (
    start "" "%PYCORE%\%BRAND_EXE%" "%~dp0screen_search.py"
    goto :eof
)

REM 3) pythonw.exe on PATH
where pythonw.exe >nul 2>&1
if %errorLevel% equ 0 (
    start "" pythonw.exe "%~dp0screen_search.py"
    goto :eof
)

echo [screen_search] No Python found. Run setup.bat first, or install Python 3.10+.
pause