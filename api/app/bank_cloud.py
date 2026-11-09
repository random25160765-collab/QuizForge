"""题库整包上云 / 下云（题目 + 考纲 + 知识图谱，**不含材料正文**）。

## 为什么需要这条通道

`library_cloud` 只搬 `materials` / `material_slices` / `material_figures`，
行级 `sync` 只搬用户数据。题库剩下那半（题目、考纲、概念、点、边）以前没有云端
通道 —— 只能靠 **打包时塞进 APK 的 62MB `bank.db`**。于是每台设备都带着一份打包
那天的快照，题库更新了也看不见。

这条通道把这半也搬上 Turso，APK 里就不再需要 `bank.db`：新设备首启联网拉一次。

## 分工（别重复搬）

| 谁 | 搬什么 |
| --- | --- |
| `library_cloud` | materials · material_slices · material_figures |
| `bank_cloud` | topics · topic_groups · meta_documents · questions · concepts · concept_edges · knowledge_points · point_sources · point_edges · question_concepts · question_points |
| `sync` | 用户数据（对话 / 记录 / 计划 / 日程 / 记忆 …） |

`point_candidates`（归并的原料，能重跑）与 `bank_versions`（本机导入记录）
**不搬**：前者可再生成，后者是这台机器的历史。

## 为什么一律用业务键

`concepts.id` / `knowledge_points.id` 是自增列，两台机器上同一份题库 id 不同。
所以包里只放业务键（概念 `key`、点 `(material.slug, point.key)`、题 `id`），
拉取时边写边建映射，绝不赌 id 一致。

## 分块

`questions` 13MB（每题的 `payload` + `raw_markdown`）。Turso 单次 pipeline 在
几 MB 上会被掐断（`library_cloud` 实测 6.9MB 就断），所以题目按 768KB 切块写
`qf_bank_block`；结构性小表（概念 / 点 / 边，合计约 1MB）随元数据走。

## 安全阀

本机一道题都没有时**绝不推空包** —— 云端可能是另一台机器整理好的题库，
推一个空包上去等于抹掉它（与 `library_cloud` 同一条规矩）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text

from . import sync
from .models import (
    Concept,
    ConceptEdge,
    KnowledgePoint,
    Material,
    MetaDocument,
    PointEdge,
    PointSource,
    Question,
    QuestionConcept,
    QuestionPoint,
    Topic,
    TopicGroup,
)

FORMAT = "quizforge.bank"
VERSION = 1

PACK_ID = 1

#: 题目一块多少字节。与 `library_cloud.TEXT_BLOCK_BYTES` 同一个经验值。
BLOCK_BYTES = 768 * 1024

REMOTE_TABLE = """
CREATE TABLE IF NOT EXISTS qf_bank (
  id      INTEGER PRIMARY KEY,
  at      REAL NOT NULL,
  origin  TEXT NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""

REMOTE_BLOCK_TABLE = """
CREATE TABLE IF NOT EXISTS qf_bank_block (
  idx     INTEGER PRIMARY KEY,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""

#: 自动同步的两把标尺（存在 `qf_sync_state`，本机状态、不参与同步）
_STATE_AT = "bank_pack_at"
_STATE_FP = "bank_pack_fp"


def _client() -> Any:
    cfg = sync.turso_config()
    if not cfg.get("url") or not cfg.get("token"):
        raise RuntimeError(
            "还没配云同步：先在设置里填 Turso 地址与令牌（或写 `config/settings.local.json` 的 turso 块）"
        )
    return sync.TursoClient(cfg["url"], cfg["token"])


def _origin(db: Any) -> str:
    """本机标识。**用会话自己的引擎**，不要 `get_engine()` —— 后者读的是进程级
    默认库，测试里（以及任何"同时操作两个库"的场景）会指错地方。"""
    bind = db.get_bind()
    return sync._origin(getattr(bind, "engine", bind))  # noqa: SLF001


def _state_get(db: Any, key: str) -> str:
    row = db.execute(text("SELECT v FROM qf_sync_state WHERE k = :k"), {"k": key}).first()
    return str(row[0]) if row is not None and row[0] is not None else ""


def _state_set(db: Any, key: str, value: str) -> None:
    db.execute(
        text(
            "INSERT INTO qf_sync_state(k, v) VALUES(:k, :v)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v"
        ),
        {"k": key, "v": value},
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _apply(hit: Any, item: dict, fields: tuple[str, ...]) -> None:
    for name in fields:
        if name not in item:
            continue
        value = item[name]
        if name in ("retired_at", "centric_at"):
            value = _dt(value)
        setattr(hit, name, value)


# ------------------------------------------------------------------ 打包


_TOPIC_FIELDS = (
    "key", "name", "group_key", "order_index", "color", "desc", "depth", "parent_key",
    "path", "path_names", "children", "descendants", "is_leaf", "retired_at",
)
_CONCEPT_FIELDS = (
    "key", "name", "kind", "definition", "topic_key", "aliases", "point_count",
    "material_count", "question_count", "status", "confidence", "centric_at",
)
_QUESTION_FIELDS = (
    "id", "type", "topic", "difficulty", "source", "status", "layer", "wing", "chapter",
    "verify_report", "raw_markdown", "payload", "content_hash", "retired_at",
)


def _row(item: Any, fields: tuple[str, ...]) -> dict:
    return {name: getattr(item, name) for name in fields}


def pack(db: Any) -> dict:
    """结构性小表打成一份 JSON（**不含题目正文**，也不含材料三张表）。

    材料那三张由 `library_cloud` 负责；这里只放"题库的骨头" —— 考纲、概念、
    点、边、题与点的关系。题目本身走 `question_blocks()` 分块。
    """
    slug_of = {m.id: m.slug for m in db.scalars(select(Material)).all()}

    topics = []
    for item in db.scalars(select(Topic)):
        one = _row(item, _TOPIC_FIELDS)
        one["retired_at"] = _iso(item.retired_at)
        one["updated_at"] = _iso(item.updated_at)
        topics.append(one)

    topic_groups = [
        {"key": g.key, "name": g.name, "order_index": g.order_index}
        for g in db.scalars(select(TopicGroup))
    ]

    meta = [
        {"key": d.key, "content": d.content, "updated_at": _iso(d.updated_at)}
        for d in db.scalars(select(MetaDocument))
    ]

    concepts = []
    for item in db.scalars(select(Concept)):
        one = _row(item, _CONCEPT_FIELDS)
        one["centric_at"] = _iso(item.centric_at)
        concepts.append(one)

    concept_key_of = {c.id: c.key for c in db.scalars(select(Concept)).all()}

    points = []
    point_key_of: dict[int, tuple[str, str]] = {}
    for p in db.scalars(select(KnowledgePoint)):
        slug = slug_of.get(p.material_id)
        if not slug:
            continue
        point_key_of[p.id] = (slug, p.key)
        points.append(
            {
                "material": slug,
                "key": p.key,
                "concept": concept_key_of.get(p.concept_id) if p.concept_id else None,
                "name": p.name,
                "kind": p.kind,
                "thickness": p.thickness,
                "layers": p.layers,
                "producible": p.producible,
                "note": p.note,
            }
        )

    sources = []
    for s in db.scalars(select(PointSource)):
        key = point_key_of.get(s.point_id)
        if not key:
            continue
        sources.append(
            {
                "material": key[0],
                "point_key": key[1],
                "slice_id": s.slice_id,
                "start_line": s.start_line,
                "end_line": s.end_line,
            }
        )

    point_edges = []
    for e in db.scalars(select(PointEdge)):
        src = point_key_of.get(e.from_point_id)
        dst = point_key_of.get(e.to_point_id)
        if not src or not dst:
            continue
        point_edges.append(
            {
                "material": src[0],
                "from_key": src[1],
                "to_key": dst[1],
                "type": e.type,
                "why": e.why,
                "derived_by": e.derived_by,
            }
        )

    concept_edges = []
    for e in db.scalars(select(ConceptEdge)):
        src = concept_key_of.get(e.from_concept_id)
        dst = concept_key_of.get(e.to_concept_id)
        if not src or not dst:
            continue
        concept_edges.append(
            {
                "from_key": src,
                "to_key": dst,
                "type": e.type,
                "why": e.why,
                "derived_by": e.derived_by,
                "weight": e.weight,
            }
        )

    question_concepts = []
    for link in db.scalars(select(QuestionConcept)):
        key = concept_key_of.get(link.concept_id)
        if key:
            question_concepts.append({"question": link.question_id, "concept": key})

    question_points = []
    for link in db.scalars(select(QuestionPoint)):
        key = point_key_of.get(link.point_id)
        if key:
            question_points.append(
                {
                    "question": link.question_id,
                    "material": key[0],
                    "point_key": key[1],
                    "is_primary": link.is_primary,
                }
            )

    return {
        "format": FORMAT,
        "version": VERSION,
        "topics": topics,
        "topic_groups": topic_groups,
        "meta_documents": meta,
        "concepts": concepts,
        "points": points,
        "point_sources": sources,
        "point_edges": point_edges,
        "concept_edges": concept_edges,
        "question_concepts": question_concepts,
        "question_points": question_points,
    }


def question_blocks(db: Any, *, limit: int = BLOCK_BYTES) -> list[tuple[int, str, int]]:
    """题目 → 若干块 `(idx, json, bytes)`，每块不超过 `limit`。"""
    blocks: list[tuple[int, str, int]] = []
    buf: list[dict] = []
    size = 0
    for q in db.scalars(select(Question).order_by(Question.id)):
        item = _row(q, _QUESTION_FIELDS)
        item["retired_at"] = _iso(q.retired_at)
        chunk = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if buf and size + chunk > limit:
            blob = json.dumps(buf, ensure_ascii=False)
            blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
            buf, size = [], 0
        buf.append(item)
        size += chunk
    if buf:
        blob = json.dumps(buf, ensure_ascii=False)
        blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
    return blocks


#: 指纹只覆盖"内容"：传输元信息（块数 / 字节数 / at / origin）不参与，
#: 否则推送前后算出来的值不一样，云端与本地永远对不上。
_FP_KEYS = (
    "topics", "topic_groups", "meta_documents", "concepts", "points",
    "point_sources", "point_edges", "concept_edges", "question_concepts", "question_points",
)


def _question_hashes(db: Any) -> list[tuple[str, str]]:
    return list(
        db.execute(select(Question.id, Question.content_hash).order_by(Question.id)).all()
    )


def _digest(data: dict, hashes: list[tuple[str, str]]) -> str:
    """结构 + 题目内容哈希 → 一个十六进制串。"""
    slim = {name: data.get(name, []) for name in _FP_KEYS}
    blob = json.dumps(slim, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8"))
    trail = "\n".join(f"{qid}:{h}" for qid, h in hashes)
    digest.update(b"\n" + trail.encode("utf-8"))
    return digest.hexdigest()


def fingerprint(db: Any) -> str:
    """本地题库的指纹：小表直接哈希，题目只看 `(id, content_hash)`。

    不读 `payload` 是刻意的 —— 13MB 读一遍只为算指纹不划算，而 `content_hash`
    就是"这道题变了没有"的权威判据（`questions` 表自己的注释也这么说）。
    """
    return _digest(pack(db), _question_hashes(db))


# ------------------------------------------------------------------ 推 / 拉


def push(db: Any, *, remote: Any | None = None) -> dict:
    """把整包（结构 + 题目分块）写到云端，覆盖上一次。"""
    data = pack(db)
    # 指纹要盖住"题目内容变了但行数没变"的情况，所以在这里算好一起写进去；
    # `_digest` 只取 `_FP_KEYS`，下面的传输字段不会污染它。
    data["fingerprint"] = _digest(data, _question_hashes(db))
    blocks = question_blocks(db)
    data["questionBlocks"] = len(blocks)
    data["questionBytes"] = sum(size for _, _, size in blocks)
    data["questionCount"] = int(db.scalar(select(func.count()).select_from(Question)) or 0)

    blob = json.dumps(data, ensure_ascii=False)
    client = remote or _client()
    client.execute(REMOTE_TABLE)
    client.execute(REMOTE_BLOCK_TABLE)
    pushed_at = time.time()
    client.execute(
        "INSERT OR REPLACE INTO qf_bank (id, at, origin, bytes, payload) VALUES (?, ?, ?, ?, ?)",
        (PACK_ID, pushed_at, _origin(db), len(blob.encode("utf-8")), blob),
    )
    client.execute("DELETE FROM qf_bank_block")
    for idx, payload, size in blocks:
        client.execute(
            "INSERT INTO qf_bank_block (idx, bytes, payload) VALUES (?, ?, ?)",
            (idx, size, payload),
        )
    return {
        "at": pushed_at,
        "questions": data["questionCount"],
        "topics": len(data["topics"]),
        "concepts": len(data["concepts"]),
        "points": len(data["points"]),
        "bytes": len(blob.encode("utf-8")),
        "questionBlocks": len(blocks),
        "questionBytes": data["questionBytes"],
    }


def _upsert_by_key(db: Any, model: Any, items: list[dict], fields: tuple[str, ...]) -> dict[str, Any]:
    """按主键 `key` 对齐小表，返回 `key → 实例`。"""
    existing = {getattr(row, "key"): row for row in db.scalars(select(model)).all()}
    for item in items:
        key = str(item.get("key") or "")
        if not key:
            continue
        hit = existing.get(key)
        if hit is None:
            hit = model(key=key)
            db.add(hit)
            existing[key] = hit
        _apply(hit, item, fields)
    return existing


def pull(db: Any, *, remote: Any | None = None) -> dict:
    """从云端拉整包，按业务键 upsert（材料必须已经由 `library_cloud` 拉过）。"""
    client = remote or _client()
    try:
        rows = client.query("SELECT payload, bytes FROM qf_bank WHERE id = ?", (PACK_ID,))
    except Exception:  # noqa: BLE001 - 表还没建过
        return {"empty": True, "questions": 0, "points": 0, "concepts": 0}
    if not rows:
        return {"empty": True, "questions": 0, "points": 0, "concepts": 0}

    data = json.loads(rows[0]["payload"] or "{}")
    counts: dict[str, int] = defaultdict(int)

    # 1) 考纲与配置文件
    _upsert_by_key(db, Topic, data.get("topics", []), _TOPIC_FIELDS)
    _upsert_by_key(db, TopicGroup, data.get("topic_groups", []), ("name", "order_index"))
    _upsert_by_key(db, MetaDocument, data.get("meta_documents", []), ("content",))
    counts["topics"] = len(data.get("topics", []))

    # 2) 概念（后面所有边都靠 key 映射回来）
    concept_of = _upsert_by_key(db, Concept, data.get("concepts", []), _CONCEPT_FIELDS)
    counts["concepts"] = len(data.get("concepts", []))
    db.flush()

    # 3) 题目：从分块里读，按 id 覆盖
    questions = {q.id: q for q in db.scalars(select(Question)).all()}
    try:
        blocks = client.query("SELECT payload FROM qf_bank_block ORDER BY idx")
    except Exception:  # noqa: BLE001 - 老包里没有这张表
        blocks = []
    for block in blocks:
        try:
            items = json.loads(block.get("payload") or "[]")
        except ValueError:
            continue
        for item in items:
            qid = str(item.get("id") or "")
            if not qid:
                continue
            hit = questions.get(qid)
            if hit is None:
                hit = Question(id=qid)
                db.add(hit)
                questions[qid] = hit
            _apply(hit, item, _QUESTION_FIELDS)
            counts["questions"] += 1
        counts["questionBlocks"] += 1
    db.flush()

    # 4) 点：靠 (material.slug, key) 落到本机的自增 id 上
    materials = {m.slug: m for m in db.scalars(select(Material)).all()}
    slug_of_id = {m.id: slug for slug, m in materials.items()}
    point_of: dict[tuple[str, str], KnowledgePoint] = {}
    for p in db.scalars(select(KnowledgePoint)).all():
        slug = slug_of_id.get(p.material_id)
        if slug:
            point_of[(slug, p.key)] = p

    for item in data.get("points", []):
        material = materials.get(str(item.get("material") or ""))
        if material is None:
            # 材料还没到（library_cloud 没拉过）：跳过，下一轮再补
            counts["skippedPoints"] += 1
            continue
        key = (material.slug, str(item.get("key") or ""))
        if not key[1]:
            continue
        hit = point_of.get(key)
        if hit is None:
            hit = KnowledgePoint(material_id=material.id, key=key[1])
            db.add(hit)
            point_of[key] = hit
        hit.name = str(item.get("name") or "")
        hit.kind = str(item.get("kind") or "noun")
        hit.thickness = int(item.get("thickness") or 1)
        hit.layers = item.get("layers") or []
        hit.producible = bool(item.get("producible", True))
        hit.note = str(item.get("note") or "")
        concept_key = str(item.get("concept") or "")
        concept = concept_of.get(concept_key)
        hit.concept_id = concept.id if concept is not None else None
    counts["points"] = len(data.get("points", []))
    db.flush()

    # 5) 出处
    sources = {
        (s.point_id, s.slice_id, s.start_line, s.end_line): s
        for s in db.scalars(select(PointSource)).all()
    }
    for item in data.get("point_sources", []):
        point = point_of.get((str(item.get("material") or ""), str(item.get("point_key") or "")))
        if point is None:
            continue
        key = (point.id, str(item.get("slice_id") or ""), int(item.get("start_line") or 0),
               int(item.get("end_line") or 0))
        if key not in sources:
            hit = PointSource(
                point_id=key[0], slice_id=key[1], start_line=key[2], end_line=key[3]
            )
            db.add(hit)
            sources[key] = hit
        counts["pointSources"] += 1

    # 6) 点与点的边
    edges = {(e.from_point_id, e.to_point_id, e.type): e for e in db.scalars(select(PointEdge)).all()}
    for item in data.get("point_edges", []):
        src = point_of.get((str(item.get("material") or ""), str(item.get("from_key") or "")))
        dst = point_of.get((str(item.get("material") or ""), str(item.get("to_key") or "")))
        if src is None or dst is None:
            continue
        key = (src.id, dst.id, str(item.get("type") or ""))
        hit = edges.get(key)
        if hit is None:
            hit = PointEdge(from_point_id=key[0], to_point_id=key[1], type=key[2])
            db.add(hit)
            edges[key] = hit
        hit.why = str(item.get("why") or "")
        hit.derived_by = str(item.get("derived_by") or "code")
        counts["pointEdges"] += 1
    db.flush()

    # 7) 概念与概念的边
    concept_edges = {
        (e.from_concept_id, e.to_concept_id, e.type): e
        for e in db.scalars(select(ConceptEdge)).all()
    }
    for item in data.get("concept_edges", []):
        src = concept_of.get(str(item.get("from_key") or ""))
        dst = concept_of.get(str(item.get("to_key") or ""))
        if src is None or dst is None:
            continue
        key = (src.id, dst.id, str(item.get("type") or ""))
        hit = concept_edges.get(key)
        if hit is None:
            hit = ConceptEdge(from_concept_id=key[0], to_concept_id=key[1], type=key[2])
            db.add(hit)
            concept_edges[key] = hit
        hit.why = str(item.get("why") or "")
        hit.derived_by = str(item.get("derived_by") or "code")
        hit.weight = float(item.get("weight") or 1.0)
        counts["conceptEdges"] += 1
    db.flush()

    # 8) 题 ↔ 概念
    links = {(l.question_id, l.concept_id) for l in db.scalars(select(QuestionConcept)).all()}
    for item in data.get("question_concepts", []):
        concept = concept_of.get(str(item.get("concept") or ""))
        qid = str(item.get("question") or "")
        if concept is None or qid not in questions:
            continue
        if (qid, concept.id) in links:
            continue
        db.add(QuestionConcept(question_id=qid, concept_id=concept.id))
        links.add((qid, concept.id))
        counts["questionConcepts"] += 1

    # 9) 题 ↔ 点
    qp_links = {(l.question_id, l.point_id) for l in db.scalars(select(QuestionPoint)).all()}
    for item in data.get("question_points", []):
        qid = str(item.get("question") or "")
        point = point_of.get((str(item.get("material") or ""), str(item.get("point_key") or "")))
        if point is None or qid not in questions:
            continue
        if (qid, point.id) in qp_links:
            continue
        db.add(
            QuestionPoint(
                question_id=qid,
                point_id=point.id,
                is_primary=bool(item.get("is_primary", True)),
            )
        )
        qp_links.add((qid, point.id))
        counts["questionPoints"] += 1

    db.commit()
    return {
        "questions": counts["questions"],
        "topics": counts["topics"],
        "concepts": counts["concepts"],
        "points": counts["points"],
        "pointSources": counts["pointSources"],
        "pointEdges": counts["pointEdges"],
        "conceptEdges": counts["conceptEdges"],
        "questionConcepts": counts["questionConcepts"],
        "questionPoints": counts["questionPoints"],
        "skippedPoints": counts["skippedPoints"],
        "bytes": int(rows[0].get("bytes") or 0),
    }


# ------------------------------------------------------------------ 自动同步


def sync_pack(db: Any, *, remote: Any | None = None) -> dict:
    """自动同步一轮题库包：比指纹，决定推 / 拉 / 都不动。

    规则与 `library_cloud.sync_pack` 完全一致（只有一边变 → 单向；两边都变 →
    先拉后推，取并集），因为它们面对的是同一类问题：整包、无行级时间戳、
    而且贵到不能每分钟全量传。

    首次同步（本地还没有标尺）走"先拉后推"：新设备装完 APK 就是这条路径 ——
    本地空库、云端有题库，拉下来即可；反过来（老设备第一次开这条通道）
    云端没有包，直接推上去。
    """
    client = remote or _client()
    client.execute(REMOTE_TABLE)
    client.execute(REMOTE_BLOCK_TABLE)
    bind = db.get_bind()
    sync.ensure_sync_schema(bind if hasattr(bind, "begin") else bind.engine)

    rows = client.query(
        "SELECT at, origin, bytes, payload FROM qf_bank WHERE id = ?", (PACK_ID,)
    )
    local_count = int(db.scalar(select(func.count()).select_from(Question)) or 0)
    local_fp = fingerprint(db)
    state_at = float(_state_get(db, _STATE_AT) or 0.0)
    state_fp = _state_get(db, _STATE_FP)

    if not rows:
        if not local_count:
            return {"action": "idle", "reason": "两边都还没有题目"}
        out = push(db, remote=client)
        _state_set(db, _STATE_AT, str(out["at"]))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "push", **out, "fingerprint": local_fp}

    remote_row = rows[0]
    remote_at = float(remote_row.get("at") or 0.0)
    try:
        remote_data = json.loads(remote_row.get("payload") or "{}")
    except ValueError:
        remote_data = {}
    remote_fp = remote_data.get("fingerprint") or ""
    remote_changed = remote_at > state_at + 1e-6
    local_changed = local_fp != state_fp

    if not remote_changed and not local_changed:
        return {"action": "idle", "at": remote_at, "fingerprint": local_fp}

    if remote_changed and remote_fp and remote_fp == local_fp:
        _state_set(db, _STATE_AT, str(remote_at))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "same", "at": remote_at, "fingerprint": local_fp}

    if remote_changed and not local_changed:
        out = pull(db, remote=client)
        new_fp = fingerprint(db)
        _state_set(db, _STATE_AT, str(remote_at))
        _state_set(db, _STATE_FP, new_fp)
        db.commit()
        return {"action": "pull", **out, "at": remote_at, "fingerprint": new_fp}

    if local_changed and not remote_changed:
        if not local_count:
            _state_set(db, _STATE_FP, local_fp)
            db.commit()
            return {"action": "idle", "reason": "本机没有题目，不覆盖云端"}
        out = push(db, remote=client)
        _state_set(db, _STATE_AT, str(out["at"]))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "push", **out, "fingerprint": local_fp}

    # 两边都变：先拉齐（云端为基准），再把本机独有的推回去
    pulled = pull(db, remote=client)
    out = push(db, remote=client)
    new_fp = fingerprint(db)
    _state_set(db, _STATE_AT, str(out["at"]))
    _state_set(db, _STATE_FP, new_fp)
    db.commit()
    return {"action": "merge", "pulled": pulled, "pushed": out, "fingerprint": new_fp}
