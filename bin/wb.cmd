@echo off
rem Runs the wb CLI from this checkout without installing it: the py launcher when present, else python.
setlocal
set "WB_REPO=%~dp0.."
set "PYTHONPATH=%WB_REPO%\sdk\python;%WB_REPO%\core;%WB_REPO%\host;%PYTHONPATH%"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 -m wb %*
) else (
  python -m wb %*
)
exit /b %ERRORLEVEL%
