@echo off
cd /d "%~dp0"
REM Run in GUI mode (no console window). Running "pdftoy" without arguments launches the GUI.
if exist ".venv\Scripts\pythonw.exe" (
    ".venv\Scripts\pythonw.exe" pdftoy.py
) else (
    ".venv\Scripts\python.exe" pdftoy.py
)
