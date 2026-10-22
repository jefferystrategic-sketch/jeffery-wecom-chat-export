@echo off
setlocal
title 开启定时导出

set "SCRIPTS=%~dp0scripts"

echo.
echo ================================================================
echo   开启定时导出（企业微信聊天记录）
echo ================================================================
echo.
echo   开启后电脑会按你设的间隔在后台自动导出，不弹窗。
echo   只在你登录着电脑、企业微信开着的时候才会真的导出；
echo   企微没开的那几轮会自动跳过。结果并入 scripts\wxwork_archive\
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo   [X] 没有找到 Python。先看「使用说明.txt」装好 Python。
    echo.
    pause
    exit /b 1
)

REM 依赖和账号选择都在手动导出里完成；先手动跑通一次，确认账号选对了再定时
python -c "import Crypto" >nul 2>&1
if errorlevel 1 goto need_manual
if not exist "%SCRIPTS%\config.json" goto need_manual

echo   当前状态：
python -I "%SCRIPTS%\_schedule_task.py" status

set "SEC=60"
echo   每隔多少秒检查一次？
echo     只有企微里真的来了新消息才会导出，没新消息时只看一眼就结束，
echo     所以填小一点也不费电脑。例：5 = 每 5 秒，60 = 每分钟，3600 = 每小时。
echo.
set /p "SEC=  输入秒数，直接回车用 60："
echo.
python -I "%SCRIPTS%\_schedule_task.py" install %SEC%
echo.
echo   想关掉时双击「关闭定时导出.bat」。
echo.
pause
exit /b 0

:need_manual
echo   [X] 请先双击「一键导出聊天记录.bat」手动跑通一次，
echo       它会装好依赖、选好要导出的企微账号。跑通后再来开启定时。
echo.
pause
exit /b 1
