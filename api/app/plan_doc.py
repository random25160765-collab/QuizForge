"""计划书：每份任务计划配套的那份 Markdown（与 coding agent 的 plan 文件同一种东西）。

## 它是什么 / 不是什么

* **是**：一份可以自由书写的计划文档 —— 目标、拆解思路、进展、做过的决定。
  建计划时从标题 / 目标 / 条目播下第一版，之后 AI 用 `read_plan_doc` /
  `update_plan_doc` 当工作文档维护，人可以在日程页打开看、也能直接编辑。
* **不是**条目清单的镜像：条目（`plan_items`）才是结构化的真相，排程、勾选、
  改期都以它为准。文档里只有**标记出来**的那一段活清单（`ITEMS_START` /
  `ITEMS_END` 之间）会跟着条目自动刷新；标记之外的正文系统一个字都不碰。

这条分界是刻意的：文档要是也承担"勾选"的职责，就会有两份互相冲突的真相，
而两边都不会知道自己错了。现在文档负责"为什么这么做、做到哪了"，条目负责
"具体有哪几件事、哪件做完了"。

## 为什么清单是"活"的

条目会在日程页被勾选 / 改期 / 改名，而文档是打开就能看的那份计划书。
清单停在建立那一天，打开就是一份假的进展。所以每次条目变化都调一次
`refresh()`，只替换标记之间的内容。AI 若把整份文档重写掉了（连标记一起），
自动同步就停止 —— 那是它主动接管了文档，不是故障。
"""

from __future__ import annotations

from sqlalchemy import select

from .models import Plan, PlanItem

#: 计划文档的字数上限。计划书是「思路 + 进展」，不是资料库。
DOC_MAX = 100_000

#: 活清单的标记（对 Markdown 渲染不可见）。
ITEMS_START = "<!-- qf:items:start -->"
ITEMS_END = "<!-- qf:items:end -->"


def item_line(item: PlanItem | dict) -> str:
    """一条待办的一行。接受**提案里的条目**（dict，键是 `dueDate`）与库里的行。

    为什么 dict 也认：提案阶段还没落库，而卡片上要展示的正文得先播一版
    （`propose_plan` 没给 doc 时的兜底），两边共用同一套清单格式。
    """
    if isinstance(item, dict):
        mark = "x" if item.get("done") else " "
        due = f"（{item['dueDate']}）" if item.get("dueDate") else ""
        return f"- [{mark}] {item.get('title') or ''}{due}"
    mark = "x" if item.done else " "
    due = f"（{item.due_date.isoformat()}）" if item.due_date else ""
    return f"- [{mark}] {item.title}{due}"


def items_block(items: list[PlanItem | dict]) -> str:
    lines = [item_line(one) for one in items]
    return ITEMS_START + "\n" + "\n".join(lines) + ("\n" if lines else "") + ITEMS_END


def seed(title: str, note: str, items: list[PlanItem | dict]) -> str:
    """建计划时播下的第一版：标题、目标、初始清单、留白。"""
    head = f"# {title or '未命名计划'}\n"
    if note.strip():
        head += "\n> " + note.strip().replace("\n", "\n> ") + "\n"
    return (
        head
        + "\n## 待办\n\n"
        + items_block(items)
        + "\n\n## 计划\n\n"
        + "（这里写目标、拆解思路与进展；条目勾选以计划卡为准。）\n"
    )


def sync_items(plan: Plan, items: list[PlanItem]) -> None:
    """把标记之间的清单换成最新的；没有标记就原样不动。"""
    doc = plan.doc or ""
    start = doc.find(ITEMS_START)
    end = doc.find(ITEMS_END)
    if start < 0 or end < start:
        return
    plan.doc = doc[:start] + items_block(items) + doc[end + len(ITEMS_END) :]


def items_now(db, plan_id: str) -> list[PlanItem]:  # noqa: ANN001
    return list(
        db.scalars(
            select(PlanItem)
            .where(PlanItem.plan_id == plan_id)
            .order_by(PlanItem.sort, PlanItem.created_at)
        ).all()
    )


def ensure(db, plan: Plan) -> None:  # noqa: ANN001
    """老计划（加 `doc` 列之前建的）第一次读写时补上第一版。"""
    if (plan.doc or "").strip():
        return
    plan.doc = seed(plan.title, plan.note, items_now(db, plan.id))


def refresh(db, plan: Plan) -> None:  # noqa: ANN001
    """条目变了：有文档就只刷新清单那一段，没有就补一整份。"""
    items = items_now(db, plan.id)
    if (plan.doc or "").strip():
        sync_items(plan, items)
    else:
        plan.doc = seed(plan.title, plan.note, items)


def read(db, plan_id: str) -> tuple[Plan | None, str]:  # noqa: ANN001
    """`(计划, 文档)`；计划不存在时 `(None, "")`。读即补第一版（按需）。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        return None, ""
    ensure(db, plan)
    return plan, plan.doc or ""


def write(db, plan_id: str, doc: str) -> Plan | None:
    """整份写回（调用方负责 commit / 校验长度）。"""
    plan = db.get(Plan, plan_id)
    if plan is None:
        return None
    plan.doc = doc
    return plan
