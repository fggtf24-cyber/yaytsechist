@echo off
rem Собирает Яйцечист.exe (нужны: pip install pywebview pystray pillow pyinstaller)
rem пути абсолютные: при --specpath относительные считаются от папки spec
chcp 65001 >nul
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name Яйцечист --icon "%~dp0icon.ico" ^
  --add-data "%~dp0ui.html;." --add-data "%~dp0fonts;fonts" --add-data "%~dp0icon.ico;." ^
  --hidden-import pystray._win32 --distpath "%~dp0." ^
  --exclude-module numpy --exclude-module setuptools --exclude-module pkg_resources --exclude-module yaml ^
  --exclude-module PIL.AvifImagePlugin --exclude-module PIL.WebPImagePlugin --exclude-module PIL.ImageCms ^
  --workpath "%TEMP%\gameboost-build" --specpath "%TEMP%\gameboost-build" "%~dp0gameboost.py"
pause
