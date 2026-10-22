#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把企微 -wal 文件里的新数据补进已解密的库。

为什么需要：企微的库是 WAL 模式，新消息先写进 message.db-wal，隔一阵才
合并（checkpoint）进 message.db。上游 decrypt_wxwork_db.py 只解 .db、
明确跳过 -wal，所以刚收到的消息要等企微合并后才导得出来。

做法：等于在解密后的副本上手动做一次 checkpoint——
  1. 按 SQLite WAL 格式逐帧读，用头部 salt 和校验和链认出有效帧
     （文件尾部可能是旧轮次的残帧或正在写的半帧，校验和对不上的一律不要）
  2. 只取到最后一个「提交帧」为止，没提交的事务不算
  3. 每帧的页用同一把密钥、按页号解密（WAL 里的页和 .db 里加密方式一样），
     写到解密库对应的位置
原库和 -wal 只读，不碰企微的文件。
"""
import os
import struct

from wxwork_crypto import PAGE_SZ, decrypt_wxsqlite3_aes128_page

WAL_HDR = 32
FRAME_HDR = 24


def _checksum(data, s0, s1, fmt):
    """SQLite walChecksumBytes：两两取 32 位字累加。"""
    words = struct.unpack(f"{fmt}{len(data) // 4}I", data)
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def committed_frames(wal):
    """返回 [(页号, 加密页)]，以及提交后库的总页数。无有效内容返回 ([], 0)。"""
    if len(wal) < WAL_HDR:
        return [], 0
    magic, _ver, page_size, _seq, salt1, salt2, c0, c1 = struct.unpack(">8I", wal[:WAL_HDR])
    if magic not in (0x377F0682, 0x377F0683) or page_size != PAGE_SZ:
        return [], 0
    fmt = ">" if magic & 1 else "<"
    if _checksum(wal[:24], 0, 0, fmt) != (c0, c1):
        return [], 0

    s0, s1 = c0, c1
    pending, frames, db_pages = [], [], 0
    off = WAL_HDR
    while off + FRAME_HDR + PAGE_SZ <= len(wal):
        pgno, commit, f1, f2, k0, k1 = struct.unpack(">6I", wal[off:off + FRAME_HDR])
        page = wal[off + FRAME_HDR: off + FRAME_HDR + PAGE_SZ]
        if (f1, f2) != (salt1, salt2):
            break                       # 上一轮 WAL 留下的旧帧
        s0, s1 = _checksum(wal[off:off + 8], s0, s1, fmt)
        s0, s1 = _checksum(page, s0, s1, fmt)
        if (s0, s1) != (k0, k1):
            break                       # 半帧或旧帧，到此为止
        pending.append((pgno, page))
        if commit:                      # 提交帧：这之前的才算数
            frames.extend(pending)
            pending = []
            db_pages = commit
        off += FRAME_HDR + PAGE_SZ
    return frames, db_pages


def apply_wal(wal_path, decrypted_db, raw_key):
    """把 wal_path 里已提交的页解密后写进 decrypted_db。返回写入的页数。"""
    try:
        with open(wal_path, "rb") as f:
            wal = f.read()          # 一次读完，读的过程中企微再写也不影响这份
    except OSError:
        return 0
    frames, db_pages = committed_frames(wal)
    if not frames:
        return 0

    latest = {}
    for pgno, page in frames:       # 同一页可能改过多次，后面的为准
        latest[pgno] = page
    with open(decrypted_db, "r+b") as out:
        for pgno, page in latest.items():
            out.seek((pgno - 1) * PAGE_SZ)
            out.write(decrypt_wxsqlite3_aes128_page(raw_key, page, pgno))
        out.truncate(db_pages * PAGE_SZ)
    return len(latest)
