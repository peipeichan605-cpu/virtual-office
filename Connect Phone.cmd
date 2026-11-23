@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
where py.exe >nul 2>&1
if errorlevel 1 goto python
py -3 phone_setup_assistant.py
goto done
:python
python.exe phone_setup_assistant.py
:done
set "office_result=%errorlevel%"
pause
popd
exit /b %office_result%
