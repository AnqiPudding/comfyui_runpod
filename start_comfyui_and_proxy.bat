@echo off
setlocal

cd /d "%~dp0"

set "PROXY_HOST=127.0.0.1"
set "PROXY_PORT=8188"
set "COMFY_HOST=127.0.0.1"
set "COMFY_PORT=8189"
set "LOCAL_COMFY_URL=http://%COMFY_HOST%:%COMFY_PORT%"
set "COMFY_ROOT=D:\antigravity\comfy_api\ComfyUI_windows_portable\ComfyUI"
set "COMFY_PYTHON=D:\antigravity\comfy_api\ComfyUI_windows_portable\python_embeded\python.exe"

echo.
echo Starting local ComfyUI and RunPod proxy...
echo ComfyUI:  http://%COMFY_HOST%:%COMFY_PORT%
echo Proxy UI: http://%PROXY_HOST%:%PROXY_PORT%
echo Settings: http://%PROXY_HOST%:%PROXY_PORT%/runpod/settings
echo.

if not exist "%COMFY_PYTHON%" (
  echo Could not find ComfyUI embedded Python:
  echo %COMFY_PYTHON%
  echo.
  pause
  exit /b 1
)

if not exist "%COMFY_ROOT%\main.py" (
  echo Could not find ComfyUI main.py:
  echo %COMFY_ROOT%\main.py
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %COMFY_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo Starting local ComfyUI on port %COMFY_PORT%...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', '\"%COMFY_PYTHON%\" main.py --listen %COMFY_HOST% --port %COMFY_PORT% --disable-auto-launch' -WorkingDirectory '%COMFY_ROOT%' -WindowStyle Minimized"
  echo Waiting for ComfyUI to listen...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddMinutes(3); while((Get-Date) -lt $deadline){ if(Get-NetTCPConnection -LocalPort %COMFY_PORT% -State Listen -ErrorAction SilentlyContinue){ exit 0 }; Start-Sleep -Seconds 2 }; exit 1"
  if errorlevel 1 (
    echo.
    echo ComfyUI did not start listening on port %COMFY_PORT% within 3 minutes.
    echo Check the "Local ComfyUI 8189" window for the real error.
    pause
    exit /b 1
  )
) else (
  echo Local ComfyUI is already listening on port %COMFY_PORT%.
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH for the proxy server.
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
    echo Failed to install proxy requirements.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %PROXY_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
  echo.
  echo Proxy is already listening on port %PROXY_PORT%.
  echo Open: http://%PROXY_HOST%:%PROXY_PORT%
  echo.
  pause
  exit /b 0
)

echo.
echo Starting proxy. Keep this window open while using the UI.
echo Open: http://%PROXY_HOST%:%PROXY_PORT%
echo.
python serverless_proxy.py

echo.
echo Proxy stopped. Local ComfyUI may still be running in its own window.
pause
