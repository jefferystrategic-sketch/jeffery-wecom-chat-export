#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成交付给下游的 JSON：第一次全量，之后只出增量。

  _delta_export.py            按状态自动判断全量还是增量
  _delta_export.py --full     强制重出一次全量（会重置进度）

输出：scripts/wxwork_delta/<批次>/<会话>.json
  批次目录 <时间>_full/ 或 <时间>_incr/，按目录名排序即按时间顺序；
  里面每个有数据的会话一个 JSON，文件名形如「单聊_内部_张三_S_168…_168….json」：
  类型_内外部_会话名称_会话ID，末尾的会话 ID 保证同名群也不会撞。
  增量批次只包含有新消息的会话、且只含新消息；没有新消息就不建批次目录。

每个 JSON 文件头带一份元数据（不是每条消息都带）：
  source      固定 "wecom"
  batch       全量 / 增量、批次号、上一批次号
  account     登录企微的用户：ID、姓名、账号、邮箱、部门，以及所在组织
  conversation  会话 ID、类型、名称、内部还是外部、对方（单聊）或成员（群聊）

只导出和人的对话：单聊 S:、群聊 R:、微信客户 M:。应用、审批、邮件提醒、
系统消息、文件传输助手不导出。

内部 / 外部：对方和登录用户属于同一家企业（企业 ID 相同）就是内部。
群聊里只要有一个外部成员，或者有查不到资料的成员，就算外部。
微信客户（M:）一律算外部。

进度存在 wxwork_delta/state.json：记的是每个会话已经交付过的消息唯一键
（与 _archive_merge 去重用的是同一个键），不是最大消息编号。企微补拉到
更早的历史消息时也能当作新消息交付，不会漏。批次文件全部写完才更新进度，
中途失败下次会重出。以后接服务器时，改成收到 200 后再更新进度即可。
"""
import io
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:            # python -I 下要补，才 import 得到同目录模块
    sys.path.insert(0, HERE)
from _archive_merge import msg_key, msg_list, read_json, sort_key  # noqa: E402

ARCHIVE = os.path.join(HERE, "wxwork_archive")
OUT = os.path.join(HERE, "wxwork_delta")
STATE = os.path.join(OUT, "state.json")
SCHEMA = "wecom-chat-export/v1"

PERSON_PREFIXES = {"S": "single", "R": "group", "M": "wechat"}
TYPE_LABEL = {"single": "单聊", "group": "群聊", "wechat": "微信客户"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
NOT_EMAIL_TLD = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
# user_table.pb_content 里邮箱出现在这几个字段，按命中率排序（157 人里 84 号字段占 128 个）
EMAIL_FIELD_ORDER = [(84,), (17,), (3,)]


# ---------- protobuf：不依赖 .proto，按线格式扫出所有字符串 ----------

def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def pb_strings(b, path=(), depth=0, out=None):
    """返回 [(字段路径, 字符串)]。嵌套消息也会展开（最多 4 层）。"""
    out = [] if out is None else out
    i = 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            field, wt = key >> 3, key & 7
            if field == 0:
                break
            if wt == 0:
                _, i = _varint(b, i)
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            elif wt == 2:
                n, i = _varint(b, i)
                chunk = b[i:i + n]
                i += n
                try:
                    s = chunk.decode("utf-8")
                    if s and s.isprintable():
                        out.append((path + (field,), s))
                except UnicodeDecodeError:
                    pass
                if depth < 4 and n > 1:
                    pb_strings(chunk, path + (field,), depth + 1, out)
            else:
                break
    except (IndexError, ValueError):
        pass                        # 不是合法 protobuf 的片段，扫到哪算哪
    return out


def emails_from_pb(blob):
    """按字段优先级排好的邮箱列表，去重。"""
    if not blob:
        return []
    found = []
    for p, s in pb_strings(bytes(blob)):
        m = EMAIL_RE.fullmatch(s.strip())
        if m and s.rsplit(".", 1)[-1].lower() not in NOT_EMAIL_TLD:
            found.append((p, s.strip()))
    rank = {p: i for i, p in enumerate(EMAIL_FIELD_ORDER)}
    found.sort(key=lambda x: rank.get(x[0], len(rank)))
    seen, out = set(), []
    for _p, e in found:
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


# ---------- 从解密库读人员、组织、会话 ----------

def _open(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class Directory:
    """登录用户、通讯录、组织、会话成员。只读解密库。"""

    def __init__(self, cfg):
        dec = cfg.get("wxwork_decrypted_dir", "wxwork_decrypted")
        self.dec = dec if os.path.isabs(dec) else os.path.join(HERE, dec)
        # 数据目录是 Documents/WXWork/<登录用户 ID>/Data
        self.self_id = int(os.path.basename(os.path.dirname(cfg["wxwork_db_dir"].rstrip("/\\"))))
        self.users, self.depts, self.corps = {}, defaultdict(list), {}
        self.conv_names, self.members = {}, defaultdict(set)
        self.self_corp = {}
        self._load_users()
        self._load_corps()
        self._load_sessions()
        me = self.users.get(self.self_id, {})
        self.self_corp_num = me.get("_corp_num")

    def _load_users(self):
        conn = _open(os.path.join(self.dec, "user.db"))
        try:
            tabs = _tables(conn)
            for r in conn.execute("SELECT * FROM user_table"):
                emails = [r["email"]] if r["email"] else []
                emails += [e for e in emails_from_pb(r["pb_content"]) if e not in emails]
                self.users[int(r["id"])] = {
                    "user_id": int(r["id"]),
                    "name": r["real_name"] or r["name"] or r["account"] or None,
                    "english_name": r["english_name"] or None,
                    "account": r["account"] or None,
                    "email": emails[0] if emails else None,
                    "emails": emails,
                    "position": r["position"] or None,
                    "_corp_num": r["corp_id"],
                }
            if {"department_tableV2", "user_dept_tableV2"} <= tabs:
                names = {int(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM department_tableV2")}
                for r in conn.execute("SELECT user_id, department_id FROM user_dept_tableV2"):
                    n = names.get(int(r["department_id"]))
                    if n and n not in self.depts[int(r["user_id"])]:
                        self.depts[int(r["user_id"])].append(n)
        finally:
            conn.close()

    def _load_corps(self):
        conn = _open(os.path.join(self.dec, "company.db"))
        try:
            tabs = _tables(conn)
            if "external_company_table_v2" in tabs:
                for r in conn.execute("SELECT corpany_id, name, short_name FROM external_company_table_v2"):
                    self.corps[int(r["corpany_id"])] = {
                        "corp_numeric_id": int(r["corpany_id"]),
                        "name": r["name"] or None,
                        "short_name": r["short_name"] or None,
                    }
            if "self_corp_list_table" in tabs:
                me = self.users.get(self.self_id, {})
                for r in conn.execute("SELECT corpany_id, self_corp_info FROM self_corp_list_table"):
                    if int(r["corpany_id"]) != me.get("_corp_num"):
                        continue
                    # 字段号来自实测：3=简称，24=全称，60=企业 ID（ww 开头）
                    f = {}
                    for p, s in pb_strings(bytes(r["self_corp_info"] or b"")):
                        if len(p) == 1:
                            f.setdefault(p[0], s)
                    base = self.corps.get(int(r["corpany_id"]), {})
                    cid = f.get(60)
                    self.self_corp = {
                        "corp_id": cid if cid and cid.startswith("ww") else None,
                        "corp_numeric_id": int(r["corpany_id"]),
                        "name": f.get(24) or base.get("name"),
                        "short_name": f.get(3) or base.get("short_name"),
                    }
                    for p, s in pb_strings(bytes(r["self_corp_info"] or b"")):
                        # 登录用户本人的邮箱有时只在这份资料里
                        if p[:1] == (11,) and EMAIL_RE.fullmatch(s) and me and not me.get("email"):
                            me["email"] = s
                            me["emails"] = [s]
        finally:
            conn.close()

    def _load_sessions(self):
        conn = _open(os.path.join(self.dec, "session.db"))
        try:
            for r in conn.execute("SELECT id, name, type_content FROM conversation_table"):
                name = r["name"] or None
                if not name and r["id"].startswith("M:") and r["type_content"]:
                    # 微信客户的昵称只在 type_content 的 5 号字段（实测 14/14）
                    f = {p[0]: s for p, s in pb_strings(bytes(r["type_content"])) if len(p) == 1}
                    name = f.get(5)
                self.conv_names[r["id"]] = name
            for r in conn.execute("SELECT conversation_id, user_id FROM conversation_user_table"):
                self.members[r["conversation_id"]].add(int(r["user_id"]))
        finally:
            conn.close()

    # ----- 对外的几个查询 -----

    def corp_of(self, corp_num):
        if corp_num is None:
            return None
        if corp_num == self.self_corp_num and self.self_corp:
            return dict(self.self_corp)
        c = self.corps.get(corp_num)
        return dict(c) if c else {"corp_numeric_id": corp_num, "name": None, "short_name": None}

    def is_internal(self, uid):
        u = self.users.get(uid)
        return bool(u) and self.self_corp_num is not None and u["_corp_num"] == self.self_corp_num

    def person(self, uid, fallback_name=None):
        u = self.users.get(uid)
        if not u:
            return {"user_id": uid, "name": fallback_name, "english_name": None, "account": None,
                    "email": None, "emails": [], "position": None, "departments": [],
                    "org": None, "is_internal": False, "profile_found": False}
        p = {k: v for k, v in u.items() if not k.startswith("_")}
        internal = self.is_internal(uid)
        p["departments"] = self.depts.get(uid, []) if internal else []
        p["org"] = self.corp_of(u["_corp_num"])
        p["is_internal"] = internal
        p["profile_found"] = True
        return p

    def account(self):
        a = self.person(self.self_id)
        a.pop("is_internal", None)
        a["org"] = dict(self.self_corp) if self.self_corp else a.get("org")
        return a


# ---------- 会话 ----------

def conv_type(cid):
    return PERSON_PREFIXES.get(cid.split(":", 1)[0]) if ":" in cid else None


def describe_conversation(d, cid, display_name):
    """返回会话元数据：类型、名称、内外部、对方或成员。"""
    t = conv_type(cid)
    # 上游查不到名字时 display_name 就是会话 ID 本身，那种不算名字
    if display_name and display_name.replace(":", "_") in (cid, cid.replace(":", "_")):
        display_name = None
    name = d.conv_names.get(cid) or display_name if t == "wechat" else display_name or d.conv_names.get(cid)
    meta = {"conversation_id": cid, "type": t, "type_label": TYPE_LABEL[t], "name": name}

    if t == "wechat":
        # 个人微信用户不在企微通讯录里，只有会话名称
        meta["scope"] = "external"
        meta["scope_reason"] = "微信客户"
        tail = cid.split(":", 1)[1]
        meta["counterpart"] = {
            "user_id": int(tail) if tail.isdigit() else tail, "name": name, "english_name": None,
            "account": None, "email": None, "emails": [], "position": None, "departments": [],
            "org": None, "is_internal": False, "profile_found": False, "contact_type": "wechat",
        }
        return meta

    if t == "single":
        ids = [int(x) for x in re.findall(r"\d+", cid.split(":", 1)[1])]
        others = [i for i in ids if i != d.self_id] or ids
        cp = d.person(others[0], name) if others else None
        meta["counterpart"] = cp
        internal = bool(cp and cp["is_internal"])
        meta["scope"] = "internal" if internal else "external"
        meta["scope_reason"] = "对方与登录用户同一企业" if internal else "对方属于其他企业或资料缺失"
        return meta

    # 群聊
    member_ids = sorted(d.members.get(cid, set()))
    members = [d.person(m) for m in member_ids]
    unknown = [m for m in members if not m["profile_found"]]
    external = [m for m in members if m["profile_found"] and not m["is_internal"]]
    meta["member_count"] = len(members)
    meta["members"] = members
    if not members:
        meta["scope"], meta["scope_reason"] = "external", "查不到群成员"
    elif external or unknown:
        meta["scope"] = "external"
        meta["scope_reason"] = f"含 {len(external)} 名外部成员、{len(unknown)} 名资料缺失成员"
    else:
        meta["scope"], meta["scope_reason"] = "internal", "全部成员与登录用户同一企业"
    return meta


def shape_message(d, m, conv):
    """原始字段原样保留，去掉每条都一样的会话字段，补一个结构化的发送人。"""
    out = {k: v for k, v in m.items() if k not in ("conversation_id", "conversation", "conversation_kind")}
    sid = m.get("sender_id")
    cp = conv.get("counterpart") or {}
    name = (d.users.get(sid) or {}).get("name")
    if not name and sid == cp.get("user_id"):
        name = cp.get("name")           # 微信客户不在通讯录里，用会话里的昵称
    out["sender"] = {
        "user_id": sid,
        "name": name or m.get("sender"),
        "is_self": sid == d.self_id,
        "is_internal": d.is_internal(sid) if isinstance(sid, int) else False,
    }
    return out


# ---------- 状态 ----------

def load_state():
    st = read_json(STATE)
    return st if isinstance(st, dict) else None


def save_state(st):
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def file_name(meta):
    """类型_内外部_会话名称_会话ID.json，人一眼能看懂，末尾 ID 保证唯一。"""
    # Windows 文件名不允许的字符换成下划线；群名可能很长，截到 40 字
    name = re.sub(r'[\\/:*?"<>|\s]', "_", meta.get("name") or "").strip(" ._")[:40]
    cid = re.sub(r"[^0-9A-Za-z_\-]", "_", meta["conversation_id"])
    scope = "内部" if meta["scope"] == "internal" else "外部"
    return "_".join([meta["type_label"], scope] + ([name] if name else []) + [cid]) + ".json"


def main():
    cfg = read_json(os.path.join(HERE, "config.json")) or {}
    if not os.path.isdir(ARCHIVE) or not cfg.get("wxwork_db_dir"):
        print("      (没有归档或配置，跳过交付数据)")
        return 0

    # 手动导出和定时导出可能同时跑到这一步，两边同时读写 state.json 会重复交付或丢进度。
    # LK_LOCK 拿不到会每秒重试、10 次后抛错，这一轮就算失败，下次再补
    import msvcrt
    os.makedirs(OUT, exist_ok=True)
    lock = open(os.path.join(OUT, ".lock"), "a+")
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
    try:
        return produce(cfg)
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def produce(cfg):
    d = Directory(cfg)
    state = load_state()
    full = ("--full" in sys.argv[1:] or not state
            or state.get("account_user_id") != d.self_id)
    sent = {} if full else {k: set(v) for k, v in state.get("sent_keys", {}).items()}

    now = datetime.now().astimezone()
    # 时间在前：下游按目录名排序就是按时间顺序，重新全量后也不会乱
    batch_id = f"{now:%Y%m%d_%H%M%S}_{'full' if full else 'incr'}"
    batch = {
        "type": "full" if full else "incremental",
        "batch_id": batch_id,
        "previous_batch_id": None if full else state.get("last_batch_id"),
        "generated_at": now.isoformat(timespec="seconds"),
    }
    account = d.account()

    files, new_sent, total = [], defaultdict(set), 0
    for name in sorted(os.listdir(ARCHIVE)):
        doc = read_json(os.path.join(ARCHIVE, name, "messages.json"))
        if doc is None:
            continue
        msgs, meta = msg_list(doc)
        conv = (meta or {}).get("conversation") or {}
        cid = conv.get("conversation_id") or (msgs[0].get("conversation_id") if msgs else None)
        if not cid or not conv_type(cid):
            continue                  # 应用、审批、系统等非人对话
        done = sent.get(cid, set())
        fresh = [m for m in msgs if msg_key(m) not in done]
        new_sent[cid] = done | {msg_key(m) for m in msgs}
        if not fresh:
            continue
        fresh.sort(key=sort_key)
        cmeta = describe_conversation(d, cid, conv.get("display_name"))
        files.append((cid, {
            "schema": SCHEMA,
            "source": "wecom",
            "batch": batch,
            "account": account,
            "conversation": cmeta,
            "message_count": len(fresh),
            "time_range": {"first": fresh[0].get("time"), "last": fresh[-1].get("time")},
            "messages": [shape_message(d, m, cmeta) for m in fresh],
        }))
        total += len(fresh)

    if not files:
        print("      交付数据：没有新消息，不出批次")
        return 0

    # 先写到临时目录，全部写完再改名：中途失败不会留下半个批次
    os.makedirs(OUT, exist_ok=True)
    final = os.path.join(OUT, batch_id)
    tmp = final + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for cid, body in files:
        with io.open(os.path.join(tmp, file_name(body["conversation"])), "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, final)

    # 未出现在本次归档里的会话（理论上不会）保留原进度
    for k, v in sent.items():
        new_sent.setdefault(k, v)
    save_state({
        "schema": SCHEMA,
        "account_user_id": d.self_id,
        "last_batch_id": batch_id,
        "updated_at": now.isoformat(timespec="seconds"),
        "sent_keys": {k: sorted(v) for k, v in new_sent.items()},
    })
    kind = "全量" if full else "增量"
    scopes = defaultdict(int)
    for _cid, body in files:
        scopes[body["conversation"]["scope"]] += 1
    print(f"      交付数据：{kind} {len(files)} 个会话（内部 {scopes['internal']}、外部 {scopes['external']}），"
          f"{total} 条消息 → wxwork_delta\\{batch_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
