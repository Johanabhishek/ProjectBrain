@echo off
setlocal
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
    if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
)
python -m twine upload "%~dp0dist\*"
