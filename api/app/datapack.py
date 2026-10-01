"""数据包：把「用户数据」和「题库数据」分开搬。

## 为什么必须分成两类

这个应用的数据有两个来源，生命周期完全不同：

* **题库**是出题机的产物（题目 / 考纲树 / 概念图 / 材料切片）。换一份题库是
  一件正常的事，用户也可以选择不导入。
* **用户数据**是自己长出来的（笔记 / 资料库元数据 / 对话 / 自出的题 / 设置）
  加上自己的学习进度（records / attempts / days）。换机器时要原样带走。

混在一个包里搬，就会出现"搬一次用户数据顺手把题库也覆盖掉"这种事 —— 那正是
这个模块要挡住的。

## 边界：三分类，唯一事实来源

`BOUNDARY` 把 `models.py` 里的**每一张表**钉到三类之一，`check_boundary()`
在"表结构与声明不符"时直接抛；`api/tests/test_datapack.py` 有用例钉住它。
于是以后往 `models.py` 加表的人，必须当场回答"这张表属于谁"，而不是等
某天打包时才发现自己漏了一张。

* `bank`    —— 出题机的产物。用户包**绝不**带；由题库包（`pipeline/bankfile.py`）搬。
* `user`    —— 用户自己写的 + 自己的进度。用户包带的就是这些。
* `derived` —— 能重算的（向量、向量化跑单）。谁的包都不带，写清怎么重建。

## 包长什么样

一个 zip：

    manifest.json          这份包里有什么、没什么、配的是哪一份题库（指纹）
    db/user.db             **与运行库同一套表结构**，但只有用户数据（题库表是空的）
    files/notes/…          用户笔记（`data/notes/` 整棵）
    files/library/*.yaml   资料库元数据（权威那一层）
    files/library/.text/…  抽出的正文（`--with-library-text` 才带，见下）

**为什么包里带完整表结构**：导入端拿到这个文件就有一套可校验的东西 ——
`manifest.schema.userTablesHash` 说明"写入端当时的用户表长什么样"，
而"题库那些表是空的"这件事**可以被用例断言**，比一句"我们没导题库"可靠。

## 一条不许省的纪律：抹密钥

`app_settings.data.ai.apiKey` 是用户自己填的密钥，而**包会被拷来拷去、
发出去、进网盘**。`tools/db_snapshot.py` 为这件事翻过一次车（一把真密钥跟着
快照上了公开仓库，事后只能换密钥），这里照同一条规矩：导出**默认抹空**，
`--with-secrets` 才带上。导入时反过来 —— 包里没有密钥就**保留本机那一把**，
免得"恢复一次数据把刚填好的密钥弄没了"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from . import models as _models  # noqa: F401  —— 先让 `Base.metadata` 长齐
from .config import get_settings
from .db import Base

#: 包的自我介绍。导入端拿它对版本，不认识的直接拒绝（**别猜**）。
FORMAT = "quizforge.user-pack"
FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.json"
DB_NAME = "db/user.db"
FILES_PREFIX = "files/"

#: 设置表里那个密钥的路径（`app_settings.data` 那棵树）。与
#: `tools/db_snapshot.py` 的 `_SECRET_PATH` 是同一个位置，改名要一起改。
SECRET_PATH = ("ai", "apiKey")

SETTINGS_TABLE = "app_settings"


class PackError(Exception):
    """包本身的问题（格式不对、版本不认识、内容对不上）。"""


class PackRefused(PackError):
    """本机已有数据，而调用方没给 `force` —— 拒绝覆盖。"""


# ============================================================================ 边界

#: 表 → 归属。**每一张表都必须在这里出现恰好一次**（见 `check_boundary`）。
#: 分错类的后果不是"少搬一点"，而是"把不该搬的搬走了"，所以宁可让它抛。
BOUNDARY: dict[str, str] = {
    # ---- 题库：出题机产出，用户可选导入（题库包走 pipeline/bankfile.py）----
    "bank_versions": "bank",      # 一次导入的快照记录（指纹、题数、时间）
    "topics": "bank",             # 考纲树（学科 → 单元 → 知识点）
    "topic_groups": "bank",       # 考纲一级分组（叫什么、排第几）
    "meta_documents": "bank",     # 单文件配置正文（topics.yaml 的快照）
    "questions": "bank",          # 题目本体（payload + 原文）
    "point_candidates": "bank",   # 抽取候选（归并的原料，可重跑）
    "materials": "bank",          # 材料（一个源文件 = 一个知识空间）
    "material_slices": "bank",    # 材料切片
    "material_figures": "bank",   # 材料里的图
    "concepts": "bank",           # 概念（图节点）
    "concept_edges": "bank",      # 概念语义边
    "question_concepts": "bank",  # 题 ↔ 概念
    "knowledge_points": "bank",   # 知识点（概念在材料里的一次出现）
    "point_sources": "bank",      # 点 → 原文行区间
    "point_edges": "bank",        # 点 ↔ 点（带类型）
    "question_points": "bank",    # 题 ↔ 点
    # ---- 用户：自己写的 + 自己的进度。用户包带的就是这些 ----
    "app_settings": "user",       # 本机设置 + 选题篮 + 置顶（全表一行）
    "my_questions": "user",       # 用户自出的题
    "conversations": "user",      # 对话
    "conversation_folders": "user",  # 对话分组
    "messages": "user",           # 消息（parts 是真相）
    "attachments": "user",        # 对话附件
    "records": "user",            # 每题累计学习状态（掌握度就是它）
    "attempts": "user",           # 逐次作答流水
    "days": "user",               # 每日聚合（热力图）
    "ai_usage": "user",           # 按天的 AI 用量（自己的账）
    # ---- 派生：能重算的，谁的包都不带 ----
    "slice_embeddings": "derived",  # 窗口向量（本机 168MB，占库 82%；`make embed` 重建）
    "embed_runs": "derived",        # 向量化跑单（运维账，可丢）
}


def _by_kind(kind: str) -> tuple[str, ...]:
    return tuple(sorted(name for name, got in BOUNDARY.items() if got == kind))


USER_TABLES: tuple[str, ...] = _by_kind("user")
BANK_TABLES: tuple[str, ...] = _by_kind("bank")
DERIVED_TABLES: tuple[str, ...] = _by_kind("derived")

#: 判定"本机已经有用户数据了吗"时看的表：设置表不算 —— 它全表一行，
#: 应用一起来就有一行默认值，拿它当依据的话每一台机器都会要求 `--force`。
GUARD_TABLES: tuple[str, ...] = tuple(t for t in USER_TABLES if t != SETTINGS_TABLE)


def check_boundary() -> None:
    """表结构与边界声明必须严丝合缝。多一张、少一张、类型不认识 —— 都抛。"""
    known = set(Base.metadata.tables)
    declared = set(BOUNDARY)

    if known != declared:
        missing = sorted(known - declared)
        stale = sorted(declared - known)
        parts = []
        if missing:
            parts.append(f"库里多了没分类的表 {missing}")
        if stale:
            parts.append(f"边界里声明了库里没有的表 {stale}")
        raise AssertionError(
            "数据边界与表结构对不上：" + "；".join(parts)
            + "。请到 app/datapack.py 的 BOUNDARY 里给每张表定一个归属"
            "（bank = 出题机产物 / user = 用户自己的 / derived = 可重算）。"
        )

    unknown = sorted({kind for kind in BOUNDARY.values()} - {"user", "bank", "derived"})
    if unknown:
        raise AssertionError(f"BOUNDARY 里有不认识的类别：{unknown}（只认 user / bank / derived）")


# ============================================================================ 库与文件

def db_file(settings: Any | None = None) -> Path:
    """当前库是哪一份文件。local-first 下就是一个 SQLite 文件。"""
    settings = settings or get_settings()
    url = str(settings.database_url or "")
    if not url.startswith("sqlite:///"):
        raise PackError(
            "这个工具只认 SQLite（local-first 的那份库文件）。"
            f"当前 DATABASE_URL={url or '(空)'}"
        )
    path = Path(url[len("sqlite:///"):])
    if str(path) == ":memory:":
        raise PackError("当前是内存库，没有文件可打包。")
    return path


def data_dir(settings: Any | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.data_dir)


def _collect_files(root: Path, *, with_library_text: bool) -> list[tuple[str, Path, Path]]:
    """要进包的文件：`[(归属键, 绝对路径, 相对 data_dir)]`。

    只有两样是用户自己的：`notes/`（整棵，含 `.trash` 与 `.snapshots`）与
    `library/*.yaml`（元数据的权威那一层）。`library/.text/` 是**抽出来的正文** ——
    派生，但重建要原始文件，而源目录在仓库外，所以做成开关而不是直接丢掉。
    """
    out: list[tuple[str, Path, Path]] = []

    notes = root / "notes"
    if notes.is_dir():
        for path in sorted(notes.rglob("*")):
            if path.is_file():
                out.append(("notes", path, path.relative_to(root)))

    library = root / "library"
    if library.is_dir():
        for path in sorted(library.glob("*.yaml")):
            out.append(("library", path, path.relative_to(root)))
        if with_library_text:
            text_dir = library / ".text"
            if text_dir.is_dir():
                for path in sorted(text_dir.rglob("*")):
                    if path.is_file():
                        out.append(("libraryText", path, path.relative_to(root)))

    return out


def _file_stats(files: list[tuple[str, Path, Path]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for key, path, _rel in files:
        slot = stats.setdefault(key, {"files": 0, "bytes": 0})
        slot["files"] += 1
        slot["bytes"] += path.stat().st_size
    return stats


# ============================================================================ SQLite 小工具

def _backup(src: Path, dst: Path) -> None:
    """一致性快照。

    **不能直接拷文件**：库开着 WAL 时还有没落盘的事务，拷出来的可能是半个状态。
    用 SQLite 自己的 backup API（`tools/db_snapshot.py` 为同一个理由也是这么做的）。
    """
    def once(uri: str) -> None:
        source = sqlite3.connect(uri, uri=uri.startswith("file:"))
        try:
            target = sqlite3.connect(dst)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    try:
        once(f"file:{src}?mode=ro")
    except sqlite3.OperationalError:
        # 只读打不开（库停在 WAL 里、`-shm` 又不在）时退一步用普通连接：
        # 那一下会顺手把 WAL 收掉，是 SQLite 的标准行为，不算"改了用户的数据"。
        once(str(src))


def _create_schema(path: Path) -> None:
    """按**运行库同一套 DDL** 建表（`Base.metadata` 是唯一事实来源）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA {schema}.table_info("{table}")').fetchall()
    return [row[1] for row in rows]


def _table_counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    if not path.exists():
        return dict.fromkeys(tables, 0)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts: dict[str, int] = {}
        for table in tables:
            try:
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = 0     # 老的库里可能没有这张表
        return counts
    finally:
        conn.close()


def _replace_tables(
    src: Path,
    dst: Path,
    tables: tuple[str, ...],
    *,
    wipe_first: bool,
) -> dict[str, int]:
    """把 `src` 里这些表的内容搬进 `dst`。

    一个事务：要么全成，要么一行都不动（导入时目标里可能就是用户唯一的数据）。
    列取**交集**：包可能来自一个略旧的版本，缺的列交给服务端默认值，
    多的列（本机新加的）写不进去也不该让整件事失败。
    """
    conn = sqlite3.connect(dst, timeout=30)
    conn.isolation_level = None            # 自己管事务，免得 sqlite3 偷偷先 BEGIN
    try:
        conn.execute("PRAGMA foreign_keys=OFF")   # 先全删再全插，顺序由我们保证
        conn.execute("ATTACH DATABASE ? AS incoming", (str(src),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            if wipe_first:
                for table in tables:
                    conn.execute(f'DELETE FROM main."{table}"')
            moved: dict[str, int] = {}
            for table in tables:
                have = set(_columns(conn, "incoming", table))
                cols = [c for c in _columns(conn, "main", table) if c in have]
                if not cols:
                    moved[table] = 0
                    continue
                names = ", ".join(f'"{c}"' for c in cols)
                conn.execute(
                    f'INSERT INTO main."{table}" ({names})'
                    f' SELECT {names} FROM incoming."{table}"'
                )
                moved[table] = conn.execute(
                    f'SELECT COUNT(*) FROM main."{table}"'
                ).fetchone()[0]
            conn.execute("COMMIT")
            return moved
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        raise PackError(
            f"写不进去（{exc}）。如果服务正在跑（`make api-dev`），先停掉它再导入 —— "
            "库被占用时这个工具宁可失败，也不做半截替换。"
        ) from exc
    finally:
        try:
            conn.execute("DETACH DATABASE incoming")
        except sqlite3.Error:
            pass
        conn.close()


def _schema_hash(path: Path, tables: tuple[str, ...]) -> str:
    """这些表的 DDL 指纹（含索引 —— `sqlite_master.tbl_name` 也指向表）。

    用途只有一个：导入时发现"包是用另一套表结构写的"就提醒一声，
    而不是让人对着一堆 `no such column` 猜。
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        marks = ", ".join("?" for _ in tables)
        rows = conn.execute(
            f"SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            f" AND tbl_name IN ({marks}) ORDER BY type, name",
            tables,
        ).fetchall()
    finally:
        conn.close()
    blob = "\n".join(f"{kind}|{name}|{sql}" for kind, name, sql in rows)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _bank_signature(path: Path) -> str:
    """题库的**活体**签名：`条数:每道题 id+content_hash 的哈希`。

    为什么不能只看 `bank_versions`：那一行是**账**，而账会旧 —— 实测这台机器上
    它是 9-16 号那一次导入留下的（109 题），而 `questions` 表里躺着 1623 道题。
    原因见 `pipeline/bankfile.py` 的 `import_bundle`：它一直没写版本行。

    **为什么不把 `updated_at` 算进来**：那是本机时间戳 —— 题目导入时由
    `server_default` 写成"此刻"，两台机器导同一份包也会各不相同，拿它当指纹
    只会假报警。`id` 与 `content_hash` 都是随包原样搬过来的，才算数。
    """
    if not path.exists():
        return ""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT id, COALESCE(content_hash, '') FROM questions"
                " WHERE retired_at IS NULL ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return ""
    finally:
        conn.close()
    if not rows:
        return "0:"
    blob = "\n".join(f"{qid}|{digest}" for qid, digest in rows)
    return f"{len(rows)}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def _bank_fingerprint(path: Path) -> dict[str, Any]:
    """本机装的是哪一份题库（`bank_versions` 里 `is_current` 那一行）。

    用户包记下它，导入端才好回答"这份进度配的是哪份题库"。带两样东西：
    `contentHash`（应用自己认的那个版本指纹）与 `signature`（直接量出来的内容签名）。
    两者都可能有一边是旧的，所以导入端**任何一个对上就算同一份**。
    """
    empty = {"contentHash": "", "questions": 0, "topics": 0, "importedAt": "", "signature": ""}
    if not path.exists():
        return empty
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(
                "SELECT content_hash, question_count, topic_count, imported_at FROM bank_versions"
                " WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
    finally:
        conn.close()
    if row is None:
        return {**empty, "signature": _bank_signature(path)}
    return {
        "contentHash": row[0] or "",
        "questions": int(row[1] or 0),
        "topics": int(row[2] or 0),
        "importedAt": str(row[3] or ""),
        "signature": _bank_signature(path),
    }


def _scrub_secrets(path: Path) -> bool:
    """抹掉设置表里的 AI 密钥，返回"抹了没有"。

    照 `tools/db_snapshot.py` 的同一条纪律，连"只动有值的"这条也一样：
    一个字段都不多改，免得把设置里别的东西搅坏。
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"SELECT id, data FROM {SETTINGS_TABLE}").fetchone()
        if row is None:
            return False
        row_id, raw = row
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        node = data.get(SECRET_PATH[0])
        if not isinstance(node, dict) or not node.get(SECRET_PATH[1]):
            return False            # 没有、或本来就是空的
        node[SECRET_PATH[1]] = ""
        conn.execute(
            f"UPDATE {SETTINGS_TABLE} SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), row_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _read_secret(path: Path) -> str:
    """库里的 AI 密钥（没有就返回空串）。"""
    if not path.exists():
        return ""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(f"SELECT data FROM {SETTINGS_TABLE}").fetchone()
        except sqlite3.OperationalError:
            return ""               # 老库里可能没有这张表
    finally:
        conn.close()
    if row is None:
        return ""
    try:
        data = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else row[0]
    except (ValueError, TypeError):
        return ""
    node = (data or {}).get(SECRET_PATH[0]) if isinstance(data, dict) else None
    return str(node.get(SECRET_PATH[1]) or "") if isinstance(node, dict) else ""


def _write_secret(path: Path, key: str) -> bool:
    """把密钥写回设置表（其余字段不动）。"""
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"SELECT id, data FROM {SETTINGS_TABLE}").fetchone()
        if row is None:
            return False
        row_id, raw = row
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        node = data.setdefault(SECRET_PATH[0], {})
        if not isinstance(node, dict):
            return False
        node[SECRET_PATH[1]] = key
        conn.execute(
            f"UPDATE {SETTINGS_TABLE} SET data = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), row_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ============================================================================ 导出

def _exclusions(*, with_library_text: bool, with_files: bool) -> dict[str, str]:
    """manifest 里那份"没带什么、为什么、怎么补"的清单。

    **写进包里而不是只写在注释里**：拿到包的人（或几个月后的自己）第一件事
    就是问这个，而那一刻他手上只有这个包。
    """
    out = {
        "bank/*": "题库属于题库包（make bank-export / bank-import），用户包不带",
        "slice_embeddings": "派生：窗口向量，`make embed` 重建（本机 168MB，占库 82%）",
        "embed_runs": "派生：向量化跑单，可丢",
        "data/cache": "可再下载的运行时缓存（ONNX 模型 / Pyodide），不进包",
        "data/logs": "运行日志",
        "library 的源文件": "资料库的源 PDF/HTML 在仓库外的 reference/，包里不带；"
                            "要能看原文，那份目录得自己跟着走",
    }
    if not with_files:
        out["notes / library"] = "本次带了 --no-files：只导库里的用户数据，文件一律不带"
    if not with_library_text:
        out["library/.text"] = (
            "抽出的正文（本机 251MB）。默认不带：它是派生物，重建要源文件。"
            "想要异地的资料库也能搜，用 --with-library-text"
        )
    return out


def _same_bank(one: dict[str, Any], other: dict[str, Any]) -> bool:
    """两边是不是同一份题库。任一条拿得出来且相等就算（见 `_bank_signature`）。"""
    for key in ("signature", "contentHash"):
        mine, theirs = one.get(key) or "", other.get(key) or ""
        if mine and theirs and mine == theirs:
            return True
    return False


def export_user(
    out: Path,
    *,
    db_path: Path | None = None,
    root: Path | None = None,
    with_library_text: bool = False,
    with_files: bool = True,
    with_secrets: bool = False,
) -> dict[str, Any]:
    """把本机的用户数据打成一个包。返回这份包的 manifest。"""
    check_boundary()
    settings = get_settings()
    db_path = Path(db_path) if db_path else db_file(settings)
    root = Path(root) if root else data_dir(settings)
    if not db_path.exists():
        raise PackError(f"找不到库文件：{db_path}")

    out = Path(out)
    files = _collect_files(root, with_library_text=with_library_text) if with_files else []

    with tempfile.TemporaryDirectory(prefix="qf-pack-") as tmp:
        work = Path(tmp)
        snapshot = work / "snapshot.db"
        _backup(db_path, snapshot)                  # 一致性快照（WAL 也安全）

        pack_db = work / "user.db"
        _create_schema(pack_db)                     # 与运行库同一套 DDL
        moved = _replace_tables(snapshot, pack_db, USER_TABLES, wipe_first=False)
        scrubbed = False if with_secrets else _scrub_secrets(pack_db)

        manifest: dict[str, Any] = {
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
            "exportedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": {"db": str(db_path), "dataDir": str(root)},
            "schema": {
                "userTablesHash": _schema_hash(pack_db, USER_TABLES),
                "tables": list(USER_TABLES),
            },
            "bank": _bank_fingerprint(snapshot),
            "tables": moved,
            "files": _file_stats(files),
            "secrets": {"aiKeyScrubbed": scrubbed},
            "excluded": _exclusions(with_library_text=with_library_text, with_files=with_files),
        }

        out.parent.mkdir(parents=True, exist_ok=True)
        # 先写 .part 再改名：别留下一个"看着像包、其实是半截"的文件
        partial = out.with_name(out.name + ".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=1, default=str),
            )
            archive.write(pack_db, DB_NAME)
            for _key, path, rel in files:
                archive.write(path, FILES_PREFIX + rel.as_posix())
        partial.replace(out)

    manifest["pack"] = {"path": str(out), "bytes": out.stat().st_size}
    return manifest


# ============================================================================ 读包

def read_manifest(pack: Path) -> dict[str, Any]:
    """只读 manifest —— 不展开包，`show` 与导入的第一步都走它。"""
    pack = Path(pack)
    if not pack.exists():
        raise PackError(f"找不到包：{pack}")
    try:
        with zipfile.ZipFile(pack) as archive:
            raw = archive.read(MANIFEST_NAME)
    except KeyError as exc:
        raise PackError(f"{pack.name} 里没有 {MANIFEST_NAME}：这不是一个数据包。") from exc
    except zipfile.BadZipFile as exc:
        raise PackError(f"{pack.name} 不是 zip（或已损坏）：{exc}") from exc

    manifest = json.loads(raw.decode("utf-8"))
    if manifest.get("format") != FORMAT:
        raise PackError(
            f"{pack.name} 是 {manifest.get('format')!r}，不是用户数据包（{FORMAT}）。"
            "题库包走 `make bank-import`。"
        )
    if int(manifest.get("formatVersion") or 0) != FORMAT_VERSION:
        raise PackError(
            f"包的格式版本是 {manifest.get('formatVersion')}，这份代码只认 {FORMAT_VERSION}。"
        )
    return manifest


def _extract_db(pack: Path, work: Path) -> Path:
    try:
        with zipfile.ZipFile(pack) as archive:
            archive.extract(DB_NAME, work)
    except KeyError as exc:
        raise PackError(f"{pack.name} 里没有 {DB_NAME}。") from exc
    return work / DB_NAME


# ============================================================================ 导入

def import_user(
    pack: Path,
    *,
    db_path: Path | None = None,
    root: Path | None = None,
    force: bool = False,
    with_files: bool = True,
) -> dict[str, Any]:
    """把包里的用户数据放回本机。

    题库**一行都不动**：包里那些表本来就该是空的（有用例钉住），真有内容也只是
    提醒一声然后跳过 —— 那是题库包的事。
    """
    check_boundary()
    pack = Path(pack)
    manifest = read_manifest(pack)
    settings = get_settings()
    db_path = Path(db_path) if db_path else db_file(settings)
    root = Path(root) if root else data_dir(settings)

    with tempfile.TemporaryDirectory(prefix="qf-user-pack-") as tmp:
        work = Path(tmp)
        pack_db = _extract_db(pack, work)

        # 包自己的体检：半截的包宁可当场拒绝，也别导进去做出一份"少了半个月"的进度
        conn = sqlite3.connect(f"file:{pack_db}?mode=ro", uri=True)
        try:
            problems = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
        if problems and problems[0][0] != "ok":
            raise PackError(f"包里的库不完整：{problems[0][0]}")

        packed_counts = _table_counts(pack_db, USER_TABLES + BANK_TABLES)
        stray = {t: n for t, n in packed_counts.items() if t in BANK_TABLES and n}

        created = not db_path.exists()
        if created:
            _create_schema(db_path)

        occupied = _table_counts(db_path, GUARD_TABLES)
        occupied = {t: n for t, n in occupied.items() if n}
        if occupied and not force:
            detail = "、".join(f"{t} {n} 行" for t, n in sorted(occupied.items()))
            raise PackRefused(
                f"本机已经有用户数据（{detail}），不加 --force 我不动它。\n"
                f"  想先留一份现在的：make user-export\n"
                f"  确认要覆盖：再加 --force"
            )

        # 本机那一把密钥要在**替换之前**读出来 —— 替换会把设置表整行盖掉。
        # 密钥不是进度，它跟着机器走：包里没有就留着本机这一把。
        local_secret = _read_secret(db_path)

        moved = _replace_tables(pack_db, db_path, USER_TABLES, wipe_first=True)

        packed_secret = _read_secret(pack_db)
        kept_secret = False
        if not packed_secret and local_secret:
            kept_secret = _write_secret(db_path, local_secret)

        written = 0
        if with_files:
            with zipfile.ZipFile(pack) as archive:
                for info in archive.infolist():
                    if not info.filename.startswith(FILES_PREFIX) or info.is_dir():
                        continue
                    rel = info.filename[len(FILES_PREFIX):]
                    target = root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, open(target, "wb") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    written += 1

        now_bank = _bank_fingerprint(db_path)
        was_bank = manifest.get("bank") or {}
        return {
            "manifest": manifest,
            "db": str(db_path),
            "created": created,
            "overwrote": bool(occupied),
            "tables": moved,
            "files": written,
            "keptLocalSecret": kept_secret,
            "bank": {
                "pack": was_bank,
                "local": now_bank,
                # 内容签名比版本账可靠（见 `_bank_signature`），所以先看它；
                # 签名拿不到（老库没那个列）时退回版本指纹。
                "matches": _same_bank(was_bank, now_bank),
            },
            "strayBankRows": stray,
        }


def describe(pack: Path) -> dict[str, Any]:
    """看一个包里有什么（只读，不展开）。"""
    manifest = read_manifest(pack)
    with tempfile.TemporaryDirectory(prefix="qf-qf-show-") as tmp:
        pack_db = _extract_db(pack, Path(tmp))
        counts = _table_counts(pack_db, BANK_TABLES)
        schema_ok = _schema_hash(pack_db, USER_TABLES) == (manifest.get("schema") or {}).get(
            "userTablesHash"
        )
    return {
        "manifest": manifest,
        "bankRowsInPack": {t: n for t, n in counts.items() if n},
        "schemaUnchanged": schema_ok,
        "bytes": Path(pack).stat().st_size,
    }
