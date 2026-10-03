@echo off
cd /d "%~dp0"

REM Clean old build artifacts (PyInstaller safe-delete shim may block in-place deletion)
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building pdftoy executable (windowed: zero console window, GUI + CLI)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --windowed --name pdftoy-zh --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy_zh.py

echo.
echo Done. Executable in dist\:
echo   pdftoy.exe  - double-click: GUI only, NO console window at all
echo                - in terminal: pdftoy input.pdf runs CLI (logs to terminal)
echo                - without terminal (drag-drop a PDF): a log console opens
echo                  automatically and closes when done
echo.
pause
