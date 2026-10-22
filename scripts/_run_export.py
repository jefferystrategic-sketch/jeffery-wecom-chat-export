#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出的总调度：提密钥 → 解密 → 补 WAL → 交给 wce.py 导出。

为什么不直接跑 wce.py：
  1. wce.py 会打印 ✓ ✗ 这类字符，但没给自己设输出编码（上游只在
     export_wxwork_messages.py 里设了）。中文 Windows 控制台默认 GBK，直接跑会
     UnicodeEncodeError 崩在第 124 行。PYTHONUTF8/PYTHONIOENCODING 在输出被
     重定向时不总是生效，所以这里用 reconfigure 直接改当前进程，再把 wce.py
     当模块跑——同一个进程，设定必然继承。
  2. 已存密钥还能用就跳过内存扫描（省约 45 秒）。
  3. 上游解密只解 .db、跳过 -wal，刚收到的消息还在 -wal 里就导不出来
     （实测：发出 4 秒后导出，没有这条）。所以前两步由这里按上游原脚本跑，
     中间插一步 _wal_apply，最后让 wce.py 带 --skip-extract --skip-decrypt
     只做导出。上游脚本本身一行没改。
"""
import io
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass   # 老版本 Python 没有 reconfigure，errors=replace 兜不住也不该中断

# 子脚本靠 os.getcwd() 找 config.json 之外的相对路径，统一切到 scripts/
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
# bat 用 python -I 跑本脚本，-I 会把脚本目录和当前目录都移出 sys.path。
# 不补的话下面 import wxwork_crypto 失败，被当成「密钥失效」，每次都白白重提 45 秒
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def load_cfg():
    with io.open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def keys_still_valid():
    """已存密钥还能不能解开本机数据库？能就跳过内存扫描（省约 45 秒）。

    验证只读磁盘：拿每个库的首页试解一次，不需要企微在跑。实测 17 个库
    9 毫秒验完。企微重启后密钥会变，那时验证必然失败，自动回到重新提取，
    不会拿着过期密钥去解出一堆乱码。
    """
    try:
        keys = json.load(io.open(os.path.join(HERE, "wxwork_keys.json"), encoding="utf-8"))
        db_dir = load_cfg().get("wxwork_db_dir", "")
        if not keys or not os.path.isdir(db_dir):
            return False
        from wxwork_crypto import verify_wxsqlite3_aes128_key
    except Exception:
        return False

    # 库分散在 Data 的多层子目录里，先建个文件名索引
    found = {}
    for root, _dirs, files in os.walk(db_dir):
        for f in files:
            if f.endswith(".db"):
                found.setdefault(f, os.path.join(root, f))

    checked = 0
    for name, info in keys.items():
        enc = info.get("enc_key") if isinstance(info, dict) else None
        path = found.get(name)
        if not enc or not path:
            continue
        try:
            with open(path, "rb") as fh:
                page1 = fh.read(4096)
            if not verify_wxsqlite3_aes128_key(bytes.fromhex(enc), page1):
                return False        # 有一个对不上就当整体失效，宁可重提
            checked += 1
        except Exception:
            return False
    return checked > 0


def step(script):
    """和 wce.py 的 _run 一样跑上游脚本：不加 -I，输出直通。"""
    sys.stdout.flush()
    return subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=HERE).returncode


def apply_all_wal():
    """把每个库 -wal 里已提交、未合并的数据补进解密副本。

    先在临时副本上补、做完整性检查，过了才替换。企微正好在合并 WAL 时，
    读到的 .db 和 -wal 可能不是同一时刻的，补出来的库会坏——那就保留没补的
    版本，新消息下一轮再拿，不能让整次导出失败。
    """
    from _wal_apply import apply_wal
    from key_utils import get_key_info, strip_key_metadata

    cfg = load_cfg()
    db_dir = cfg["wxwork_db_dir"]
    out_dir = cfg.get("wxwork_decrypted_dir", "wxwork_decrypted")
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(HERE, out_dir)
    with io.open(os.path.join(HERE, "wxwork_keys.json"), encoding="utf-8") as f:
        keys = strip_key_metadata(json.load(f))

    dbs = pages = 0
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db"):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, db_dir)
            out = os.path.join(out_dir, rel)
            info = get_key_info(keys, rel)
            try:
                if os.path.getsize(src + "-wal") == 0 or not info or not os.path.exists(out):
                    continue
            except OSError:
                continue                    # 没有 -wal
            tmp = out + ".wal_tmp"
            try:
                shutil.copyfile(out, tmp)
                n = apply_wal(src + "-wal", tmp, bytes.fromhex(info["enc_key"]))
                if n:
                    conn = sqlite3.connect(tmp)
                    try:
                        ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    finally:
                        conn.close()
                    if not ok:
                        print(f"      ({rel} 的未合并数据这次没对上，跳过，下次再补)")
                        continue
                    os.replace(tmp, out)
                    dbs += 1
                    pages += n
            except Exception as e:
                print(f"      ({rel} 补 WAL 失败，跳过：{e})")
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    print(f"      已补入尚未合并的最新数据：{dbs} 个库，{pages} 页")


argv = sys.argv[1:]
if "--skip-decrypt" not in argv:
    if "--skip-extract" in argv or keys_still_valid():
        print("      已有密钥仍然有效，跳过内存扫描")
    else:
        print("      提取密钥（要扫企业微信进程内存，约 45 秒）...")
        if step("find_wxwork_keys.py") != 0:
            print("✗ 密钥提取失败。确认企业微信已登录；重启用客户端后密钥会变化，请重试。")
            sys.exit(1)
    if step("decrypt_wxwork_db.py") != 0:
        print("✗ 解密失败。检查 config.json 的 wxwork_db_dir 与密钥。")
        sys.exit(1)
    apply_all_wal()
    argv = ["--skip-extract", "--skip-decrypt"] + [
        a for a in argv if a not in ("--skip-extract", "--skip-decrypt")]

sys.argv = [os.path.join(HERE, "wce.py")] + argv
runpy.run_path(sys.argv[0], run_name="__main__")
