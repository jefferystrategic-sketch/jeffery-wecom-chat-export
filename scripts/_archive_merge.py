#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本次导出合并进长期归档，再按日期留一份快照。

要解决的问题：
  上游每次都是全量导出，而且写文件用的是 open(path, "w") —— 直接覆盖。
  企微客户端会清理本地旧消息，所以上个月能导到的记录，这个月可能已经
  不在本地库里了。一旦覆盖，那批记录就真的没了。

做法：
  归档目录 wxwork_archive/<会话>/messages.json 是累积的全集，
  每次用 message_id 去重后把新消息并进去，只增不删。
  归档里的消息顺带重新生成 csv，html 留给本次快照（排版由上游负责）。

  本次导出原样保留一份 wxwork_export_<日期>/，谁要看单次结果就看它。

为什么不改上游的 SQL 做真增量：
  那条 SQL 跨多张表拼接、带参数绑定，动它风险高；而且就算只查新消息，
  上游写文件仍然是覆盖模式，照样会丢旧数据。在外面合并能同时解决两件事，
  且上游将来升级不受影响。
"""
import csv
import io
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg():
    p = os.path.join(HERE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_json(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def msg_list(doc):
    """导出的 json 可能是裸数组，也可能包在 messages/data 里。"""
    if isinstance(doc, list):
        return doc, {}
    if isinstance(doc, dict):
        for k in ("messages", "data", "items"):
            v = doc.get(k)
            if isinstance(v, list):
                meta = {kk: vv for kk, vv in doc.items() if kk != k}
                return v, meta
    return [], {}


def msg_key(m):
    """优先 message_id；缺了就用能唯一定位的几项拼一个。"""
    if not isinstance(m, dict):
        return repr(m)
    for k in ("message_id", "server_id"):
        v = m.get(k)
        if v not in (None, "", 0, "0"):
            return f"{k}:{v}"
    return "|".join(str(m.get(k, "")) for k in ("send_time", "sender_id", "content"))


def sort_key(m):
    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return (num(m.get("send_time")), num(m.get("sequence")), num(m.get("message_id")))


def write_csv(path, msgs):
    if not msgs:
        return
    cols, seen = [], set()
    for m in msgs:
        for k in m:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    # utf-8-sig：Excel 不认无 BOM 的 UTF-8，中文会乱码
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for m in msgs:
            w.writerow(m)


def write_html(path, meta, msgs, dst_dir):
    """复用上游的 _write_html，归档里也有一份能直接看的气泡排版。

    直接 import 上游模块（它要 pycryptodome 等依赖，到这一步必然已装好）。
    会话名从 .info 或目录名还原——上游是用 conv 字典渲染标题的。
    """
    if not msgs:
        return
    # bat 用 python -I 跑本脚本，-I 会把脚本所在目录也移出 sys.path，不补就 import 不到
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import export_wxwork_messages as up
    except Exception as e:
        print(f"      (归档 html 跳过：{e})")
        return

    folder = os.path.basename(dst_dir.rstrip(os.sep))
    # .info 里有上游写好的干净会话名，比从目录名反推可靠（目录名带账号 ID）
    info = {}
    try:
        with io.open(os.path.join(dst_dir, ".info"), encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
    except OSError:
        pass

    conv = {
        "display_name": info.get("display_name") or meta.get("display_name") or folder.rsplit("_", 1)[0],
        "conversation_id": info.get("conversation_id") or meta.get("conversation_id") or folder,
        "kind": info.get("kind") or meta.get("kind") or ("群聊" if "_R_" in folder else ""),
    }
    try:
        up._write_html(path, conv, msgs)
    except Exception as e:
        print(f"      (归档 html 生成失败：{e})")


def merge_one(src_dir, dst_dir):
    """返回 (本次新增条数, 归档后总条数)。"""
    src = os.path.join(src_dir, "messages.json")
    new_doc = read_json(src)
    if new_doc is None:
        return 0, 0
    new_msgs, meta = msg_list(new_doc)

    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "messages.json")
    old_msgs, old_meta = msg_list(read_json(dst) or [])

    merged, keys = list(old_msgs), {msg_key(m) for m in old_msgs}
    added = 0
    for m in new_msgs:
        k = msg_key(m)
        if k not in keys:
            keys.add(k)
            merged.append(m)
            added += 1

    # 没新消息就不重写：58 个会话全部重生成 html 要 5 秒，定时高频跑时这是大头
    if not added and all(os.path.exists(os.path.join(dst_dir, f))
                         for f in ("messages.json", "messages.csv", "messages.html")):
        return 0, len(merged)

    merged.sort(key=sort_key)

    out = dict(old_meta or meta or {})
    out.update(meta or {})
    out["messages"] = merged
    out["_archive_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out["_archive_total"] = len(merged)
    with io.open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # .info 要先复制过来：write_html 靠它拿干净的会话名（否则只能从目录名反推）
    info = os.path.join(src_dir, ".info")
    if os.path.exists(info):
        try:
            shutil.copy(info, os.path.join(dst_dir, ".info"))
        except OSError:
            pass

    write_csv(os.path.join(dst_dir, "messages.csv"), merged)
    write_html(os.path.join(dst_dir, "messages.html"), out, merged, dst_dir)
    return added, len(merged)


def main():
    cfg = load_cfg()
    export = os.path.join(HERE, cfg.get("wxwork_export_dir", "wxwork_export"))
    if not os.path.isdir(export):
        print("      (没有找到本次导出结果，跳过归档)")
        return 0

    archive = os.path.join(HERE, "wxwork_archive")
    os.makedirs(archive, exist_ok=True)

    convs = total_new = 0
    for name in sorted(os.listdir(export)):
        s = os.path.join(export, name)
        if not os.path.isdir(s):
            continue
        added, _total = merge_one(s, os.path.join(archive, name))
        convs += 1
        total_new += added

    print(f"      归档：{convs} 个会话，本次新增 {total_new} 条")
    print(f"      全部历史  {archive}")

    # 定时任务用：每小时留一份快照一天就是 24 个文件夹，内容又都已进了归档
    if "--no-snapshot" in sys.argv[1:]:
        shutil.rmtree(export, ignore_errors=True)
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    snap = os.path.join(HERE, f"wxwork_export_{stamp}")
    try:
        if os.path.exists(snap):
            shutil.rmtree(snap)
        shutil.move(export, snap)
    except OSError as e:
        print(f"      (本次快照改名失败，结果仍在 wxwork_export：{e})")
        snap = export

    print(f"      本次快照  {snap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
