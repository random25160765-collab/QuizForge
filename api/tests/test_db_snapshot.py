"""tools/db_snapshot.py 的口令加密 —— 快照进公开仓库前必须变成密文。

密文要兑现的三个承诺，各验一条：
* 拿着口令解得出原样（roundtrip）；
* 口令不对解不开，且报的是"口令不对"（不是一堆看不懂的底层报错）；
* 密文被改一位也解不开（GCM 的完整性 —— "clone 了打不开"之外还有"改不了"）。

再验一条链路的：save → encrypt → load 全程走 main()（和 `make db-snapshot` /
`make db-restore` 走的是同一条路），落出来的库和原来一样；以及没有 .enc 时
回退到明文旧快照的迁移路径。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import db_snapshot  # noqa: E402


def test_roundtrip(tmp_path: Path):
    src = tmp_path / "plain.gz"
    src.write_bytes(b"hello quizforge" * 100)
    enc = tmp_path / "plain.gz.enc"
    out = tmp_path / "back.gz"

    db_snapshot.encrypt_file(src, enc, "正确的口令")
    db_snapshot.decrypt_file(enc, out, "正确的口令")
    assert out.read_bytes() == src.read_bytes()


def test_wrong_passphrase_says_so(tmp_path: Path):
    src = tmp_path / "plain.gz"
    src.write_bytes(b"x" * 64)
    enc = tmp_path / "plain.gz.enc"
    db_snapshot.encrypt_file(src, enc, "正确的口令")
    with pytest.raises(ValueError, match="口令不对"):
        db_snapshot.decrypt_file(enc, tmp_path / "out.gz", "错误的口令")


def test_tampered_ciphertext_is_rejected(tmp_path: Path):
    src = tmp_path / "plain.gz"
    src.write_bytes(b"x" * 64)
    enc = tmp_path / "plain.gz.enc"
    db_snapshot.encrypt_file(src, enc, "口令")
    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 0x01
    enc.write_bytes(raw)
    with pytest.raises(ValueError, match="口令不对，或文件被人改过"):
        db_snapshot.decrypt_file(enc, tmp_path / "out.gz", "口令")


def test_garbage_input_is_not_ciphertext(tmp_path: Path):
    junk = tmp_path / "junk.enc"
    junk.write_bytes(b"this is not a quizforge ciphertext")
    with pytest.raises(ValueError, match="文件头"):
        db_snapshot.decrypt_file(junk, tmp_path / "out.gz", "口令")


def test_ciphertext_does_not_contain_the_plaintext(tmp_path: Path):
    src = tmp_path / "plain.gz"
    src.write_bytes(b"secret chat records")
    enc = tmp_path / "plain.gz.enc"
    db_snapshot.encrypt_file(src, enc, "口令")
    assert b"secret chat records" not in enc.read_bytes()


def test_save_encrypt_load_full_chain(tmp_path: Path, monkeypatch):
    src_db = tmp_path / "src.db"
    conn = sqlite3.connect(src_db)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO messages VALUES (1, '一句会跟着快照走的聊天')")
    conn.commit()
    conn.close()

    gz = tmp_path / "snap.db.gz"
    enc = tmp_path / "snap.db.gz.enc"
    target = tmp_path / "restored.db"

    monkeypatch.setenv("QUIZFORGE_SNAPSHOT_PASSPHRASE", "链路口令")
    assert db_snapshot.main(["save", "--out", str(gz), "--db", str(src_db), "--no-vectors"]) == 0
    assert db_snapshot.main(["encrypt", "--in", str(gz), "--out", str(enc)]) == 0
    assert db_snapshot.main(["load", "--path", str(enc), "--target", str(target)]) == 0

    conn = sqlite3.connect(target)
    assert conn.execute("SELECT content FROM messages").fetchone()[0] == "一句会跟着快照走的聊天"
    conn.close()


def test_load_falls_back_to_plaintext_when_enc_is_missing(tmp_path: Path):
    """迁移期：老机器还只有明文快照时，make db-restore 仍能工作。"""
    src_db = tmp_path / "src.db"
    conn = sqlite3.connect(src_db)
    conn.commit()
    conn.close()
    gz = tmp_path / "snap.db.gz"
    target = tmp_path / "restored.db"
    assert db_snapshot.main(["save", "--out", str(gz), "--db", str(src_db), "--no-vectors"]) == 0
    missing_enc = Path(str(gz) + ".enc")
    assert db_snapshot.main(["load", "--path", str(missing_enc), "--target", str(target)]) == 0
    assert target.is_file()


def test_wrong_passphrase_blocks_load(tmp_path: Path, monkeypatch):
    src_db = tmp_path / "src.db"
    conn = sqlite3.connect(src_db)
    conn.commit()
    conn.close()
    gz = tmp_path / "snap.db.gz"
    enc = tmp_path / "snap.db.gz.enc"
    assert db_snapshot.main(["save", "--out", str(gz), "--db", str(src_db), "--no-vectors"]) == 0
    monkeypatch.setenv("QUIZFORGE_SNAPSHOT_PASSPHRASE", "真口令")
    assert db_snapshot.main(["encrypt", "--in", str(gz), "--out", str(enc)]) == 0
    monkeypatch.setenv("QUIZFORGE_SNAPSHOT_PASSPHRASE", "假口令")
    assert db_snapshot.main(["load", "--path", str(enc), "--target", str(tmp_path / "restored.db")]) == 1
    assert not (tmp_path / "restored.db").exists()


def test_genkey_refuses_to_overwrite(tmp_path: Path):
    key_file = tmp_path / "snapshot.key"
    assert db_snapshot.genkey(key_file) == 0
    assert key_file.stat().st_mode & 0o077 == 0  # 只有本人可读写
    assert db_snapshot.genkey(key_file) == 1  # 已有口令：不覆盖（两台机器会对不上）
