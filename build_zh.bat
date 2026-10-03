@echo off
cd /d "%~dp0"

REM Clean old build artifacts (PyInstaller safe-delete shim may block in-place deletion)
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building pdftoy executable (GUI + CLI, console mode)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --console --name pdftoy-zh --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy_zh.py

echo.
echo Done. Executable in dist\:
echo   pdftoy-zh.exe  - double-click to launch GUI (auto-hides its console window)
echo                - in terminal: pdftoy-zh input.pdf runs CLI (logs to terminal)
echo.
pause
