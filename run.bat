@echo off
cd /d "%~dp0"
python -m adaptivedns --demo --open %*
pause
