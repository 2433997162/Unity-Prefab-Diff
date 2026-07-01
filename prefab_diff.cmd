@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if defined PYTHON (
    "%PYTHON%" "%SCRIPT_DIR%prefab_diff.py" %*
) else (
    python "%SCRIPT_DIR%prefab_diff.py" %*
)
