@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d %~dp0
echo ============================================================
echo   screen_search - Setup local virtual environment (.venv)
echo ============================================================
echo.

set "PY_EXE="

REM 1) 项目自带 Python (LOCALAPPDATA)
if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Python\bin\python.exe"
    goto :got_py
)

REM 2) py launcher
where py.exe >nul 2>&1
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`py -3 -c "import sys;print(sys.executable)"`) do set "PY_EXE=%%P"
    if defined PY_EXE goto :got_py
)

REM 3) python.exe on PATH
where python.exe >nul 2>&1
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`where python.exe`) do (
        set "PY_EXE=%%P"
        goto :got_py
    )
)

echo [-] No Python found. Please install Python 3.10+ from https://www.python.org
echo     (Make sure to check "Add python.exe to PATH" during install.)
pause
exit /b 1

:got_py
echo [+] Using Python: !PY_EXE!
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
    echo [i] .venv already exists, reusing.
    goto :install_deps
)
echo [+] Creating .venv ...
"!PY_EXE!" -m venv "%~dp0.venv"
if errorlevel 1 (
    echo [-] Failed to create .venv.
    pause
    exit /b 2
)

:install_deps
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

echo.
echo [+] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip

echo.
echo [+] Installing dependencies ...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [!] Normal install failed - retrying rapidocr-onnxruntime with --ignore-requires-python ...
    echo     ^(rapidocr-onnxruntime 1.2.3 installer restricts Python^<=3.10, but it runs fine on 3.11+^)
    echo.
    "%VENV_PY%" -m pip install --ignore-requires-python "rapidocr-onnxruntime==1.2.3"
    if errorlevel 1 (
        echo.
        echo [!] rapidocr-onnxruntime install failed - code will fallback to winocr.
        echo     Retrying rest of requirements without rapidocr ...
        "%VENV_PY%" -m pip install Pillow numpy keyboard winocr comtypes uiautomation pystray
        if errorlevel 1 (
            echo [-] Base dependency install still failed. See errors above.
            pause
            exit /b 3
        )
    ) else (
        REM rapidocr 装成功，再补装其余依赖
        "%VENV_PY%" -m pip install Pillow numpy keyboard winocr comtypes uiautomation pystray
    )
)

echo.
echo ============================================================
echo [+] Setup complete
echo.
echo   Launch:    run_screen_search.bat            (silent)
echo   Debug:     run_screen_search_debug.bat      (console)
echo   Autostart: install_autostart.bat            (needs admin)
echo ============================================================
echo.
pause
endlocal