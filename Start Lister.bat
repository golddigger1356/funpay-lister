@echo off
setlocal
cd /d "%~dp0"
title FunPay Lister
where py >nul 2>nul
if not errorlevel 1 (
    py -3 app.py
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python app.py
    goto finished
)
echo Python is needed to open this tool.
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo Then double-click Start Lister.bat again.
:finished
pause
