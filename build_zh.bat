@echo off
cd /d "%~dp0"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building pdftoy Chinese GUI executable (windowed)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --windowed --name pdftoy-gui-zh --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy_gui_zh.py

echo.
echo Building pdftoy Chinese CLI executable (console)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --console --name pdftoy-zh --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy_zh.py

echo.
echo Done. Executables in dist\:
echo   pdftoy-gui-zh.exe  - Chinese GUI version
echo   pdftoy-zh.exe      - Chinese CLI version
echo.
echo For the English build, run: build.bat
pause
