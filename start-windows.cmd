@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set "STUDY_UV=%USERPROFILE%\.local\bin\uv.exe"
if exist "%STUDY_UV%" goto ready
where uv >nul 2>nul
if not errorlevel 1 (
  set "STUDY_UV=uv"
  goto ready
)
echo Installing uv from the official Astral installer...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression"
if errorlevel 1 goto failed
if not exist "%STUDY_UV%" goto failed
:ready
echo Starting Chaoxin. First launch downloads Python and dependencies.
"%STUDY_UV%" run --frozen python run.py %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. Check your internet connection and the message above.
echo uv installation guide: https://docs.astral.sh/uv/getting-started/installation/
pause
exit /b 1
