---
name: wecom-chat-export
display_name: 企业微信聊天记录导出与解密
display_name_en: WeCom Chat Export & Decryption
description: |
  本技能用于读取、解密、导出并分析用户自己电脑上企业微信（WeCom/WXWork）的本地聊天数据库。覆盖定位数据目录、从 WXWork.exe 进程内存提取全局主密钥（无需管理员）、解密 wxSQLite3 AES-128-CBC 数据库、并导出为 JSON/HTML/CSV 供分析。触发词示例："导出企业微信聊天记录"、"企业微信数据库解密"、"读取 WXWork 记录"、"分析企业微信聊天"。
description_zh: 读取并解密你自己电脑上的企业微信（WeCom/WXWork）本地聊天数据库，导出为 JSON/HTML/CSV 供分析；全程仅访问本机数据，不上传云端。
description_en: Decrypt and export your own local WeCom (WXWork) chat databases on Windows, then analyze them as JSON/HTML/CSV. Runs fully offline on your PC.
category: productivity
version: 1.0.0
author: woliejia
agent_created: true
---

# WeCom (企业微信) Local Chat Export & Decryption

Decrypt and export a user's **own** local WeCom chat databases. WeCom is a
completely different system from personal WeChat — do NOT reuse personal-WeChat
tools (SQLCipher 4). This skill applies only to **WeCom / WXWork 5.x** on Windows.

## When to use
- User asks to export / analyze / summarize WeCom (企业微信) chat records from the PC.
- User mentions WXWork.exe, 企业微信数据库, or wants to read local 企业微信 history.
- Do NOT use for personal WeChat (Weixin.exe, SQLCipher). Use the
  `wechat-group-report` skill for that instead.

## Key facts (verified on WXWork 5.0.6.6028, 32-bit)
- **Process**: `WXWork.exe` (32-bit, installed under `C:\Program Files (x86)\WXWork`).
- **Data dir**: `%USERPROFILE%\Documents\WXWork(账号ID)\Data\` is the standard
  WeCom location. If your "Documents" folder was relocated (e.g. to another drive)
  or WeCom is installed in a non-default path, point `wxwork_db_dir` at that `Data`
  folder. Always verify with `tasklist` + a directory listing before assuming the path.
- **Encryption**: WeCom's own **wxSQLite3 AES-128-CBC**, one page size 4096.
  - Single global **16-byte** master key shared by ALL `.db` files.
  - No HMAC, no random salt. Fixed salt bytes `b"sAlT"`.
  - Per-page: `key = MD5(raw_key + uint32_LE(page_no) + b"sAlT")`,
    IV derived via SQLite3MultipleCiphers LCG + MD5. See `references/algorithm.md`.
  - Encrypted file header is NOT `SQLite format 3\0` (it's ciphertext of the
    SQLite magic), so `head -c16 | strings` shows nothing — that is expected.
- **Key extraction**: read `WXWork.exe` process memory with
  `OpenProcess(PROCESS_VM_READ|PROCESS_QUERY_INFORMATION)` and scan for the
  in-memory cipher object (raw_key at object offset +0x08, 32-bit pointers).
  Works **without admin** because it is the user's own process.
- **Decrypt output**: produces plaintext SQLite; `message.db` holds messages,
  `user.db` holds member names, `session.db` holds conversation list.

## Workflow
1. **Confirm environment** (read-only):
   - `tasklist | grep -i wxwork` — must be running and logged in.
   - Locate the account Data folder (contains `message.db`).
2. **Set up the venv** `C:\Users\你的用户名\.workbuddy\binaries\python\envs\wecom\`
   (create with the managed Python 3.13, then `pip install pycryptodome`).
   If a prior install was interrupted, `rm -rf` the broken `Crypto` dir and
   `--force-reinstall pycryptodome` (slow download ~2 min, be patient).
3. **Scripts are bundled.** This skill ships the decryption/export engine in its
   own `scripts/` directory (`wxwork_crypto.py`, `find_wxwork_keys.py`,
   `key_scan_common.py`, `key_utils.py`, `decrypt_wxwork_db.py`,
   `export_wxwork_messages.py`, `config.py`). No external download needed.
   Install dependencies once:
   `python -m venv .venv && .\.venv\Scripts\pip install -r scripts\requirements.txt`.
   For a one-shot run, `python scripts\wce.py` does extract → decrypt → export in
   one command (it copies `config.example.json` → `config.json` if missing).
4. **config.json**: Copy `scripts/config.example.json` to `scripts/config.json`
   and set `wxwork_db_dir` explicitly to the `Data` folder (auto-detect only checks
   `%USERPROFILE%\Documents\WXWork`, which fails if "Documents" was relocated).
   Also set `wxwork_decrypted_dir` and `wxwork_export_dir`. See
   `references/algorithm.md` for a ready config example.
5. **Extract key**: `python find_wxwork_keys.py` → writes `wxwork_keys.json`.
   Expect ~60s struct scan; result is one global `enc_key` (32 hex chars) reused
   for all DBs.
6. **Decrypt**: `python decrypt_wxwork_db.py` → plaintext DBs in decrypted dir.
7. **Export / list**: `python export_wxwork_messages.py --list` then
   `--formats json,html,csv` (optionally `--conversation ID` to scope).
   Conversation IDs: `R:` = group, `S:` = 1:1, `M:` = WeChat contact, `Y:` =
   system/app. The export script infers `self_id` from the last 16-digit
   segment of `wxwork_db_dir` (a 16-digit number).
   Prefer `python scripts\wce.py` to run steps 5–7 (extract → decrypt → export)
   in a single command.
8. **Analyze**: write a Chinese Markdown summary. For small windows (about 150 msgs)
   read every text message; for large groups, compute per-day volume, top
   senders, type mix, and sample (about 150 msgs). Keep the summary neutral; note that stock/
   investment chatter is group opinion, not advice.

## Gotchas
- `pycryptodome` import error `No module named 'Crypto.Util'`: partial install
  from an interrupted `pip`. Remove `Crypto` from site-packages and reinstall.
- `export_wxwork_messages.py` reads `user.db`/`session.db`/`message.db` at the
  **top level** of the decrypted dir — so set `wxwork_db_dir` to the flat `Data`
  folder (not a parent), otherwise rel paths won't line up.
- Do NOT run memory scans with admin unless needed; the user's own process works
  without elevation.
- Media (images/audio/video) content is NOT decoded — only note "sent media".
