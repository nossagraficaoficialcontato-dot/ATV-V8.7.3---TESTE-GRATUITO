@echo off
title ATV DESIGN - VERIFICAR V8.7.3
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" verificar_v873.py
) else (
  py verificar_v873.py
)
echo.
pause
