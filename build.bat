@echo off
cd /d "%~dp0"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Building pdftoy CLI executable (console)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --console --name pdftoy --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy.py

echo.
echo Done. Executables in dist\:
echo   pdftoy.exe      - CLI version (command-line, equivalent to "python pdftoy.py")
pause
