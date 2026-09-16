"""把 LLM 的输出**直接写进数据库**：草稿 → 已校验 → 已发布。

为什么要有这一层：

* 一题一个文件在几十万题的规模下没法管理（10 本书 = 几十万个小文件）；
* "模型输出 → 落盘 → 搬运 → 再解析"多出三段可以失败的环节，而且会让库与文件漂移
  （实测出现过：库里改了、文件没改）；
* 校验、发布、检索本来就是数据库的事，让每个环节各自去读另一种格式是自找麻烦。

解析仍复用 `tools/question_parser`（与构建、校验**同一套规则**）—— 它是按路径解析的，
所以这里写一个**临时文件**递给它，解析完立刻删掉。那是函数调用的参数，
不是"本地小文件"。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from . import config

sys.path.insert(0, str(config.ROOT / "api"))

def _strip_front(markdown: str | None) -> str:
    """只留正文：库里的 `raw_markdown` 不该带元信息头（前端会把它当正文渲染）。"""
    text = (markdown or "").strip()
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :].lstrip("\n") if end >= 0 else text


# 出题员给的前置字段里，哪些要落成真正的列（其余进 payload）
COLUMNS = ("type", "topic", "difficulty", "layer", "wing", "chapter")


def _engine():
    from app.db import get_engine  # noqa: PLC0415

    return get_engine()


def build_raw(front: dict, question_id: str, body: str) -> str:
    """把"前置字段 + 正文"拼成标准的题目 Markdown（id 由库里分配，覆盖模型写的）。"""
    import json  # noqa: PLC0415

    rows = [f"id: {json.dumps(question_id, ensure_ascii=False)}"]
    for key, value in front.items():
        if key == "id" or value in (None, ""):
            continue
        rows.append(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            if isinstance(value, str)
            else f"{key}: {value}"
        )
    return "---\n" + "\n".join(rows) + "\n---\n\n" + body.strip() + "\n"


def parse_body(question_id: str, text: str) -> dict:
    """Markdown → payload（与 `tools/build.py`、`tools/check.py` 同一套解析规则）。"""
    from app.toolkit import parse_question, question_to_dict  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="qf-parse-"))
    path = tmp / f"{question_id}.md"
    path.write_text(text, encoding="utf-8")
    try:
        payload = question_to_dict(parse_question(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # file 是解析器给的人读路径；临时目录不该进库
    payload["file"] = f"db://{question_id}"
    return payload


def _column(payload: dict, key: str, default=""):
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    value = payload.get(key)
    if value in (None, ""):
        value = (meta or {}).get(key)
    return default if value in (None, "") else value


def next_ids(cursor, subject: str, count: int) -> list[str]:
    """在**库内**分配编号（并发安全）。

    文件时代靠"扫目录取最大值"编号，多个出题包并行必然撞号（实测 60 个材料
    全从 0039 开始）。编号的权威挪进库之后，这件事才有唯一答案：先拿学科级
    咨询锁，读最大值，再往后排 —— 同一事务里完成，别的 worker 只能排队。
    """
    from sqlalchemy import text  # noqa: PLC0415

    cursor.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"id:{subject}"}
    )
    row = cursor.execute(
        text("SELECT id FROM questions WHERE id LIKE :pat ORDER BY id DESC LIMIT 1"),
        {"pat": f"{subject}-%"},
    ).fetchone()
    start = 1
    if row:
        tail = str(row[0]).rsplit("-", 1)[-1]
        if tail.isdigit():
            start = int(tail) + 1
    return [f"{subject}-{n:04d}" for n in range(start, start + count)]


def _resolve_point(item: dict, pack_points: list[str]) -> str:
    """定这道题考的是哪个点：优先作者声明的 `point`，退而求其次按 topic 认，再退就只有一个点时认它。

    这个值将来会写进"题↔点的边"，是**防重复出题**的依据 —— 认不出来宁可空着，
    也不要瞎认（瞎认会让某个点从此不再出题，比重复出题更糟）。
    """
    if not pack_points:
        return ""
    declared = str(item.get("point") or "").strip()
    if declared in pack_points:
        return declared
    topic = str((item.get("front") or {}).get("topic") or "").strip()
    if topic in pack_points:
        return topic
    if len(pack_points) == 1:
        return pack_points[0]
    return ""


def insert_drafts(
    subject: str, items: list[dict], pack_points: list[str] | None = None
) -> tuple[list[dict], list[str]]:
    """一个出题包的产出**一次性入库**（草稿态）。

    `items` 每项：`{"front": {...}, "markdown": "...", "sources": [[起,止], ...]}`。
    返回 (写入的题, 被丢弃的题说明) —— 解析不过的题在这里就被拦下，
    它进不了库，也就不会出现在任何人的复习界面里。
    """
    import json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    written: list[dict] = []
    dropped: list[str] = []
    prepared: list[tuple[dict, str, dict]] = []
    with _engine().begin() as conn:
        ids = next_ids(conn, subject, len(items))
        for question_id, item in zip(ids, items):
            raw = build_raw(item.get("front") or {}, question_id, item.get("markdown") or "")
            try:
                payload = parse_body(question_id, raw)
            except Exception as exc:  # noqa: BLE001 —— 单题格式不合契约只丢它自己
                dropped.append(f"{question_id}（{type(exc).__name__}: {str(exc)[:80]}）")
                continue
            prepared.append((item, raw, payload))

        # 解析失败的题不占编号：按实际写成的数量重新分配
        if len(prepared) < len(ids):
            ids = next_ids(conn, subject, len(prepared))
        for question_id, (item, raw, payload) in zip(ids, prepared):
            if payload.get("id") != question_id:
                raw = build_raw(item.get("front") or {}, question_id, item.get("markdown") or "")
                payload = parse_body(question_id, raw)
            conn.execute(
                text(
                    """
                    INSERT INTO questions (id, type, topic, difficulty, source, status, layer, wing,
                                           chapter, raw_markdown, payload, content_hash, verify_report)
                    VALUES (:id, :type, :topic, :difficulty, :source, 'draft', :layer, :wing,
                            :chapter, :raw, CAST(:payload AS JSONB), :hash, NULL)
                    ON CONFLICT (id) DO UPDATE SET
                      type = EXCLUDED.type, topic = EXCLUDED.topic, difficulty = EXCLUDED.difficulty,
                      source = EXCLUDED.source, layer = EXCLUDED.layer, wing = EXCLUDED.wing,
                      chapter = EXCLUDED.chapter, raw_markdown = EXCLUDED.raw_markdown,
                      payload = EXCLUDED.payload, content_hash = EXCLUDED.content_hash,
                      status = 'draft', updated_at = now()
                    """
                ),
                {
                    "id": question_id,
                    "type": str(_column(payload, "type", "single")),
                    "topic": str(_column(payload, "topic", "tt-arch")),
                    "difficulty": int(_column(payload, "difficulty", 3) or 3),
                    "source": str(_column(payload, "source", "")),
                    "layer": str(_column(payload, "layer", "")),
                    "wing": str(_column(payload, "wing", "")),
                    "chapter": str(_column(payload, "chapter", "")),
                    "raw": _strip_front(raw),
                    # sources（依据）与 verify_ranges（校验窗口）都留在 payload 里：
                    # 补校验任务时要靠它们重建提示词，而库是唯一权威，没有第二个地方可问。
                    # `point` 也留在这里 —— 校验通过时要用它写"题↔点的边"。
                    "payload": json.dumps(
                        {
                            **payload,
                            "sources": item.get("sources") or [],
                            "verify_ranges": item.get("window") or item.get("sources") or [],
                            "point": _resolve_point(item, list(pack_points or [])),
                            # 轮次（0 = 首次出题）：打回重出靠它收敛
                            "round": int(item.get("round") or 0),
                        },
                        ensure_ascii=False,
                    ),
                    "hash": __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest(),
                },
            )
            written.append(
                {
                    "id": question_id,
                    "sources": item.get("sources") or [],
                    "layer": str(_column(payload, "layer", "")),
                    "type": str(_column(payload, "type", "")),
                }
            )
    return written, dropped


def save_slices(subject: str, meta: dict, slices: list[dict], figures: list[dict]) -> dict:
    """切片与图清单**直接入库**（`ingest` 的出口）。

    切片是抽取的作业单位：派工要问"哪些切片还没抽过"，归并要知道"片与片是否同节"。
    这些只有在库里才成立 —— 所以切片生成不再落 `maps/`，直接写表。
    """
    import json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    slug = str(meta["material"])
    with _engine().begin() as conn:
        mid = conn.execute(
            text(
                "INSERT INTO materials (slug, subject, title, source_path, sha256, lines, updated_at)"
                " VALUES (:slug, :subject, :title, :source_path, :sha256, :lines, now())"
                " ON CONFLICT (slug) DO UPDATE SET subject = EXCLUDED.subject,"
                " title = EXCLUDED.title, source_path = EXCLUDED.source_path,"
                " sha256 = EXCLUDED.sha256, lines = EXCLUDED.lines, updated_at = now()"
                " RETURNING id"
            ),
            {
                "slug": slug,
                "subject": str(meta.get("subject") or subject),
                "title": str(meta.get("title") or slug),
                "source_path": str(meta.get("source_path") or ""),
                "sha256": str(meta.get("sha256") or ""),
                "lines": int(meta.get("lines") or 0),
            },
        ).scalar_one()

        conn.execute(text("DELETE FROM material_slices WHERE material_id = :mid"), {"mid": mid})
        for item in slices:
            lines = (item.get("loc") or {}).get("lines") or [0, 0]
            raw_path = item.get("path")
            conn.execute(
                text(
                    "INSERT INTO material_slices (material_id, slice_id, path, start_line, end_line,"
                    " tokens, figures, summary) VALUES (:mid, :sid, CAST(:path AS JSONB), :start,"
                    " :end, :tokens, CAST(:figures AS JSONB), :summary)"
                ),
                {
                    "mid": mid,
                    "sid": str(item.get("id") or ""),
                    "path": json.dumps(
                        [raw_path] if isinstance(raw_path, str) else list(raw_path or []),
                        ensure_ascii=False,
                    ),
                    "start": int(lines[0]) if lines else 0,
                    "end": int(lines[-1]) if lines else 0,
                    "tokens": str(item.get("tokens") or ""),
                    "figures": json.dumps(list(item.get("figures") or []), ensure_ascii=False),
                    "summary": str(item.get("summary") or ""),
                },
            )

        conn.execute(text("DELETE FROM material_figures WHERE material_id = :mid"), {"mid": mid})
        for figure in figures:
            lines = (figure.get("loc") or {}).get("lines") or [0, 0]
            conn.execute(
                text(
                    "INSERT INTO material_figures (material_id, figure_id, src, slice_id, start_line,"
                    " end_line, caption, kind, keys, plan) VALUES (:mid, :fid, :src, :sid, :start,"
                    " :end, :caption, :kind, CAST(:keys AS JSONB), :plan)"
                ),
                {
                    "mid": mid,
                    "fid": str(figure.get("id") or ""),
                    "src": str(figure.get("src") or ""),
                    "sid": str(figure.get("slice") or ""),
                    "start": int(lines[0]) if lines else 0,
                    "end": int(lines[-1]) if lines else 0,
                    "caption": str(figure.get("caption") or ""),
                    "kind": str(figure.get("kind") or ""),
                    "keys": json.dumps(list(figure.get("keys") or []), ensure_ascii=False),
                    "plan": str(figure.get("plan") or ""),
                },
            )
    return {"material": slug, "slices": len(slices), "figures": len(figures)}


def material_id(conn, slug: str) -> int | None:
    from sqlalchemy import text  # noqa: PLC0415

    row = conn.execute(text("SELECT id FROM materials WHERE slug = :slug"), {"slug": slug}).fetchone()
    return int(row[0]) if row else None


def save_extract(material_slug: str, slice_id: str, data: dict) -> int:
    """抽取结果**直接入库**（`point_candidates`），不再落 `extract/*.json`。

    落文件还有一个隐形代价：下游要么读文件、要么读库，两套形状迟早分叉
    —— 实测就分叉过（抽取员给单数 `source`，归并读复数 `sources`，
    结果矩阵里的出处全成了空数组）。
    """
    import json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    points = data.get("points") or []
    with _engine().begin() as conn:
        mid = material_id(conn, material_slug)
        if mid is None:
            raise ValueError(f"材料不在库里：{material_slug}（先跑 pipeline.dbsync）")
        conn.execute(
            text("DELETE FROM point_candidates WHERE material_id = :mid AND slice_id = :sid"),
            {"mid": mid, "sid": slice_id},
        )
        for point in points:
            ckey = str(point.get("key") or "").strip()
            if not ckey:
                continue
            sources = point.get("sources")
            if not sources and point.get("source"):
                sources = [point["source"]]
            conn.execute(
                text(
                    "INSERT INTO point_candidates (material_id, slice_id, key, name, kind, thickness,"
                    " layers, sources, terms, raw, consumed)"
                    " VALUES (:mid, :sid, :key, :name, :kind, :thickness, CAST(:layers AS JSONB),"
                    " CAST(:sources AS JSONB), CAST(:terms AS JSONB), :raw, false)"
                    # 同一片里同一个 key 出现多次是正常的（模型会从不同角度说同一件事），
                    # 候选表按 (材料, 切片, key) 唯一 —— 保留第一条，不因此让整批回滚
                    " ON CONFLICT (material_id, slice_id, key) DO NOTHING"
                ),
                {
                    "mid": mid,
                    "sid": slice_id,
                    "key": ckey,
                    "name": str(point.get("name") or ""),
                    "kind": str(point.get("kind") or "noun"),
                    "thickness": int(point.get("thickness") or 1),
                    "layers": json.dumps(list(point.get("layers") or []), ensure_ascii=False),
                    "sources": json.dumps(sources or [], ensure_ascii=False),
                    "terms": json.dumps(list(point.get("terms") or []), ensure_ascii=False),
                    "raw": json.dumps(point, ensure_ascii=False),
                },
            )
    return len(points)


def save_vision(material_slug: str, figure_id: str, data: dict) -> int:
    """读图结果**直接入库**：每条 fact 变成一条候选点（`origin=figure`）。

    图给的是空间关系（谁连谁、箭头朝哪），往往是正文没写全的 —— 所以图本身就是
    知识点。它与文字候选走同一条归并路径，于是"图支持的点"和"文字支持的点"
    会被合并成同一个点，而不是各记一份。
    """
    import json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    result = data.get("result") or {}
    facts = result.get("facts") or []
    lines = (data.get("loc") or {}).get("lines") or [0, 0]
    with _engine().begin() as conn:
        mid = material_id(conn, material_slug)
        if mid is None:
            raise ValueError(f"材料不在库里：{material_slug}（先跑 pipeline.dbsync）")
        conn.execute(
            text("DELETE FROM point_candidates WHERE material_id = :mid AND slice_id = :sid"),
            {"mid": mid, "sid": figure_id},
        )
        for fact in facts:
            fkey = str(fact.get("key") or "").strip()
            if not fkey:
                continue
            conn.execute(
                text(
                    "INSERT INTO point_candidates (material_id, slice_id, key, name, kind, thickness,"
                    " layers, sources, terms, raw, consumed)"
                    " VALUES (:mid, :sid, :key, :name, :kind, 2, CAST(:layers AS JSONB),"
                    " CAST(:sources AS JSONB), CAST(:terms AS JSONB), :raw, false)"
                    # 一张图可能对同一个 key 给出多条事实（同一件事的不同说法），保留第一条
                    " ON CONFLICT (material_id, slice_id, key) DO NOTHING"
                ),
                {
                    "mid": mid,
                    "sid": figure_id,
                    "key": fkey,
                    "name": str(fact.get("statement") or fkey),
                    "kind": str(fact.get("kind") or "figure"),
                    "layers": json.dumps(["识记", "理解"], ensure_ascii=False),
                    "sources": json.dumps([lines], ensure_ascii=False),
                    "terms": json.dumps([], ensure_ascii=False),
                    "raw": json.dumps({"origin": "figure", **fact}, ensure_ascii=False),
                },
            )
    return len(facts)


def load_question(question_id: str) -> dict | None:
    from sqlalchemy import text  # noqa: PLC0415

    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT id, status, raw_markdown, payload, topic, type FROM questions WHERE id = :id"),
            {"id": question_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def save_verdict(question_id: str, verdict: str, report: dict) -> None:
    """写入校验结果：通过 → `verified`；不通过 → 留在 `draft` 并记下理由。"""
    import json  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    status = "verified" if verdict == "pass" else "draft"
    with _engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE questions SET status = :status, verify_report = CAST(:report AS JSONB),"
                " updated_at = now() WHERE id = :id"
            ),
            {"status": status, "report": json.dumps(report, ensure_ascii=False), "id": question_id},
        )
        if verdict == "pass":
            # **精确的"题↔点的边"只在校验通过时写**。
            # 草稿/被打回的题不该把点标成"已出题" —— 否则一道废题会让那个点
            # 从此不再出题，这比重复出题更糟。
            # 点的 key 在**题目的 payload** 里（出题入库时写下的），不在校验报告里。
            point_key = (
                conn.execute(
                    text("SELECT payload -> 'point' FROM questions WHERE id = :id"),
                    {"id": question_id},
                ).scalar()
                or ""
            )
            if point_key:
                conn.execute(
                    text(
                        "INSERT INTO question_points (question_id, point_id, is_primary)"
                        " SELECT :qid, kp.id, false FROM knowledge_points kp WHERE kp.key = :key"
                        " ON CONFLICT DO NOTHING"
                    ),
                    {"qid": question_id, "key": str(point_key)},
                )


def draft_ids(limit: int = 0) -> list[str]:
    """还没通过校验的题（补校验任务时用）。"""
    from sqlalchemy import text  # noqa: PLC0415

    # 已退役的草稿不该再排校验（退役的语义就是"别再管它了"）
    sql = "SELECT id FROM questions WHERE status = 'draft' AND retired_at IS NULL ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _engine().connect() as conn:
        return [row[0] for row in conn.execute(text(sql))]
