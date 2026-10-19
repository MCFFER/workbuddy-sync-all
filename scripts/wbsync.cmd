@echo off
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "PY="

if defined WBSYNC_PYTHON call :try_py "%WBSYNC_PYTHON%"

pushd "%USERPROFILE%"
if not errorlevel 1 (
  pushd ".workbuddy"
  if not errorlevel 1 (
    for /d %%d in ("binaries\python\versions\*") do call :try_py "%%~fd\python.exe"
    popd
  )
  popd
)

call :try_py "py"
call :try_py "python"
call :try_py "python3"

if not defined PY (
  echo Cannot find a usable Python interpreter
  echo Set WBSYNC_PYTHON to python.exe full path
  exit /b 1
)

"%PY%" "%SCRIPT_DIR%wbsync.py" %*
exit /b %ERRORLEVEL%

:try_py
if defined PY goto :eof
if "%~1"=="" goto :eof
echo %~1 | find /i "WindowsApps" >nul
if not errorlevel 1 goto :eof
if exist "%~1" for %%s in ("%~1") do if %%~zs LSS 1024 goto :eof
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info[0]>=3 else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof
