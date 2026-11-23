@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
if defined OFFICE_CONFIG goto configured
set "OFFICE_CONFIG=%~dp0.office\config.json"
:configured
if not exist "%OFFICE_CONFIG%" goto missing
where py.exe >nul 2>&1
if errorlevel 1 goto python
rem Resolve the selected interpreter without keeping the py.exe job around Office.
set "office_python="
for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)"') do set "office_python=%%I"
if not defined office_python goto python
"%office_python%" office.py start --open-migrated
goto done
:python
python.exe office.py start --open-migrated
:done
set "office_result=%errorlevel%"
if not "%office_result%"=="0" pause
popd
exit /b %office_result%
:missing
echo Office configuration was not found. Open the installation you normally use.
pause
popd
exit /b 1
