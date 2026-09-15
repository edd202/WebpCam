@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run
py -3 --version >nul 2>&1
if errorlevel 1 goto missing
py -3 -m venv .venv
if errorlevel 1 goto failed
:run
".venv\Scripts\python.exe" -c "import PIL; assert PIL.__version__ == '12.3.0'" >nul 2>&1
if not errorlevel 1 goto launch
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
:launch
".venv\Scripts\python.exe" webpcam.py
if errorlevel 1 goto failed
exit /b 0
:missing
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/
echo Include the Python launcher and Tcl/Tk during installation.
pause
exit /b 1
:failed
echo webpCam could not start. Please check the error above.
pause
exit /b 1
