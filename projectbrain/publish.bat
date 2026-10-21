@echo off
setlocal
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do set "%%A=%%B"
python -m twine upload --skip-existing "%~dp0dist\*"
