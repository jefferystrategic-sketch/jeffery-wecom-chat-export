#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动定位本机企业微信的聊天数据目录，写进 config.json。

为什么要单独写一个：上游 config.py 里的 auto_detect 找的是「微信」
(xwechat_files/*/db_storage)，企业微信的目录结构完全不同
(Documents/WXWork/<账号ID>/Data)，没有对应的探测逻辑。

判据：同时看「最近修改时间」和「.db 文件数」，最近用的那个账号
两项都会领先。实测一台装了 5 个账号的机器可正确选中在用的那个。
多个账号难分伯仲时列出来让用户自己选，不替用户猜。
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(SCRIPT_DIR, "config.json")
EXAMPLE = os.path.join(SCRIPT_DIR, "config.example.json")


def wxwork_root():
    """Documents 可能被重定向到 OneDrive，所以按顺序试几个位置。"""
    home = os.path.expanduser("~")
    for p in (
        os.path.join(home, "Documents", "WXWork"),
        os.path.join(home, "OneDrive", "Documents", "WXWork"),
        os.path.join(home, "OneDrive", "文档", "WXWork"),
        os.path.join(home, "文档", "WXWork"),
    ):
        if os.path.isdir(p):
            return p
    return None


def scan(root):
    """返回 [(账号ID, Data路径, db数, 最新修改时间)]，按「最新时间」降序。"""
    out = []
    for name in os.listdir(root):
        data = os.path.join(root, name, "Data")
        if not os.path.isdir(data):
            continue
        dbs, newest = 0, 0.0
        for cur, _dirs, files in os.walk(data):
            # 只看两层，再深就是缓存，既慢又不说明问题
            if cur[len(data):].count(os.sep) >= 2:
                _dirs[:] = []
                continue
            for f in files:
                if f.endswith(".db"):
                    dbs += 1
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(cur, f)))
                    except OSError:
                        pass
        if dbs:
            out.append((name, data, dbs, newest))
    out.sort(key=lambda x: (-x[3], -x[2]))
    return out


def fmt_time(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未知"


def choose(found):
    """只有一个直接用；多个且第一名明显更新(超过1天)也直接用；否则让用户选。"""
    if len(found) == 1:
        return found[0]
    top, second = found[0], found[1]
    if top[3] - second[3] > 86400:
        return top

    print()
    print("      本机有多个企业微信账号，请选择要导出的那个：")
    print()
    for i, (name, _data, dbs, mt) in enumerate(found, 1):
        mark = "  <- 最近在用" if i == 1 else ""
        print(f"        [{i}] {name}   最后活动 {fmt_time(mt)}   {dbs} 个数据库{mark}")
    print()
    while True:
        try:
            s = input(f"      输入序号 (1-{len(found)})，直接回车选 1：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return found[0]
        if not s:
            return found[0]
        if s.isdigit() and 1 <= int(s) <= len(found):
            return found[int(s) - 1]
        print("      序号不对，再试一次。")


def main():
    root = wxwork_root()
    if not root:
        print("      [X] 找不到 Documents\\WXWork 目录。")
        print("          企业微信可能装在非默认位置，或者还没登录过。")
        return 1

    found = scan(root)
    if not found:
        print(f"      [X] {root} 下没有找到任何含聊天数据的账号。")
        print("          请先登录企业微信并收发几条消息，再重试。")
        return 1

    name, data, dbs, mt = choose(found)

    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}          # 坏掉的配置直接重写，不让它卡住流程
    elif os.path.exists(EXAMPLE):
        try:
            with open(EXAMPLE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    cfg["wxwork_db_dir"] = data.replace("\\", "/")
    cfg.setdefault("wxwork_keys_file", "wxwork_keys.json")
    cfg.setdefault("wxwork_decrypted_dir", "wxwork_decrypted")
    cfg.setdefault("wxwork_export_dir", "wxwork_export")

    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"      账号 {name}（{dbs} 个数据库，最后活动 {fmt_time(mt)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
