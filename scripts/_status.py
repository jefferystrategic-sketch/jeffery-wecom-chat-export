#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时导出的状态面板：看它是不是在跑、最近导出了什么。

  _status.py          每 2 秒刷新一次，关窗口或 Ctrl+C 退出
  _status.py --once   只打印一次（测试用）

只读：读计划任务状态、logs/ 下的心跳和日志，不碰任务也不触发导出。
「在跑」的依据是 logs/last_check.txt——后台每检查一次就写一次时间，
--watch 模式下几秒一更新；超过 2 分钟没更新就说明没在跑。
"""
import io
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)        # python -I 下要补，才 import 得到同目录模块
from _schedule_task import TASK, schtasks  # noqa: E402
from _scheduled_export import wxwork_running  # noqa: E402

LOG_DIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOG_DIR, "scheduled_export.log")
HEARTBEAT = os.path.join(LOG_DIR, "last_check.txt")
STALE = 120                     # 心跳超过这么多秒没更新，判定为没在跑


def task_info():
    rc, out = schtasks("/Query", "/TN", TASK, "/FO", "LIST")
    if rc != 0:
        return None
    # 字段名随系统语言变（下次运行时间 / Next Run Time），按行序取值不认字段名
    vals = [ln.split(":", 1)[1].strip() for ln in out.splitlines() if ":" in ln]
    return vals


def heartbeat_age():
    try:
        with io.open(HEARTBEAT, encoding="utf-8") as f:
            t = datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
        return t, (datetime.now() - t).total_seconds()
    except Exception:
        return None, None


def log_lines():
    try:
        with io.open(LOG, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def render():
    out = []
    now = datetime.now()
    out.append("=" * 64)
    out.append(f"  企业微信定时导出 · 状态        {now:%Y-%m-%d %H:%M:%S}")
    out.append("=" * 64)
    out.append("")

    info = task_info()
    last, age = heartbeat_age()
    if info is None:
        verdict = "[未开启]  计划任务不存在，双击「开启定时导出.bat」开启"
    elif age is None:
        verdict = "[等待中]  任务已登记，还没检查过（开启后约 1 分钟开始）"
    elif age <= STALE:
        verdict = "[运行中]  后台正在按间隔检查"
    else:
        verdict = f"[没在跑]  已经 {int(age // 60)} 分钟没检查了（没登录 / 关机 / 任务被禁用？）"
    out.append(f"  {verdict}")
    out.append("")
    if last:
        out.append(f"  最近一次检查    {last:%H:%M:%S}（{int(age)} 秒前）")
    out.append(f"  企业微信        {'运行中' if wxwork_running() else '没有运行（这时只检查、不导出）'}")
    if info:
        out.append(f"  计划任务        {info[-2] if len(info) >= 2 else ''}  ·  下次触发 {info[-3] if len(info) >= 3 else ''}")

    lines = log_lines()
    today = now.strftime("%Y-%m-%d")
    done = [ln for ln in lines if ln.startswith(f"[{today}") and "导出完成" in ln]
    added = sum(int(m.group(1)) for ln in done for m in [re.search(r"新增 (\d+) 条", ln)] if m)
    out.append(f"  今天            导出 {len(done)} 次，新增 {added} 条消息")
    out.append("")
    out.append("  最近的记录（只有真正导出、状态变化或出错时才记）：")
    out.append("  " + "-" * 60)
    for ln in lines[-10:] or ["（还没有记录）"]:
        out.append("  " + ln)
    out.append("")
    out.append(f"  完整日志：{LOG}")
    out.append("  每 2 秒刷新；关掉这个窗口不影响后台导出。")
    return "\n".join(out)


def main():
    if "--once" in sys.argv:
        print(render())
        return 0
    try:
        while True:
            text = render()
            os.system("cls")
            print(text, flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
