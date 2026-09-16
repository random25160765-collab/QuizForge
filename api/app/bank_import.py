"""题库导入：``questions/*.md`` + ``meta/topics.yaml`` → PostgreSQL。

设计要点：

* **复用构建期那一套**：解析、校验、数据集装配都调 `tools/` 下的函数，
  所以导入进库的题目与离线产物逐字节一致（`tools/dataset.build_dataset`）。
* **幂等**：按 `id` upsert，逐题比对内容哈希，没变的题连 UPDATE 都不发。
* **退役而不是删除**：源里没有了的题 / 主题只置 `retired_at`。
  用户的做题记录挂在题目 id 上，物理删除会让历史进度出现空洞，
  掌握度与错题本都会莫名其妙地少东西。
* **可预演**：`dry_run=True` 只算不写，用于导入前确认影响面。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .config import Settings, get_settings
from .models import BankVersion, Question, Topic
from .toolkit import (
    Diagnostic,
    check_file,
    iter_question_files,
    parse_question,
    question_to_dict,
)
from .toolkit import TYPE_LABELS  # noqa: F401  （导出给调用方复用）
from .toolkit import load_topic_tree
from dataset import build_dataset


def _digest(payload: object) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_digest(question: dict) -> str:
    """单题内容指纹。题目文件一改（含改名换目录），这里就会变。"""
    return _digest(question)


# --------------------------------------------------------------------- 扫描


@dataclass
class ScanResult:
    """一次源码扫描的结果。不碰数据库，因此可以随意预演。"""

    dataset: dict
    diagnostics: list[Diagnostic]
    file_count: int
    content_hash: str
    topic_nodes: dict[str, dict]

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "ERROR"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "WARN"]

    @property
    def questions(self) -> list[dict]:
        return self.dataset["questions"]


def scan(settings: Settings | None = None) -> ScanResult:
    """解析题目目录与考纲，产出与 `dist/data.json` 同构的数据集。

    只做解析与校验，不连数据库 —— 这样 `--dry-run`、单元测试与真实导入
    走的是同一条代码路径，不会出现「预演说没事、真导入却炸」。
    """
    settings = settings or get_settings()
    questions_dir = Path(settings.questions_dir)

    nodes, ordered, groups, topic_problems = load_topic_tree()
    diagnostics: list[Diagnostic] = []

    for problem in topic_problems:
        diagnostics.append(Diagnostic("ERROR", Path(settings.topics_file), 0, problem))

    files = iter_question_files(questions_dir) if questions_dir.is_dir() else []
    parsed: list[dict] = []

    for path in files:
        try:
            question = parse_question(path)
        except Exception as exc:  # QuestionParseError 携带文件与行号
            line = getattr(exc, "line", 0)
            diagnostics.append(Diagnostic("ERROR", path, line, str(exc).split(": ", 1)[-1]))
            continue
        # 与构建期完全同一套校验（主题存在性、小节契约、LaTeX 定界符……）
        check_file(path, nodes, diagnostics)
        parsed.append(question_to_dict(question))

    dataset = build_dataset(parsed, nodes, ordered, groups)

    # 版本指纹只看「内容」：题目指纹 + 主题树结构。
    # 不含 generatedAt，否则每次导入都会产生新版本，ETag 也就失去意义了。
    content_hash = _digest(
        {
            "topics": [f"{key}:{nodes[key]['pathNames'][-1]}:{nodes[key]['parent'] or ''}" for key in ordered],
            "questions": {q["id"]: question_digest(q) for q in dataset["questions"]},
        }
    )

    return ScanResult(
        dataset=dataset,
        diagnostics=diagnostics,
        file_count=len(files),
        content_hash=content_hash,
        topic_nodes=nodes,
    )


# --------------------------------------------------------------------- 导入


@dataclass
class ImportReport:
    content_hash: str = ""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    topics_added: list[str] = field(default_factory=list)
    topics_updated: list[str] = field(default_factory=list)
    topics_retired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.updated) + len(self.retired) + len(self.restored)

    def render(self) -> str:
        head = "导入预演（未写库）" if self.dry_run else "导入完成"
        lines = [f"{head} · 版本 {self.content_hash[:12]}"]
        lines.append(
            f"  题目：新增 {len(self.added)} · 更新 {len(self.updated)} · "
            f"未变 {len(self.unchanged)} · 退役 {len(self.retired)} · 恢复 {len(self.restored)}"
        )
        lines.append(
            f"  主题：新增 {len(self.topics_added)} · 更新 {len(self.topics_updated)} · "
            f"退役 {len(self.topics_retired)}"
        )
        for label, items in (("新增", self.added), ("更新", self.updated), ("退役", self.retired)):
            if items:
                shown = "、".join(items[:8]) + ("…" if len(items) > 8 else "")
                lines.append(f"    {label}: {shown}")
        for err in self.errors[:10]:
            lines.append(f"  [错误] {err}")
        return "\n".join(lines)


def apply(
    session: OrmSession,
    result: ScanResult,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ImportReport:
    """把扫描结果写进数据库。

    分两步：**先算增量，再落库**。这样 `dry_run=True` 是真的什么都不写 ——
    只算不写的预演才有意义，否则调用方一旦忘了回滚就会留下半成品。
    （早先的实现即使在预演模式下也在改 ORM 对象，只是没插版本行。）

    `force=False` 时，只要校验有 ERROR 就拒绝导入 —— 与 `make build` 的行为一致，
    免得半成品题目进库污染统计。库里已有的旧数据也不会被清空，这是刻意的：
    修好题目再导一次即可。
    """
    report = ImportReport(content_hash=result.content_hash, dry_run=dry_run)

    if result.errors and not force:
        report.errors = [d.render() for d in result.errors]
        report.errors.append("存在校验错误，已拒绝导入（用 --force 可强行导入）")
        return report

    topics = _plan_topics(session, result.dataset["meta"]["topics"])
    questions = _plan_questions(session, result.questions)

    report.added = questions.added
    report.updated = questions.updated
    report.unchanged = questions.unchanged
    report.retired = questions.retired
    report.restored = questions.restored
    report.topics_added = topics.added
    report.topics_updated = topics.updated
    report.topics_retired = topics.retired

    if dry_run:
        return report

    _write_topics(session, topics)
    _write_questions(session, questions)
    _mark_current_version(session, result, topic_count=len(topics.incoming))
    return report


# ------------------------------------------------------------------ 增量计划


@dataclass
class _TopicPlan:
    incoming: dict[str, dict] = field(default_factory=dict)
    rows: dict[str, "Topic"] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)


@dataclass
class _QuestionPlan:
    incoming: dict[str, dict] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    rows: dict[str, "Question"] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def _topic_payload(node: dict) -> dict:
    return {
        "name": node["name"],
        "group_key": node["group"],
        "order_index": node["order"],
        "color": node["color"],
        "desc": node["desc"],
        "depth": node["depth"],
        "parent_key": node["parent"],
        "path": node["path"],
        "path_names": node["pathNames"],
        "children": node["children"],
        "descendants": node["descendants"],
        "is_leaf": node["leaf"],
    }


def _plan_topics(session: OrmSession, nodes: list[dict]) -> _TopicPlan:
    plan = _TopicPlan(rows={row.key: row for row in session.scalars(select(Topic)).all()})

    for node in nodes:
        key = node["key"]
        plan.incoming[key] = _topic_payload(node)
        row = plan.rows.get(key)
        if row is None:
            plan.added.append(key)
            continue
        changed = any(getattr(row, name) != value for name, value in plan.incoming[key].items())
        # 退役过的主题又回来了：即使内容没变也要解除退役
        if row.retired_at is not None or changed:
            plan.updated.append(key)

    for key, row in plan.rows.items():
        if key not in plan.incoming and row.retired_at is None:
            plan.retired.append(key)

    return plan


def _plan_questions(session: OrmSession, questions: list[dict]) -> _QuestionPlan:
    plan = _QuestionPlan(rows={row.id: row for row in session.scalars(select(Question)).all()})

    for item in questions:
        qid = item["id"]
        digest = question_digest(item)
        plan.incoming[qid] = item
        plan.digests[qid] = digest

        row = plan.rows.get(qid)
        if row is None:
            plan.added.append(qid)
            continue
        if row.content_hash == digest and row.retired_at is None:
            plan.unchanged.append(qid)
            continue
        if row.retired_at is not None:
            plan.restored.append(qid)
        else:
            plan.updated.append(qid)

    for qid, row in plan.rows.items():
        if qid not in plan.incoming and row.retired_at is None:
            plan.retired.append(qid)

    return plan


def _write_topics(session: OrmSession, plan: _TopicPlan) -> None:
    for key in plan.added:
        session.add(Topic(key=key, **plan.incoming[key]))
    for key in plan.updated:
        row = plan.rows[key]
        for name, value in plan.incoming[key].items():
            setattr(row, name, value)
        row.retired_at = None
    for key in plan.retired:
        plan.rows[key].retired_at = _now()
    session.flush()


def _write_questions(session: OrmSession, plan: _QuestionPlan) -> None:
    for qid in plan.added:
        item = plan.incoming[qid]
        session.add(
            Question(
                id=qid,
                type=item["type"],
                topic=item["topic"],
                difficulty=item["difficulty"],
                source=item["source"],
                payload=item,
                content_hash=plan.digests[qid],
            )
        )
    for qid in plan.updated + plan.restored:
        item = plan.incoming[qid]
        row = plan.rows[qid]
        row.type = item["type"]
        row.topic = item["topic"]
        row.difficulty = item["difficulty"]
        row.source = item["source"]
        row.payload = item
        row.content_hash = plan.digests[qid]
        row.retired_at = None
    for qid in plan.retired:
        plan.rows[qid].retired_at = _now()
    session.flush()


def _mark_current_version(session: OrmSession, result: ScanResult, *, topic_count: int) -> None:
    """把当前版本指向本次内容。

    **版本由内容标识，不是每次导入都新增一行。**
    否则原样再导一次就会插入相同 content_hash 的第二行，撞上唯一约束直接报错；
    而且「同一份内容有多个版本」本身就没有意义。
    这里同时保证 `imported_at` 与 content_hash 一一对应 ——
    ETag 也是内容哈希，304 命中时生成的响应体必须与首次一致，
    时间戳跟着每次导入抖动就会自相矛盾。
    """
    existing = session.scalars(
        select(BankVersion).where(BankVersion.content_hash == result.content_hash)
    ).first()

    for row in session.scalars(select(BankVersion).where(BankVersion.is_current.is_(True))).all():
        if existing is None or row.id != existing.id:
            row.is_current = False

    if existing is not None:
        existing.is_current = True
        session.flush()
        return

    session.add(
        BankVersion(
            content_hash=result.content_hash,
            question_count=len(result.questions),
            topic_count=topic_count,
            stats=result.dataset["meta"]["stats"],
            is_current=True,
        )
    )
    session.flush()


def _live_stats(session: OrmSession, topics: list[dict]) -> dict:
    """按实时数据算统计：**题数沿 path 累加到每级祖先**（学科 = 其下所有知识点之和）。

    与前端 `data.js` 的本地算法同一口径；两边一致，界面才不会出现"子项加起来对不上"。
    """
    from sqlalchemy import func  # noqa: PLC0415

    from .models import Question as _Question  # noqa: PLC0415

    path_of = {node["key"]: (node.get("path") or [node["key"]]) for node in topics}
    by_topic: dict[str, int] = {key: 0 for key in path_of}
    by_type: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    rows = session.execute(
        select(_Question.topic, _Question.type, _Question.difficulty, func.count())
        .where(
            _Question.retired_at.is_(None),
            _Question.status.in_(("verified", "published")),
        )
        .group_by(_Question.topic, _Question.type, _Question.difficulty)
    ).all()
    for topic_key, question_type, difficulty, count in rows:
        for ancestor in path_of.get(topic_key) or [topic_key]:
            by_topic[ancestor] = by_topic.get(ancestor, 0) + count
        by_type[question_type] = by_type.get(question_type, 0) + count
        by_difficulty[difficulty] = by_difficulty.get(difficulty, 0) + count
    return {
        "total": sum(row[3] for row in rows),
        "byTopic": by_topic,
        "byType": by_type,
        "byDifficulty": by_difficulty,
    }


def _now():  # noqa: ANN202
    from datetime import UTC, datetime

    return datetime.now(UTC)


def current_bank(session: OrmSession) -> dict:
    """组装 `GET /api/bank` 的响应体。

    结构必须与 `dist/data.json` 一致：前端 `data.install()` 直接吃它，
    多一个字段少一个字段都会让某块界面静静地空掉。
    """
    topics = _depth_first(
        [
            {
                "key": row.key,
                "name": row.name,
                "group": row.group_key,
                "order": row.order_index,
                "color": row.color,
                "desc": row.desc,
                "depth": row.depth,
                "parent": row.parent_key,
                "path": row.path,
                "pathNames": row.path_names,
                "children": row.children,
                "descendants": row.descendants,
                "leaf": row.is_leaf,
            }
            for row in session.scalars(select(Topic).where(Topic.retired_at.is_(None))).all()
        ]
    )

    groups: dict[str, dict] = {}
    for node in topics:
        if node["depth"] != 1:
            continue
        group = node["group"] or ""
        groups.setdefault(group, {"key": group, "name": group, "order": 999, "topics": []})["topics"].append(
            node["key"]
        )
    _decorate_group_names(groups)

    # 题目顺序必须与离线构建一致：先按主题树的位置（父在子前，同级按 order），
    # 再按 id。前端的「从头开始」「再显示 20 题」都直接吃这个数组顺序，
    # 顺序变了用户看到的题库排列就跟离线版不一样。
    order_index = {node["key"]: i for i, node in enumerate(topics)}
    payloads = [
        row.payload
        # 只放行**过了校验**的题：草稿是流水线的中间态，绝不能进学生的界面
        # （实测踩过：接口把 draft 也发了出去 —— 未校验的答案会直接被当成标准答案）
        for row in session.scalars(
            select(Question).where(
                Question.retired_at.is_(None),
                Question.status.in_(("verified", "published")),
            )
        ).all()
    ]
    questions = sorted(
        payloads,
        key=lambda q: (order_index.get(q.get("topic", ""), 9999), q.get("id", "")),
    )

    version = session.scalars(
        select(BankVersion).where(BankVersion.is_current.is_(True)).order_by(BankVersion.id.desc())
    ).first()

    # 统计必须**按实时数据**算。
    # `BankVersion.stats` 是"导入那一刻"的快照 —— 导完之后流水线又发布了几百道，
    # 界面按它显示就会永远停在旧数字（实测：Tenstorrent 显示 92，而当时实际 1017 道，
    # 且三个子主题加起来还凑不出学科数）。前端拿到接口给的 stats 就不会用自己那套
    # 正确的累加算法（`data.js` 里 `provided.byTopic || local.byTopic`），所以这里必须算对。
    stats = _live_stats(session, topics)
    return {
        "meta": {
            "generator": "quizforge",
            "version": 2,
            "generatedAt": version.imported_at.isoformat(timespec="seconds") if version else "",
            "topics": topics,
            "groups": sorted(groups.values(), key=lambda g: g["order"]),
            "typeLabels": TYPE_LABELS,
            "stats": stats,
        },
        "questions": questions,
    }


def _depth_first(topics: list[dict]) -> list[dict]:
    """按「父在子前、同级按 order」重建 DFS 顺序。

    不要在 SQL 里按 order_index 全局排序：order 只在**同一级之内**有意义
    （主题是 110/120…，单元是 10/20…），全局排序会把单元排到主题前面，
    再从这个错误的顺序里挑 depth==1 就会把主题顺序彻底打乱 ——
    而前端是按数组顺序渲染筛选器的。
    """
    children: dict[str | None, list[dict]] = {}
    for node in topics:
        children.setdefault(node.get("parent"), []).append(node)
    for bucket in children.values():
        bucket.sort(key=lambda n: (n.get("order", 999), n.get("key", "")))

    ordered: list[dict] = []

    def emit(key: str) -> None:
        node = by_key.get(key)
        if node is None:
            return
        ordered.append(node)
        for child in children.get(key, []):
            emit(child["key"])

    by_key = {node["key"]: node for node in topics}
    for node in children.get(None, []):
        emit(node["key"])

    # 兜底：父节点已退役但子节点还在的孤儿，也得出现在结果里
    emitted = {n["key"] for n in ordered}
    ordered.extend(node for node in topics if node["key"] not in emitted)
    return ordered


def _decorate_group_names(groups: dict[str, dict]) -> None:
    """补上分组的中文名与排序权重。

    权威在库（`topic_groups`）；库里还没有时，从 `meta/topics.yaml` 读一次
    **并顺手写回库** —— 于是那个文件可以删掉，首次导入之后这件事与文件无关。
    读不到就退化为显示原始 key：界面难看一点，但不会坏。
    """
    from . import outline  # noqa: PLC0415

    definitions: list[dict] = []
    try:
        definitions = [
            {"key": row["key"], "name": row["name"], "order": row["order_index"]}
            for row in outline.groups()
        ]
    except Exception:  # noqa: BLE001 —— 库抖了一下不该让题库接口失败
        definitions = []
    if not definitions:
        try:
            _nodes, _ordered, definitions, _problems = load_topic_tree()
        except Exception:  # YAML 缺失或损坏都不该让题库接口失败
            return
        try:
            outline.upsert_groups(definitions)
        except Exception:  # noqa: BLE001
            pass
    for definition in definitions:
        group = groups.get(definition["key"])
        if group is not None:
            group["name"] = definition["name"]
            group["order"] = definition["order"]
