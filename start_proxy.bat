@echo off
setlocal

cd /d "%~dp0"

set "PROXY_HOST=127.0.0.1"
set "PROXY_PORT=8188"
set "LOCAL_COMFY_URL=http://127.0.0.1:8189"

echo.
echo Starting RunPod ComfyUI proxy...
echo Repo: %CD%
echo UI:   http://%PROXY_HOST%:%PROXY_PORT%
echo Settings: http://%PROXY_HOST%:%PROXY_PORT%/runpod/settings
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python or run this from a terminal where python is available.
  echo.
  pause
  exit /b 1
)

python -m pip show fastapi >nul 2>nul
if errorlevel 1 (
  echo Installing proxy requirements...
  python -m pip install -r requirements-proxy.txt
  if errorlevel 1 (
    echo.
    echo Failed to install requirements.
    pause
    exit /b 1
  )
)

echo Starting server. Close this window to stop the proxy.
echo.
python serverless_proxy.py

echo.
echo Proxy stopped.
pause
