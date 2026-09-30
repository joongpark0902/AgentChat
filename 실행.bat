@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set URL=http://127.0.0.1:8780

rem --- Find a real Python (not the Microsoft Store alias): py launcher -> PATH -> default install folder
set "PY="
for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('where python 2^>nul ^| findstr /v /i WindowsApps') do if not defined PY set "PY=%%p"
if not defined PY for /f "delims=" %%p in ('dir /b /s /o-n "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
  echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and run this file again.
  pause
  exit /b 1
)

rem --- First run: install required packages
"%PY%" -c "import fastapi, uvicorn, yaml, openpyxl, docx, pptx" 2>nul
if errorlevel 1 (
  echo Installing required packages. This runs only once...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Package installation failed. See the messages above.
    pause
    exit /b 1
  )
)

rem --- Open in Chrome instead of the default browser (Edge). If Chrome is missing, use the default browser.
set "CHROME="
for /f "tokens=2,*" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| find "REG_SZ"') do set "CHROME=%%b"
if not defined CHROME if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if defined CHROME (start "" "%CHROME%" %URL%) else (start "" %URL%)
"%PY%" -m app.server
pause
