#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wce.py — WeCom (企业微信) Chat Export, 一键运行器

串联三步：提取内存密钥 -> 解密数据库 -> 导出会话(JSON/HTML/CSV)。
所有脚本与 config.json 同目录（即本 scripts/ 目录）。

用法：
  python wce.py                  # 全流程：提密钥 + 解密 + 导出全部会话
  python wce.py --list           # 仅列出会话（需已解密）
  python wce.py --formats json   # 只导出 JSON
  python wce.py --conversation R:1970324996143826   # 只导出指定会话（可多次）
  python wce.py --skip-extract   # 复用已有 wxwork_keys.json，跳过内存扫描
  python wce.py --skip-decrypt   # 复用已解密的库，只重新导出
"""
import json
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(SCRIPT_DIR, "config.json")
CONFIG_EXAMPLE = os.path.join(SCRIPT_DIR, "config.example.json")
KEYS_FILE = os.path.join(SCRIPT_DIR, "wxwork_keys.json")

STEPS = [
    ("01-extract-keys", "find_wxwork_keys.py"),
    ("02-decrypt-db", "decrypt_wxwork_db.py"),
    ("03-export", "export_wxwork_messages.py"),
]


def _banner(title):
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def _need_windows():
    if sys.platform != "win32":
        print("✗ 本工具依赖读取 WXWork.exe 进程内存（ctypes.windll），仅支持 Windows。")
        print("  其他平台请在企业微信所在的 Windows 机器上运行。")
        return False
    return True


def _check_deps():
    try:
        import Crypto  # noqa: F401
    except Exception:
        print("✗ 缺少依赖 pycryptodome。请先执行：")
        print("    python -m venv .venv && .\\.venv\\Scripts\\pip install -r requirements.txt")
        return False
    return True


def _ensure_config():
    if not os.path.exists(CONFIG):
        if os.path.exists(CONFIG_EXAMPLE):
            shutil.copy(CONFIG_EXAMPLE, CONFIG)
            print(f"已生成 scripts/config.json（基于示例）。请编辑 wxwork_db_dir 指向本机数据目录。")
        else:
            print("✗ 未找到 config.json 与 config.example.json。")
            return False
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as exc:
        print(f"✗ config.json 解析失败：{exc}")
        return False
    db_dir = cfg.get("wxwork_db_dir", "")
    if not db_dir or not os.path.isdir(db_dir):
        print(f"✗ config.json 中的 wxwork_db_dir 无效或不存在：{db_dir}")
        print("  请编辑 scripts/config.json，填为本机真实路径，例如：")
        print(r"    C:\Users\你的用户名\Documents\WXWork\账号ID\Data")
        return False
    return True


def _wxwork_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WXWork.exe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        return "WXWork.exe" in out
    except Exception:
        return False


def _run(script_name, extra_args):
    script = os.path.join(SCRIPT_DIR, script_name)
    cmd = [sys.executable, script] + extra_args
    print(f"\n$ {' '.join(cmd)}\n")
    rc = subprocess.run(cmd, cwd=SCRIPT_DIR).returncode
    return rc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 简单参数解析（独立于子脚本，只控制编排）
    do_list = "--list" in argv
    skip_extract = "--skip-extract" in argv
    skip_decrypt = "--skip-decrypt" in argv
    # 透传给 export 的参数
    passthrough = [a for a in argv if a not in ("--skip-extract", "--skip-decrypt")]

    _banner("WeCom Chat Export · 企业微信聊天记录导出")
    print("工作目录:", SCRIPT_DIR)

    if not _need_windows():
        return 2
    if not _check_deps():
        return 2
    if not _ensure_config():
        return 2

    if not _wxwork_running():
        print("✗ 未检测到 WXWork.exe 正在运行。请先登录并打开企业微信客户端。")
        return 2
    print("✓ 检测到 WXWork.exe 正在运行。")

    # 1) 提取密钥
    if do_list:
        # 仅列出会话：确保已解密；若未解密则先解密
        print("\n[模式] 仅列出会话（--list），将先确保数据库已解密。")
        skip_extract = True  # 列出不需要密钥
    if not skip_extract:
        _banner("步骤 1/3 · 从 WXWork 进程内存提取主密钥")
        if _run("find_wxwork_keys.py", []) != 0:
            print("✗ 密钥提取失败。确认企业微信已登录；重启用客户端后密钥会变化，请重试。")
            return 1
    else:
        print("\n[跳过] 步骤 1/3 密钥提取（--skip-extract）")

    # 2) 解密
    if not skip_decrypt:
        _banner("步骤 2/3 · 解密 wxSQLite3 AES-128-CBC 数据库")
        if _run("decrypt_wxwork_db.py", []) != 0:
            print("✗ 解密失败。检查 config.json 的 wxwork_db_dir 与密钥。")
            return 1
    else:
        print("\n[跳过] 步骤 2/3 解密（--skip-decrypt）")

    # 3) 导出
    _banner("步骤 3/3 · 导出会话")
    export_args = list(passthrough)
    if not do_list and "--formats" not in export_args:
        export_args += ["--formats", "csv,html,json"]
    if _run("export_wxwork_messages.py", export_args) != 0:
        print("✗ 导出失败。")
        return 1

    _banner("完成")
    print("导出结果位于 config.json 中的 wxwork_export_dir（默认 scripts/wxwork_export/）。")
    print("提示：wxwork_keys.json 与解密库含敏感信息，请勿提交到 Git。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
