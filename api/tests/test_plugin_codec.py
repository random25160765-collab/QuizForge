"""插件 B 的加解密口径，与 Python 侧**跨语言对拍**。

跑的是 `obsidian-sync/codec.js` 真正的实现（`selftest.mjs` 里 require 它），
不是一份手抄的对照物 —— 抄一份只能证明抄的人当时抄对了。断言的每一条都是
"抄错不会报错、只会让同步永远失败"的地方：

* scrypt 的 salt / N / r / p（含 Node 的 maxmem 坑）；
* `v1:` + base64(nonce12 + 密文 + tag16) 的字节顺序；
* AAD 里的 pk 是 **JSON 数组字符串**，不是路径本身；
* payload 的键名与时间串格式（QF 按列名落库）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")

REPO = Path(__file__).resolve().parents[2]

PASSPHRASE = "test-passphrase"
SALT = bytes(range(16))
NONCE = bytes(range(12))
BODY = "# 今天\n\n写了点东西。"
PK = json.dumps(["日记/2026-11-09.md"], ensure_ascii=False, separators=(",", ":"))
DATA = {
    "path": "日记/2026-11-09.md",
    "body": BODY,
    "hash": hashlib.sha256(BODY.encode("utf-8")).hexdigest()[:16],
    "mtime": 1700000000.5,
    "size": len(BODY.encode("utf-8")),
    "updated_at": "2026-11-09 12:34:56.789",
}


def _derive() -> bytes:
    return Scrypt(salt=SALT, length=32, n=2**15, r=8, p=1).derive(PASSPHRASE.encode("utf-8"))


def _seal(key: bytes) -> str:
    plain = json.dumps(DATA, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sealed = AESGCM(key).encrypt(NONCE, plain, f"quizforge-sync:v1:notes:{PK}".encode("utf-8"))
    return "v1:" + base64.b64encode(NONCE + sealed).decode("ascii")


def test_node_codec_matches_python_vector() -> None:
    proc = subprocess.run(
        ["node", str(REPO / "obsidian-sync" / "selftest.mjs")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    key = _derive()
    assert got["keyHex"] == key.hex()
    assert got["check"] == base64.b64encode(
        hmac.new(key, b"quizforge-sync-v1", hashlib.sha256).digest()
    ).decode("ascii")
    assert got["pk"] == PK
    assert got["plain"] == json.dumps(DATA, ensure_ascii=False, separators=(",", ":"))

    # 同一 key、同一 nonce、同一明文 → 密文必须逐字节相同
    assert got["payload"] == _seal(key)

    # 反过来：Python 能解开 Node 产出的密文
    blob = base64.b64decode(got["payload"][3:])
    plain = AESGCM(key).decrypt(
        blob[:12], blob[12:], f"quizforge-sync:v1:notes:{PK}".encode("utf-8")
    )
    assert json.loads(plain) == DATA


def test_sha16_matches_notecloud() -> None:
    """插件的 sha16 与 `notecloud.sha16` 必须是同一个值（冲突判定的地基）。"""
    from app.notecloud import sha16

    # 插件是单文件（Obsidian 里相对的 require 不可靠），所以同样从 main.js 截段执行
    script = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const from = src.indexOf('// #region codec');
const to = src.indexOf('// #endregion codec');
if (from < 0 || to < 0) { console.error('no codec region'); process.exit(2); }
const code = src.slice(from, to) + '\\nreturn { sha16 };';
const { sha16 } = new Function('require', code)(require);
process.stdout.write(sha16(process.argv[2]));
"""
    main_js = str(REPO / "obsidian-sync" / "main.js")
    for text in ("", "abc", "# 今天\n\n写了点东西。", "中文与 emoji 🎯"):
        proc = subprocess.run(
            ["node", "-e", script, main_js, text],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == sha16(text)
