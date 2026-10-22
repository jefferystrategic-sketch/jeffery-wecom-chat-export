# WeCom wxSQLite3 AES-128-CBC decryption — algorithm & config

## Per-page decryption primitive (from `wxwork_crypto.py`)
- Page size = 4096. Fixed salt `b"sAlT"`.
- For page `n` (1-based):
  - `key = MD5(raw_key[16 bytes] + struct.pack("<I", n) + b"sAlT")`
  - IV via SQLite3MultipleCiphers LCG seeded from a second MD5 (see source).
  - `AES.new(key, AES.MODE_CBC, iv).decrypt(page_ciphertext)`.
- Page 1 special case: SQLite header bytes 16–23 stay plaintext (legacy behavior);
  the routine reassembles them after decryption and validates "looks like SQLite".
- Verification: decrypt page 1, check the result resembles a SQLite page-1 (magic
  region / page size field). `verify_wxsqlite3_aes128_key(raw_key, page1)` does this.

## Key extraction (from `find_wxwork_keys.py`)
- WXWork 5.x is **32-bit** → all object pointers are 4 bytes.
- Cipher object layout used by the struct scanner:
  - flags at +0 and +4 (value 1/2 or page size).
  - `aes_ctx` pointer at +0x2C.
  - page-size chain: `*(cipher+0x30)` → `+4` → `+0x24` must read back a valid
    page size in {512..65536}.
  - `raw_key` = 16 bytes at `cipher+0x08`.
- The hex-literal `x'<hex>'` pattern scan is a fallback; the struct scan is the
  reliable path on 5.0.x.
- No admin required: `OpenProcess(0x0010 | 0x0400, False, pid)` on the user's
  own WXWork.exe process.

## Ready config.json (this machine)
```json
{
  "wxwork_db_dir": "C:/Users/你的用户名/Documents/WXWork/账号ID/Data",
  "wxwork_keys_file": "wxwork_keys.json",
  "wxwork_decrypted_dir": "wxwork_decrypted",
  "wxwork_export_dir": "wxwork_export"
}
```
- `self_id` inferred from the `wxwork_db_dir` last path segment (the 16-digit account id, e.g. `账号ID`).
- The global master key (example, do not hardcode in skill): a single 32-hex
  string reused for all 17 DBs. Re-run `find_wxwork_keys.py` each session because
  the in-memory key changes after WXWork restart.

## File roles
- `message.db` — tables `message_table`, `message_small_table`, `kf_message_tableV1`
  (columns include conversation_id, sender_id, content_type, send_time, content,
  extra_content, local_extra_content, message_id, server_id, sequence).
- `user.db` — `user_table` (id, name, real_name, account, external_corp_name) and
  `external_user_relation_v3` for member name resolution.
- `session.db` — `conversation_table` (id, name, roomname_remark, last_message_time),
  `conversation_user_table` / `conversation_member_nickname_table` for group nicknames.
- Conversation ID prefixes: `R:` group, `S:` 1:1, `M:` WeChat contact, `Y:` system/app,
  `O:` app/public account, `MAIL`/`FILEASSIST` special.
