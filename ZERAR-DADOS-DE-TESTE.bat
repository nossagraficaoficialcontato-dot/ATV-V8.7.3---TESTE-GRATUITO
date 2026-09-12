@echo off
title ATV DESIGN - ZERAR HOMOLOGACAO
cd /d "%~dp0"
echo.
echo ATENCAO: ISTO APAGA OS DADOS DE TESTE DA HOMOLOGACAO.
echo Usuarios, assets e configuracoes cadastrados durante os testes serao removidos.
echo.
set /p CONF=Digite APAGAR para continuar: 
if /I not "%CONF%"=="APAGAR" (
  echo Cancelado.
  pause
  exit /b 0
)
if exist "data" (
  rmdir /s /q "data"
)
echo.
echo Base de teste apagada.
echo Na proxima inicializacao a ATV criara uma base limpa.
pause
