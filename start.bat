@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python app.py
) else (
    start "" pythonw app.py
)
