"""数据边界与用户数据包。

这里钉住的四件事，都是"靠人记得"一定会失效的地方：

1. **每张表都得有归属** —— 往 `models.py` 加表的人必须当场选一边（用户 / 题库 / 派生）。
2. **用户包里没有题库数据** —— 不靠"我们没导"这句话，而是打开包直接数题库表有几行。
3. **往返一趟，用户数据一行不差**（含文件是否落地）。
4. **密钥那条纪律**：导出默认抹掉；导入时包里没带就保留本机那一把。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.datapack import (
    BANK_TABLES,
    BOUNDARY,
    DERIVED_TABLES,
    USER_TABLES,
    PackError,
    PackRefused,
    check_boundary,
    db_file,
    describe,
    export_user,
    import_user,
    read_manifest,
)


# --------------------------------------------------------------------- 边界本身

def test_boundary_covers_every_table() -> None:
    """表结构与声明严丝合缝（多一张、少一张都会在这里红）。"""
    check_boundary()


def test_boundary_notices_a_table_that_was_never_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """少分类一张就得抛 —— 否则新表会被静默漏掉，而那正是这个模块要防的事。"""
    thinned = dict(BOUNDARY)
    thinned.pop("conversation_folders")
    monkeypatch.setattr("app.datapack.BOUNDARY", thinned)
    with pytest.raises(AssertionError, match="conversation_folders"):
        check_boundary()


def test_boundary_kinds_partition_everything() -> None:
    assert set(BOUNDARY.values()) == {"user", "bank", "derived"}
    assert set(USER_TABLES) | set(BANK_TABLES) | set(DERIVED_TABLES) == set(BOUNDARY)
    assert not (set(USER_TABLES) & set(BANK_TABLES))
    # 向量表归派生：它占了整库 82%，谁的包都不该带
    assert "slice_embeddings" in DERIVED_TABLES


# --------------------------------------------------------------------- 一个小世界

@pytest.fixture()
def world(tmp_path: Path) -> dict:
    """源库用测试库（`QF_DATABASE_URL` 指的那份），data 目录与"另一台机器"都在 tmp 里。

    **务必显式传 `root=`**：不传就会落到仓库那份 `data/` 上，用例会去动真东西。
    """
    root = tmp_path / "source" / "data"
    (root / "notes" / "Math").mkdir(parents=True)
    (root / "notes" / "Math" / "大模型数学.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    (root / "library").mkdir()
    (root / "library" / "book.yaml").write_text("citekey: book\n", encoding="utf-8")
    (root / "library" / ".text").mkdir()
    (root / "library" / ".text" / "book.md").write_text("抽出来的正文\n", encoding="utf-8")
    return {
        "root": root,
        "other_root": tmp_path / "target" / "data",
        "other_db": tmp_path / "target" / "quizforge.db",
        "pack": tmp_path / "user.qfpack",
    }


def _seed(db_session) -> None:  # noqa: ANN001 - 夹具给的就是 Session
    """放几条真数据进去：一张对话 + 一条消息 + 一条学习记录 + 设置。"""
    from app.models import AppSettings, Conversation, Message, Record

    conversation = Conversation(title="边界用例", pinned=False, archived=False, folder="")
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="你好，边界",
            parts=[{"type": "text", "text": "你好，边界"}],
        )
    )
    db_session.add(
        Record(
            question_id="uq-datapack-1",
            attempts=3,
            correct=2,
            partial=0,
            wrong=1,
            last_status="correct",
            last_score=1.0,
        )
    )
    db_session.add(AppSettings(id=1, data={"theme": "dark", "ai": {"apiKey": "sk-真不该跟着包走"}}))
    db_session.commit()


def _counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables
        }
    finally:
        conn.close()


def _open_pack_db(pack: Path, tmp: Path) -> Path:
    with zipfile.ZipFile(pack) as archive:
        archive.extract("db/user.db", tmp)
    return tmp / "db" / "user.db"


# --------------------------------------------------------------------- 包里有什么

def test_export_carries_user_rows_and_no_bank_rows(engine, db_session, world: dict, tmp_path: Path) -> None:
    _seed(db_session)

    manifest = export_user(world["pack"], root=world["root"])

    assert manifest["tables"]["messages"] == 1
    assert manifest["tables"]["records"] == 1
    assert manifest["schema"]["userTablesHash"]

    pack_db = _open_pack_db(world["pack"], tmp_path / "unzip")
    bank_counts = _counts(pack_db, BANK_TABLES)
    assert set(bank_counts.values()) == {0}, f"用户包里不该有题库数据：{bank_counts}"
    assert _counts(pack_db, DERIVED_TABLES)["slice_embeddings"] == 0
    assert _counts(pack_db, ("messages",))["messages"] == 1

    # 包自己的体检：表结构指纹与 manifest 对得上
    assert describe(world["pack"])["schemaUnchanged"] is True
    assert describe(world["pack"])["bankRowsInPack"] == {}


def test_export_leaves_the_source_database_alone(engine, db_session, world: dict) -> None:
    """导出是只读操作：源库里的密钥、行数都不该被动过。"""
    _seed(db_session)
    before = _counts(db_file(), USER_TABLES)

    export_user(world["pack"], root=world["root"])

    assert _counts(db_file(), USER_TABLES) == before
    from app.datapack import _read_secret  # noqa: PLC0415 - 只在这里用，且不打印值

    assert _read_secret(db_file()) == "sk-真不该跟着包走"


def test_library_text_is_opt_in(engine, db_session, world: dict, tmp_path: Path) -> None:
    _seed(db_session)
    export_user(world["pack"], root=world["root"])
    names = zipfile.ZipFile(world["pack"]).namelist()
    assert "files/notes/Math/大模型数学.md" in names
    assert "files/library/book.yaml" in names
    assert not [n for n in names if ".text" in n]        # 默认不带（251MB 的派生物）

    export_user(world["pack"], root=world["root"], with_library_text=True)
    names = zipfile.ZipFile(world["pack"]).namelist()
    assert "files/library/.text/book.md" in names


# --------------------------------------------------------------------- 往返

def test_round_trip_restores_every_user_table(engine, db_session, world: dict) -> None:
    _seed(db_session)
    before = _counts(db_file(), USER_TABLES)

    export_user(world["pack"], root=world["root"])
    result = import_user(world["pack"], db_path=world["other_db"], root=world["other_root"])

    assert result["created"] is True                     # 新机器上还没有库
    assert _counts(world["other_db"], USER_TABLES) == before
    assert (world["other_root"] / "notes" / "Math" / "大模型数学.md").is_file()
    assert (world["other_root"] / "library" / "book.yaml").is_file()
    assert not (world["other_root"] / "library" / ".text").exists()


def test_import_refuses_to_overwrite_without_force(engine, db_session, world: dict) -> None:
    _seed(db_session)
    export_user(world["pack"], root=world["root"])
    import_user(world["pack"], db_path=world["other_db"], root=world["other_root"])

    with pytest.raises(PackRefused, match="--force"):
        import_user(world["pack"], db_path=world["other_db"], root=world["other_root"])

    again = import_user(world["pack"], db_path=world["other_db"], root=world["other_root"], force=True)
    assert again["overwrote"] is True


# --------------------------------------------------------------------- 密钥

def test_secret_is_scrubbed_unless_asked(engine, db_session, world: dict, tmp_path: Path) -> None:
    _seed(db_session)

    export_user(world["pack"], root=world["root"])
    manifest = read_manifest(world["pack"])
    assert manifest["secrets"]["aiKeyScrubbed"] is True
    from app.datapack import _read_secret  # noqa: PLC0415

    assert _read_secret(_open_pack_db(world["pack"], tmp_path / "unzip")) == ""

    export_user(world["pack"], root=world["root"], with_secrets=True)
    assert _read_secret(_open_pack_db(world["pack"], tmp_path / "unzip2")) == "sk-真不该跟着包走"


def test_import_keeps_the_local_secret_when_the_pack_has_none(
    engine, db_session, world: dict
) -> None:
    """恢复数据不该把本机刚填好的密钥清掉 —— 密钥不是进度，它跟着机器走。"""
    _seed(db_session)
    export_user(world["pack"], root=world["root"])       # 默认抹过密钥
    import_user(world["pack"], db_path=world["other_db"], root=world["other_root"])

    from app.datapack import _read_secret, _write_secret  # noqa: PLC0415

    assert _read_secret(world["other_db"]) == ""          # 包里没有 → 新机器上也没有
    assert _write_secret(world["other_db"], "sk-本机新填的") is True

    result = import_user(world["pack"], db_path=world["other_db"], root=world["other_root"], force=True)

    assert result["keptLocalSecret"] is True
    assert _read_secret(world["other_db"]) == "sk-本机新填的"


# --------------------------------------------------------------------- 认包

def test_foreign_file_is_rejected_with_a_pointer(world: dict, tmp_path: Path) -> None:
    other = tmp_path / "bank.json"
    other.write_text(json.dumps({"version": 1, "questions": []}), encoding="utf-8")
    with pytest.raises(PackError, match="不是 zip"):
        read_manifest(other)

    wrong = tmp_path / "wrong.qfpack"
    with zipfile.ZipFile(wrong, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "别的东西", "formatVersion": 1}))
    with pytest.raises(PackError, match="题库包走"):
        read_manifest(wrong)

    future = tmp_path / "future.qfpack"
    with zipfile.ZipFile(future, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"format": "quizforge.user-pack", "formatVersion": 99}),
        )
    with pytest.raises(PackError, match="99"):
        read_manifest(future)
