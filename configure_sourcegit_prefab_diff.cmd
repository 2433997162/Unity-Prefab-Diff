@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if defined PYTHON (
    "%PYTHON%" "%SCRIPT_DIR%configure_sourcegit_prefab_diff.py" "%SCRIPT_DIR%"
) else (
    python "%SCRIPT_DIR%configure_sourcegit_prefab_diff.py" "%SCRIPT_DIR%"
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo SourceGit Unity Prefab diff renderer configured.
    echo Restart SourceGit or reopen the diff tab if the old renderer is still cached.
) else (
    echo SourceGit Unity Prefab diff renderer configuration failed.
)
echo.
pause
exit /b %EXIT_CODE%
