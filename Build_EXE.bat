@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3 -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt "pyinstaller>=6,<7"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name webpCam --icon webpcam.ico --add-data "webpcam.ico;." webpcam.py
if errorlevel 1 goto failed
echo Created: dist\webpCam.exe
pause
exit /b 0
:failed
echo Build failed. Python 3.11 or newer with Tk is required. Check the error above.
pause
exit /b 1
