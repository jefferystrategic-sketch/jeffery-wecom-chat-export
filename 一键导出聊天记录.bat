@echo off
setlocal enabledelayedexpansion
title 企业微信聊天记录导出

set "ROOT=%~dp0"
set "SCRIPTS=%ROOT%scripts"
set "PICKER=%SCRIPTS%\_pick_account.py"

REM wce.py 会打印 [OK] 这类字符，但没给自己设 UTF-8 输出；
REM 中文 Windows 控制台默认 GBK，不设这个会直接 UnicodeEncodeError 崩掉。
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo.
echo ================================================================
echo   企业微信聊天记录导出
echo ================================================================
echo.
echo   导出的是你本机已登录账号的聊天记录，全程在本地完成，
echo   不上传任何服务器。结果含同事与客户对话，请妥善保管。
echo.

REM ---------- 1. Python ----------
echo [1/5] 检查 Python ...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [X] 没有找到 Python。
    echo.
    echo   请先安装：https://www.python.org/downloads/
    echo   安装时务必勾选 "Add Python to PATH"，装完重新双击本脚本。
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo       %%v

REM ---------- 2. 依赖 ----------
echo [2/5] 检查依赖 ...
python -c "import Crypto" >nul 2>&1
if errorlevel 1 (
    echo       正在安装 pycryptodome ...
    python -m pip install --quiet --disable-pip-version-check pycryptodome >nul 2>&1
    python -c "import Crypto" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   [X] 依赖安装失败。请手动执行：
        echo       python -m pip install pycryptodome
        echo.
        pause
        exit /b 1
    )
)
python -c "import zstandard" >nul 2>&1
if errorlevel 1 (
    echo       正在安装 zstandard ...
    python -m pip install --quiet --disable-pip-version-check zstandard >nul 2>&1
)
echo       依赖就绪

REM ---------- 3. 企业微信 ----------
echo [3/5] 检查企业微信 ...
REM 绝对路径调系统命令：装了 Git Bash 的机器 PATH 里有同名 find，会把企微误判成没开
%SystemRoot%\System32\tasklist.exe /FI "IMAGENAME eq WXWork.exe" 2>nul | %SystemRoot%\System32\findstr.exe /I "WXWork.exe" >nul
if errorlevel 1 (
    echo.
    echo   [X] 企业微信没有在运行。
    echo.
    echo   密钥要从企业微信进程里读取，所以必须先打开并登录企业微信，
    echo   保持它开着，再重新双击本脚本。
    echo.
    pause
    exit /b 1
)
echo       企业微信正在运行

REM ---------- 4. 选账号、写配置 ----------
echo [4/5] 定位聊天数据 ...
python -I "%PICKER%"
if errorlevel 1 (
    echo.
    echo   [X] 没能定位到聊天数据目录。
    echo.
    pause
    exit /b 1
)

REM ---------- 5. 导出 ----------
echo [5/5] 开始导出，这一步可能要几分钟 ...
echo.
python -I "%SCRIPTS%\_run_export.py" --formats html,json,csv
set "RC=%errorlevel%"

if "%RC%"=="0" (
    echo.
    echo       正在并入历史归档 ...
    python -I "%SCRIPTS%\_archive_merge.py"
)

echo.
if "%RC%"=="0" (
    echo ================================================================
    echo   导出完成
    echo ================================================================
    echo.
    echo   全部历史：%SCRIPTS%\wxwork_archive\
    echo     每次跑都往里加新消息，只增不删，看这个就够了。
    echo.
    echo   本次快照：%SCRIPTS%\wxwork_export_日期时间\
    echo     只有这一次导到的内容，想看单次结果才需要翻它。
    echo.
    echo     .html   双击用浏览器看，仿微信气泡排版
    echo     .csv    用 Excel 打开，适合筛选统计
    echo     .json   给程序处理用
    echo.
    if exist "%SCRIPTS%\wxwork_archive" start "" "%SCRIPTS%\wxwork_archive"
) else (
    echo ================================================================
    echo   导出失败
    echo ================================================================
    echo.
    echo   把上面的报错内容截图发给技术同事。常见原因：
    echo     - 企业微信中途被关掉了
    echo     - 企业微信刚更新过，密钥提取方式变了
    echo     - 本机有多个企微账号，选错了
    echo.
)
pause
exit /b %RC%
