@echo off
setlocal
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if "%~1"=="" (
  echo.
  echo   Usage:  run.bat  "data\input\your-inquiry.xlsx"  [more files...]  [-o output.xlsx]
  echo   Tip:    add  --hint "project no. 691-2610-26"  to help the model
  echo.
  exit /b 1
)
%PY% src\pipeline.py %*
pause
