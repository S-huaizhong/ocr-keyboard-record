@echo off
chcp 65001 >nul
cd /d %~dp0
echo [screen_search] Debug mode. Hotkey: Ctrl+Alt+F / Ctrl+Alt+X
echo (Console stays open for tracebacks. Ctrl+C to quit. Admin recommended for global hotkeys.)
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0screen_search.py"
    goto :done
)

where python.exe >nul 2>&1
if %errorLevel% equ 0 (
    python.exe "%~dp0screen_search.py"
    goto :done
)

echo [screen_search] No Python found. Run setup.bat first, or install Python 3.10+.

:done
pause