@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Build from the repository root, even when this file is launched by double-click.
set "REPO_ROOT=%~dp0"
pushd "%REPO_ROOT%" >nul
if errorlevel 1 (
    echo Could not enter the repository directory: "%REPO_ROOT%"
    pause
    exit /b 1
)

rem Find a system Python 3.11 or newer. Python is needed to BUILD only;
rem the resulting executable does not need Python installed to RUN.
set "SYSTEM_PYTHON="
set "SYSTEM_PYTHON_ARGS="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "SYSTEM_PYTHON=py"
        set "SYSTEM_PYTHON_ARGS=-3"
    )
)
if not defined SYSTEM_PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "SYSTEM_PYTHON=python"
    )
)
if not defined SYSTEM_PYTHON (
    echo BUILD FAILED
    echo Python 3.11 or newer is required to BUILD YES_RS485.exe.
    echo Install Python 3.11+ from python.org, then run this file again.
    popd
    pause
    exit /b 1
)

set "BUILD_VENV=%REPO_ROOT%.build-venv"
set "BUILD_PYTHON=%BUILD_VENV%\Scripts\python.exe"
if exist "%BUILD_VENV%" if not exist "%BUILD_PYTHON%" (
    rmdir /s /q "%BUILD_VENV%"
)
if not exist "%BUILD_PYTHON%" (
    echo Creating isolated build environment in "%BUILD_VENV%" ...
    "%SYSTEM_PYTHON%" !SYSTEM_PYTHON_ARGS! -m venv "%BUILD_VENV%"
    if errorlevel 1 goto :failed
)

echo Upgrading pip in the build environment ...
"%BUILD_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo Installing the project and PyInstaller build dependency ...
"%BUILD_PYTHON%" -m pip install -e ".[build]"
if errorlevel 1 goto :failed

rem Clean only this script's prior packaging output. A known-good root EXE
rem is not removed until a complete replacement has been built and staged.
set "BUILD_ROOT=%REPO_ROOT%.build\YES_RS485"
set "DIST_PATH=%BUILD_ROOT%\dist"
set "WORK_PATH=%BUILD_ROOT%\work"
set "STAGED_EXE=%DIST_PATH%\YES_RS485.exe"
set "PUBLISH_EXE=%REPO_ROOT%YES_RS485.exe.__new"
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
if exist "%PUBLISH_EXE%" del /q "%PUBLISH_EXE%"
mkdir "%DIST_PATH%" >nul 2>&1
if errorlevel 1 goto :failed
mkdir "%WORK_PATH%" >nul 2>&1
if errorlevel 1 goto :failed

echo Building YES_RS485.exe ...
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean --distpath "%DIST_PATH%" --workpath "%WORK_PATH%" "%REPO_ROOT%packaging\YES_RS485.spec"
if errorlevel 1 goto :failed

if not exist "%STAGED_EXE%" (
    echo PyInstaller reported success but did not produce the expected executable.
    goto :failed
)
for %%F in ("%STAGED_EXE%") do if %%~zF LEQ 0 (
    echo The staged executable is empty.
    goto :failed
)

rem Publish only after the complete one-file build has succeeded. Staging the
rem sibling first avoids truncating an existing root executable on failure.
copy /Y "%STAGED_EXE%" "%PUBLISH_EXE%" >nul
if errorlevel 1 (
    echo Could not stage the new executable at "%PUBLISH_EXE%".
    goto :failed
)
move /Y "%PUBLISH_EXE%" "%REPO_ROOT%YES_RS485.exe" >nul
if errorlevel 1 (
    echo Could not replace "%REPO_ROOT%YES_RS485.exe" with the new build.
    echo The staged file remains at "%PUBLISH_EXE%".
    goto :failed
)
for %%F in ("%REPO_ROOT%YES_RS485.exe") do if %%~zF LEQ 0 (
    echo The published executable is empty.
    goto :failed
)

echo.
echo ========================================
echo BUILD COMPLETE
echo YES_RS485.exe
echo ========================================
echo Output: "%REPO_ROOT%YES_RS485.exe"
echo Temporary PyInstaller files: "%BUILD_ROOT%"
popd
pause
exit /b 0

:failed
echo.
echo ========================================
echo BUILD FAILED
echo ========================================
echo Review the error output above. Temporary build files are in:
echo   "%BUILD_ROOT%"
popd
pause
exit /b 1
