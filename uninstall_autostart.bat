@echo off
chcp 65001 >nul
echo ============================================================
echo   screen_search - Remove logon autostart task
echo ============================================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [!] This script must run as administrator.
    echo [!] Right-click this file -^> "Run as administrator".
    echo.
    pause
    exit /b 1
)

schtasks /Delete /TN "ScreenSearch" /F

if %errorLevel% equ 0 (
    echo.
    echo [+] Autostart task removed.
) else (
    echo.
    echo [-] Failed (task may not exist). errorlevel=%errorLevel%
)
echo.
pause
