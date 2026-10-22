#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 Windows 计划任务里登记 / 取消 / 查看定时导出。

用法：
  _schedule_task.py install <间隔秒数>

间隔不到 60 秒时：计划任务最短只能每分钟触发一次，所以改成每分钟拉起
_scheduled_export.py --watch <秒>，由它在这一分钟里按秒检查。
间隔 60 秒及以上：按分钟取整，每次触发跑一轮。
  _schedule_task.py uninstall
  _schedule_task.py status

为什么用 XML 登记而不是 schtasks /SC MINUTE 一行搞定：
  - 那样建出来的任务默认「用电池时不启动」，笔记本拔了电源就不跑了
  - 默认不处理错过的轮次；XML 可以设「错过了就补跑」(关机开机后能接上)
  - 默认允许多个实例并发；XML 设 IgnoreNew，上一轮没完就不起新的
  - /TR 有 261 字符上限，同事的目录一深就超
"""
import io
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

TASK = "企微聊天记录定时导出"
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "_scheduled_export.py")
LOG = os.path.join(HERE, "logs", "scheduled_export.log")
SCHTASKS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")


def schtasks(*args):
    p = subprocess.run([SCHTASKS, *args], capture_output=True)
    # schtasks 按控制台代码页输出（中文系统是 GBK），mbcs 跟着系统走
    out = (p.stdout + p.stderr).decode("mbcs", errors="replace").strip()
    return p.returncode, out


def pythonw():
    """用 pythonw 跑就不会每次弹一个黑框；找不到就退回 python。"""
    w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return w if os.path.exists(w) else sys.executable


def task_xml(seconds):
    if seconds < 60:
        minutes, extra = 1, f" --watch {seconds}"
    else:
        minutes, extra = max(1, round(seconds / 60)), ""
    start = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:00")
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    e = escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>每 {human(seconds)}检查一次企业微信聊天记录，有新消息就并入 {e(os.path.join(HERE, "wxwork_archive"))}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(pythonw())}</Command>
      <Arguments>-I "{e(SCRIPT)}"{extra}</Arguments>
      <WorkingDirectory>{e(HERE)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def human(seconds):
    if seconds < 60:
        return f"{seconds} 秒"
    m = max(1, round(seconds / 60))
    return f"{m // 60} 小时" if m % 60 == 0 else f"{m} 分钟"


def install(seconds):
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with io.open(path, "w", encoding="utf-16") as f:
            f.write(task_xml(seconds))
        rc, out = schtasks("/Create", "/TN", TASK, "/XML", path, "/F")
    finally:
        os.remove(path)
    if rc != 0:
        print(f"      [X] 登记失败：{out}")
        return 1
    print(f"      已开启：每 {human(seconds)}检查一次，有新消息才导出。约 1 分钟后开始")
    return status()


def uninstall():
    rc, out = schtasks("/Delete", "/TN", TASK, "/F")
    if rc != 0:
        print("      本来就没有开启定时导出")
        return 0
    print("      已关闭定时导出。已经导出的归档原样保留。")
    return 0


def status():
    rc, out = schtasks("/Query", "/TN", TASK, "/FO", "LIST")
    if rc != 0:
        print("      当前没有开启定时导出")
        return 0
    print()
    for line in out.splitlines():
        if line.strip():
            print("      " + line.strip())
    print()
    print(f"      运行日志：{LOG}")
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "install":
        try:
            seconds = int(args[1])
        except (IndexError, ValueError):
            print("      [X] 间隔要填整数秒")
            return 1
        if not 1 <= seconds <= 30 * 86400:
            print("      [X] 间隔要在 1 秒到 30 天（2592000 秒）之间")
            return 1
        return install(seconds)
    if cmd == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    sys.exit(main())
