@echo off
setlocal
title Iniciar Reflexia local
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_local.ps1"
set "REFLEXIA_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Pulsa una tecla para cerrar esta ventana.
pause >nul
exit /b %REFLEXIA_EXIT_CODE%
