@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found.
    echo Install Python and enable Add Python to PATH.
    pause
    exit /b 1
  )
  set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 goto :error
)

call ".venv\Scripts\activate.bat"

python -m pip install -U -r requirements.txt
if errorlevel 1 goto :error

start "" "http://127.0.0.1:8765/"
python app.py
goto :eof

:error
echo.
echo Failed to start.
pause
exit /b 1
