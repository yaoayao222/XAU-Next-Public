@echo off
chcp 65001 >nul
py -3.12 "%~dp0tooling\launch\manage.py" setup %*
if errorlevel 1 pause
