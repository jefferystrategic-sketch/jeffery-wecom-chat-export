#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时任务调用的无人值守导出。

  _scheduled_export.py              跑一轮（计划任务间隔 >= 1 分钟时用）
  _scheduled_export.py --watch 5    每 5 秒查一次，持续约 55 秒后退出

为什么 --watch 要「跑 55 秒就退出」而不是常驻：计划任务的间隔最短 1 分钟，
所以由它每分钟拉起一次、进程自己在这一分钟里按秒检查。进程崩了、电脑睡眠
醒来、重新登录，下一分钟都会被重新拉起，比常驻进程可靠，也不用另装工具。

和双击 bat 走的是同一套流程（选账号 → _run_export → _archive_merge），区别：
  - 先看企微的消息库文件有没有变，没变就什么都不做（几毫秒）。
    绝大多数轮次是这种，所以秒级检查也几乎不占资源
  - 没有窗口、不暂停、不打开文件夹；只有真正导出或出错时才写日志
    logs/scheduled_export.log，「没变化」不写，否则一天上万行
  - 上一轮没跑完就跳过本轮（防重入）。企微重启后要重提密钥 ~45 秒
  - 企微没开就跳过，不算失败——下班关了企微很正常
  - 只并归档、不留快照，否则每跑一次多一个 wxwork_export_* 文件夹

账号沿用 config.json 里已经选好的（首次双击 bat 时定下的）；
没有配置时自动选最近在用的那个，不会卡在等输入上。
"""
import io
import json
import msvcrt
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOG_DIR, "scheduled_export.log")
LOCK = os.path.join(LOG_DIR, ".running.lock")
SEEN = os.path.join(LOG_DIR, ".last_seen.json")     # 上次导出时消息库的样子
STATE = os.path.join(LOG_DIR, ".last_state")        # 上次写进日志的状态，防刷屏
HEARTBEAT = os.path.join(LOG_DIR, "last_check.txt")  # 最近一次检查的时间，给人看「还活着」
LOG_MAX = 2 * 1024 * 1024          # 超过 2MB 轮转一次，只留一份旧的
WATCH_SPAN = 55                    # --watch 模式每次存活的秒数，要小于计划任务的 1 分钟间隔

# 新消息先写进 -wal，企微隔一阵才合并进 .db；两个都看，哪个变了都算
WATCH_FILES = ("message.db", "message.db-wal")

# pythonw 跑子进程时不弹黑框
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 子进程必须用控制台版 python.exe，不能沿用本进程的 pythonw.exe：
# pythonw 没有控制台，它的子孙里再起 tasklist 这类命令行程序（上游 wce.py
# 每次导出都会起一次），Windows 会给它新开一个控制台窗口，就是导出时一闪的黑框。
# python.exe + CREATE_NO_WINDOW 会拿到一个隐藏的控制台，子孙进程都沿用它。
CHILD_PY = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.exists(CHILD_PY):
    CHILD_PY = sys.executable


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with io.open(LOG, "a", encoding="utf-8") as f:
        for line in str(msg).rstrip().splitlines() or [""]:
            f.write(f"[{stamp}] {line}\n")


def log_state(state, msg):
    """同一状态只记一次：企微整晚没开，不该每 5 秒记一行。"""
    try:
        with io.open(STATE, encoding="utf-8") as f:
            if f.read() == state:
                return
    except OSError:
        pass
    log(msg)
    with io.open(STATE, "w", encoding="utf-8") as f:
        f.write(state)


def rotate_log():
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def wxwork_running():
    # 绝对路径：装了 Git Bash 的机器 PATH 里的同名工具会捣乱
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe")
    try:
        out = subprocess.run([exe, "/FI", "IMAGENAME eq WXWork.exe", "/NH"],
                             capture_output=True, creationflags=NO_WINDOW).stdout
    except OSError:
        return False
    return b"wxwork.exe" in out.lower()


def db_dir():
    try:
        with io.open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            d = json.load(f).get("wxwork_db_dir", "")
        return d if os.path.isdir(d) else None
    except Exception:
        return None


def fingerprint(data):
    """消息库文件的修改时间和大小。只 stat 不读内容，几毫秒。"""
    fp = {}
    for name in WATCH_FILES:
        try:
            st = os.stat(os.path.join(data, name))
            fp[name] = [st.st_mtime_ns, st.st_size]
        except OSError:
            fp[name] = None
    return fp


def load_seen():
    try:
        with io.open(SEEN, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_seen(fp):
    with io.open(SEEN, "w", encoding="utf-8") as f:
        json.dump(fp, f)


def run(script, *args, stdin=None):
    """跑同目录下的脚本，返回 (退出码, 输出)。

    子脚本的中文输出被重定向到管道时，默认会按 GBK 编码，日志里就是乱码。
    -X utf8 管住直接子进程（-I 会忽略环境变量）；环境变量管住上游 wce.py
    自己再起的孙进程（它们不带 -I）。两个都要，和 bat 里的设置一致。
    """
    cmd = [CHILD_PY, "-I", "-X", "utf8", os.path.join(HERE, script), *args]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=HERE, stdin=stdin, capture_output=True,
                       env=env, creationflags=NO_WINDOW)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")


def one_round():
    """检查一次，有变化才导出。返回退出码。"""
    with io.open(HEARTBEAT, "w", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if not wxwork_running():
        log_state("wxwork_off", "企业微信没有在运行，暂停导出（开了会自动接上）")
        return 0

    data = db_dir()
    if not data:
        # stdin 给空：多账号时 _pick_account 读到 EOF 会默认选最近在用的那个
        rc, out = run("_pick_account.py", stdin=subprocess.DEVNULL)
        data = db_dir()
        if rc != 0 or not data:
            log(out)
            log_state("no_account", "[X] 没能定位到聊天数据目录")
            return 1

    # 导出前先记下此刻的样子：导出期间又来了新消息，下一轮还能发现
    fp = fingerprint(data)
    archived = os.path.isdir(os.path.join(HERE, "wxwork_archive"))
    if archived and fp == load_seen():
        log_state("idle", "企业微信运行中，等待新消息")
        return 0

    t0 = time.time()
    # 只要 json：归档的 html/csv 是 _archive_merge 从合并后的全集重新生成的，
    # 上游那份只给快照用，而定时模式不留快照。少写 2/3 的文件，实测一轮从
    # 5~24 秒降到 4 秒左右（波动来自刚写出的文件被安全软件扫描）
    rc, exp_out = run("_run_export.py", "--formats", "json")
    if rc != 0:
        log(exp_out)                   # 失败才留完整输出，成功时上游那几十行没人看
        log_state("export_fail", f"[X] 导出失败，退出码 {rc}")
        return rc
    t1 = time.time()
    rc, out = run("_archive_merge.py", "--no-snapshot")
    if rc != 0:
        log(out)
        log_state("merge_fail", f"[X] 并入归档失败，退出码 {rc}")
        return rc

    save_seen(fp)
    m = re.search(r"本次新增 (\d+) 条", out)
    added = m.group(1) if m else "?"
    reused = "复用密钥" if "跳过内存扫描" in exp_out else "重新提取密钥"
    log(f"导出完成：新增 {added} 条，用时 {time.time() - t0:.1f}s"
        f"（导出 {t1 - t0:.1f}s + 归档 {time.time() - t1:.1f}s，{reused}）")

    # 交付数据（全量/增量 JSON）。失败不影响归档：它的进度没推进，
    # 下次有变化时会把没交付的一并补上
    rc, out = run("_delta_export.py")
    if rc != 0:
        log(out)
        log_state("delta_fail", f"[X] 生成交付数据失败，退出码 {rc}，下次有新消息时会补上")
    else:
        line = next((ln.strip() for ln in out.splitlines() if "交付数据" in ln), "")
        if line and "没有新消息" not in line:
            log(line)
    # 导完就回到「等待」状态，免得每次导出后紧跟一行「等待新消息」
    with io.open(STATE, "w", encoding="utf-8") as f:
        f.write("idle")
    return 0


def watch(interval):
    """在 WATCH_SPAN 秒内每 interval 秒检查一次。"""
    deadline = time.time() + WATCH_SPAN
    rc = 0
    while True:
        start = time.time()
        rc = one_round()
        nxt = start + interval
        if nxt >= deadline:
            return rc
        time.sleep(max(0.0, nxt - time.time()))


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    rotate_log()

    interval = None
    if "--watch" in sys.argv:
        try:
            interval = max(1, int(sys.argv[sys.argv.index("--watch") + 1]))
        except (IndexError, ValueError):
            interval = 5

    try:
        import Crypto  # noqa: F401
    except ImportError:
        log_state("no_dep", "[X] 缺少依赖 pycryptodome。先手动双击一次「一键导出聊天记录.bat」，它会自动装。")
        return 1

    # 文件锁而不是「锁文件存在就跳过」：进程被杀或断电时系统会自动释放，不会永久卡死
    lock = open(LOCK, "a+")
    lock.seek(0)          # locking 锁的是「当前位置起」的字节，两个进程必须锁同一处
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return 0          # 上一轮还在跑（多半在重提密钥），不打扰它

    try:
        return watch(interval) if interval else one_round()
    except Exception as e:
        log(f"[X] 意外错误：{e!r}")
        return 1
    finally:
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
