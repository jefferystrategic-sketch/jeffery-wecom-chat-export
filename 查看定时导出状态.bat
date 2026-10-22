@echo off
setlocal
title 定时导出状态

REM 只读查看，关掉窗口不影响后台导出
python -I "%~dp0scripts\_status.py"
