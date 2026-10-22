@echo off
setlocal
title 关闭定时导出

echo.
python -I "%~dp0scripts\_schedule_task.py" uninstall
echo.
pause
