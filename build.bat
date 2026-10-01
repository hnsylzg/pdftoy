@echo off
cd /d "%~dp0"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building pdftoy GUI executable (windowed)...
".venv\Scripts\pyinstaller.exe" --add-data=".venv/Lib/site-packages/sv_ttk:sv_ttk" --upx-dir .venv\Scripts  --onefile --windowed --name pdftoy-gui --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy_gui.py

echo.
echo Building pdftoy CLI executable (console)...
".venv\Scripts\pyinstaller.exe" --upx-dir .venv\Scripts --onefile --console --name pdftoy --icon=pdftoy.ico --exclude-module pandas --exclude-module numpy --exclude-module fontTools --exclude-module PIL --exclude-module cppyy --exclude-module mupdf_cppyy --exclude-module pymupdf_table pdftoy.py

echo.
echo Done. Executables in dist\:
echo   pdftoy-gui.exe  - English GUI version
echo   pdftoy.exe      - English CLI version
echo.
echo For the Chinese build, run: build_zh.bat
pause
