@echo off
title ATV DESIGN - HOMOLOGACAO V8.7.3
cd /d "%~dp0"

echo.
echo ============================================================
echo      ATV DESIGN - HOMOLOGACAO V8.7.3
echo ============================================================
echo.
echo A V8.7.3 so abre o navegador DEPOIS de confirmar
echo que o servidor V8.7.3 realmente assumiu a porta 8000.
echo.

where py >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python nao foi encontrado.
  echo Instale Python 3.11 ou 3.12 e marque "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Criando ambiente da ATV...
  py -m venv .venv
  if errorlevel 1 (
    echo [ERRO] Nao foi possivel criar o ambiente virtual.
    pause
    exit /b 1
  )
) else (
  echo [1/3] Ambiente da ATV encontrado.
)

echo [2/3] Conferindo dependencias...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERRO] Falha ao instalar as dependencias.
  pause
  exit /b 1
)

set ATV_ENV=local
set ATV_COOKIE_SECURE=0
set ATV_ADMIN_PIN=246810
set ATV_ADMIN_KEY=AD!!

echo [3/3] Ligando a ATV V8.7.3...
echo.
echo Se outra ATV estiver aberta na porta 8000,
echo esta versao VAI AVISAR e NAO abrira o servidor antigo.
echo.
echo Para desligar corretamente, pressione CTRL+C nesta janela.
echo.

".venv\Scripts\python.exe" run_atv.py

echo.
echo ATV V8.7.3 desligada.
pause
