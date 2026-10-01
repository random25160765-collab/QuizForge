"""题库的**单一交换文件**（一进一出），取代"一题一个小文件"。

为什么要有它：一题一个 `.md` 在几十万题的规模下既无法管理，也没法和 LLM 对齐。
现在**数据库是唯一权威**，文件只在两种场合出现：

* **对外部署**：一台机器导出，另一台导入（不用带着十万个小文件搬家）
* **审阅 / 备份**：同一条命令，产物是**一个文件**，可直接丢进版本控制或对象存储

格式：一个 JSON —— ``{"version", "exportedAt", "topics": [...], "questions": [...]}``。
每道题同时带 ``raw_markdown``（原文）与 ``payload``（解析结果）：
带原文才能在契约改动后重新解析，带解析结果才能导入后不必再解析一遍。

用法：
    api/.venv/bin/python -m pipeline.bankfile export            # → 默认 bank.json
    api/.venv/bin/python -m pipeline.bankfile export --out /tmp/b.json --status all
    api/.venv/bin/python -m pipeline.bankfile import --path /tmp/b.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import bindparam, select, text

from .dbstore import as_json

from . import config

sys.path.insert(0, str(config.ROOT / "api"))

DEFAULT_OUT = config.ROOT / "bank.json"
VERSION = 1


def export_bundle(out: Path, statuses: tuple[str, ...] | None = None) -> dict:
    """库 → 一个文件。默认只导"已发布"的题，`--status all` 时连草稿一起导。"""
    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        topics = [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    # desc 是 SQL 保留字，原生 SQL 里必须加引号（ORM 会自动引，这里是手写的）
                    'SELECT key, name, group_key, order_index, color, "desc", depth, parent_key,'
                    " path, path_names, children, descendants, is_leaf, retired_at FROM topics"
                    " ORDER BY depth, order_index, key"
                )
            )
        ]
        sql = (
            "SELECT id, type, topic, difficulty, source, status, layer, wing, chapter,"
            " raw_markdown, payload, content_hash FROM questions"
        )
        # `IN :st` + `expanding=True`：SQLAlchemy 会按列表长度展开成 `IN (?, ?, …)`。
        # 原先写的是 Postgres 的 `= ANY(:st)`，SQLite 上没有 `ANY` 这个函数（换库时实测炸）。
        stmt = text(sql + (" WHERE status IN :st" if statuses else "") + " ORDER BY id")
        if statuses:
            stmt = stmt.bindparams(bindparam("st", expanding=True))
        params = {"st": list(statuses)} if statuses else {}
        questions = [dict(row._mapping) for row in conn.execute(stmt, params)]
        # 裸 SQL 不会替我们解码 JSON 列（SQLite 给字符串）—— 见 `as_json`
        for question in questions:
            question["payload"] = as_json(question.get("payload"), {})
        documents = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT key, content FROM meta_documents"))
        }
        # 这一次装的是哪一份题库（应用自己认的那个指纹）。
        # **用户数据要拿它对账**：用户的进度是按题目 id 记的，换了一份题库就对不上号，
        # 所以用户包会把它抄下来（`api/app/datapack.py` 的 `_bank_fingerprint`），
        # 而这儿的包里必须带着 —— 否则新机器导完题库也不知道自己是哪一份。
        version = conn.execute(
            text(
                "SELECT content_hash, question_count, topic_count FROM bank_versions"
                " WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()

    for question in questions:
        question["retired_at"] = None
    bundle = {
        "version": VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contentHash": (version[0] if version else "") or "",
        "topics": topics,
        "documents": documents,
        "questions": questions,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    # default=str：主题行里带着 datetime（retired_at 之类），直接 dumps 会炸
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return {"topics": len(topics), "questions": len(questions), "bytes": out.stat().st_size}


#: 考纲树一行（列名与 `export_bundle` 的 SELECT 一一对应）。
#: `desc` 是 SQL 保留字，裸 SQL 里必须加引号 —— 与导出那边同一个坑。
_TOPIC_UPSERT = (
    'INSERT INTO topics (key, name, group_key, order_index, color, "desc", depth, parent_key,'
    " path, path_names, children, descendants, is_leaf, retired_at)"
    " VALUES (:key, :name, :group_key, :order_index, :color, :desc, :depth, :parent_key,"
    " :path, :path_names, :children, :descendants, :is_leaf, :retired_at)"
    ' ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, group_key = EXCLUDED.group_key,'
    " order_index = EXCLUDED.order_index, color = EXCLUDED.color,"
    ' "desc" = EXCLUDED."desc", depth = EXCLUDED.depth, parent_key = EXCLUDED.parent_key,'
    " path = EXCLUDED.path, path_names = EXCLUDED.path_names,"
    " children = EXCLUDED.children, descendants = EXCLUDED.descendants,"
    " is_leaf = EXCLUDED.is_leaf, retired_at = EXCLUDED.retired_at,"
    " updated_at = CURRENT_TIMESTAMP"
)


def _mark_version(conn, bundle: dict, *, questions: int, topics: int) -> str:
    """把「当前装的是哪一份题库」写进 `bank_versions`，返回用的指纹。

    **从前这里一行都不写**：导出有指纹、导入不落地，于是导入一份题库包之后，
    应用自己还认为装的是上一次那一份。实测：库里躺着 1623 道题，而 `is_current`
    那行还写着 9-16 号那次的 109 题 —— 用户数据包靠这个指纹跟题库对账
    （`api/app/datapack.py` 的 `_bank_fingerprint`），账是旧的那条路就是错的。

    指纹优先用包里带的（**导出端算的才算数** —— 那才是"同一份内容"的判据）；
    老包没带就按包内容现算一个（确定性：同一份包在哪台机器都得到同一个值）。
    """
    content_hash = str(bundle.get("contentHash") or "")
    if not content_hash:
        blob = json.dumps(
            {
                "questions": sorted(str(q.get("id")) for q in bundle.get("questions") or []),
                "topics": sorted(str(t.get("key")) for t in bundle.get("topics") or []),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    conn.execute(text("UPDATE bank_versions SET is_current = 0 WHERE is_current = 1"))
    conn.execute(
        text(
            "INSERT INTO bank_versions (content_hash, question_count, topic_count, stats, is_current)"
            " VALUES (:h, :q, :t, :s, 1)"
            " ON CONFLICT (content_hash) DO UPDATE SET is_current = 1,"
            " question_count = EXCLUDED.question_count, topic_count = EXCLUDED.topic_count"
        ),
        {
            "h": content_hash,
            "q": questions,
            "t": topics,
            "s": json.dumps(bundle.get("stats") or {}, ensure_ascii=False),
        },
    )
    return content_hash


def import_bundle(path: Path) -> dict:
    """一个文件 → 库。按 id upsert（题库有更新时重导即可，不会重复）。"""
    from app.db import get_engine  # noqa: PLC0415

    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("version") != VERSION:
        raise SystemExit(f"交换文件版本不认识：{bundle.get('version')}（期望 {VERSION}）")

    added = updated = 0
    with get_engine().begin() as conn:
        # ① 考纲树。**这里从前一行都不写** —— 导出写了 `topics`，导入却只写题目与
        #    `meta_documents`，返回码还照样报"topics: N"，看上去一切正常。
        #    实际后果：导入一份题库包会**静默丢掉整棵考纲树**（界面上的层级与分组全空）。
        topics = bundle.get("topics") or []
        for topic in topics:
            conn.execute(text(_TOPIC_UPSERT), topic)

        known = {row[0] for row in conn.execute(text("SELECT id FROM questions"))}
        for question in bundle.get("questions") or []:
            exists = question["id"] in known
            conn.execute(
                text(
                    """
                    INSERT INTO questions (id, type, topic, difficulty, source, status, layer, wing,
                                           chapter, raw_markdown, payload, content_hash)
                    VALUES (:id, :type, :topic, :difficulty, :source, :status, :layer, :wing,
                            :chapter, :raw_markdown, :payload, :content_hash)
                    ON CONFLICT (id) DO UPDATE SET
                      type = EXCLUDED.type, topic = EXCLUDED.topic, difficulty = EXCLUDED.difficulty,
                      source = EXCLUDED.source, layer = EXCLUDED.layer, wing = EXCLUDED.wing,
                      chapter = EXCLUDED.chapter, raw_markdown = EXCLUDED.raw_markdown,
                      payload = EXCLUDED.payload, content_hash = EXCLUDED.content_hash,
                      updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {**question, "payload": json.dumps(question["payload"], ensure_ascii=False)},
            )
            added += 0 if exists else 1
            updated += 1 if exists else 0
        for key, content in (bundle.get("documents") or {}).items():
            conn.execute(
                text(
                    "INSERT INTO meta_documents (key, content) VALUES (:k, :c) "
                    "ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content,"
                    " updated_at = CURRENT_TIMESTAMP"
                ),
                {"k": key, "c": content},
            )

        # ② 版本行 —— 不写它，应用就不知道自己刚装的是哪一份（见 `_mark_version`）。
        content_hash = _mark_version(
            conn,
            bundle,
            questions=len(bundle.get("questions") or []),
            topics=len(topics),
        )
    return {
        "added": added,
        "updated": updated,
        "topics": len(topics),
        "contentHash": content_hash,
    }


def strip_front_matter(markdown: str | None) -> str:
    """剥掉正文开头的元信息头（`---` 包住的那一段）。

    为什么要有它：库里存过带头的正文（导入时代留下的），而物化时又会在前面拼一个头，
    结果就是**双头文件** —— 页面把第二个头当正文渲染出来（实测题目上多出一块
    `id: ... type: ...` 的文字）。这里做防御：有头就剥，没头就原样返回。
    """
    text = (markdown or "").strip()
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4 :].lstrip("\n")


def _scalar(value) -> str:
    """front-matter 标量：数字裸写，其余加引号（解析器两种都收）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", str(text or "")).strip("-").lower()
    return slug[:limit] or "question"


def materialize(out: Path, statuses: tuple[str, ...] = ("published",)) -> dict:
    """库 → **临时目录**（`questions/<学科>/*.md` + `meta/topics.yaml`）。

    给构建与校验工具用：它们几十年来读的是"一题一个文件"的目录，改造它们风险大，
    而它们要的只是"一批能读的题"。所以权威留在库里，目录**按需生成、用完即弃** ——
    默认写到 /tmp，不进版本控制，也就不存在"库里改了、文件没改"的漂移。
    """
    from app.db import get_engine  # noqa: PLC0415

    written = 0
    with get_engine().connect() as conn:
        rows = [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    "SELECT id, type, topic, difficulty, source, layer, wing, chapter,"
                    " raw_markdown, payload FROM questions"
                    " WHERE status IN :st ORDER BY id"
                ).bindparams(bindparam("st", expanding=True)),
                {"st": list(statuses)},
            )
        ]

    for question in rows:
        subject = question["id"].rsplit("-", 1)[0] or "tt-arch"
        front: list[tuple[str, object]] = [
            ("id", question["id"]),
            ("type", question["type"]),
            ("topic", question["topic"]),
            ("difficulty", question["difficulty"]),
        ]
        # 裸 SQL 读到的是字符串（SQLite）—— 见 `as_json`
        payload = as_json(question.get("payload"), {}) or {}
        # 列里可能为空（历史导入没填这几列），payload 里一定有 —— 两边都看，避免物化后
        # 反而比原文件少了字段（实测少了 chapter 就多出上百条告警）。
        for key in ("layer", "wing", "chapter"):
            value = question.get(key) or payload.get(key)
            if value:
                front.append((key, value))
        if question.get("source"):
            front.append(("source", question["source"]))
        tags = payload.get("tags") or []
        if tags:
            front.append(("tags", ", ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)))
        head = "---\n" + "\n".join(f"{k}: {_scalar(v)}" for k, v in front) + "\n---\n\n"
        directory = out / "questions" / subject
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{question['id']}-{slugify(question['topic'])}.md").write_text(
            head + strip_front_matter(question["raw_markdown"]) + "\n", encoding="utf-8"
        )
        written += 1

    # 考纲由**树**渲染（库里为正）；`meta_documents` 里那份只作历史快照，不再参与
    from app.outline import render_yaml  # noqa: PLC0415

    (out / "meta").mkdir(parents=True, exist_ok=True)
    (out / "meta" / "topics.yaml").write_text(render_yaml(), encoding="utf-8")

    # 知识图谱快照：页面与导出都读它。拼图逻辑只有一份（`app.graph`），
    # 所以"页面上的图"和"导出的图"不可能不一样。
    from app.graph import payload as graph_payload  # noqa: PLC0415

    (out / "meta" / "graph.json").write_text(
        json.dumps(graph_payload(), ensure_ascii=False), encoding="utf-8"
    )
    return {"questions": written, "topics": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.bankfile", description="题库单一交换文件")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export", help="库 → 一个文件")
    exp.add_argument("--out", default=str(DEFAULT_OUT))
    exp.add_argument("--status", default="published", help="published（默认）/ all")

    imp = sub.add_parser("import", help="一个文件 → 库")
    imp.add_argument("--path", required=True)

    mat = sub.add_parser("materialize", help="库 → 临时目录（供构建/校验工具读）")
    mat.add_argument("--out", required=True)
    mat.add_argument("--status", default="published", help="published（默认）/ all")

    args = parser.parse_args(argv)
    if args.cmd == "materialize":
        statuses = None if args.status == "all" else tuple(x.strip() for x in args.status.split(",") if x.strip())
        stats = materialize(Path(args.out), statuses or ("published",))
        print(f"物化：{stats['questions']} 道题 → {args.out}（考纲 {'已' if stats['topics'] else '未'}写出）")
        return 0
    if args.cmd == "export":
        statuses = None if args.status == "all" else tuple(x.strip() for x in args.status.split(",") if x.strip())
        stats = export_bundle(Path(args.out), statuses)
        print(f"导出：{stats['questions']} 道题 · {stats['topics']} 个主题 → {args.out}（{stats['bytes'] / 1024:.0f} KB）")
        return 0
    stats = import_bundle(Path(args.path))
    print(f"导入：新增 {stats['added']} · 更新 {stats['updated']} · 主题 {stats['topics']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
