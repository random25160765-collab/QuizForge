"""skill：一段提示词，不是代码（`docs/对话树.md` §十二）。

## 它长什么样：`skills/` 目录里的独立 md

一个 skill = 一个文件，**front-matter 定元数据、正文就是那段提示词**：

    ---
    key: feynman
    label: 费曼
    kind: flow          # flow / tone / discipline
    hint: 你讲给我听，我戳你卡住的地方
    ---
    （正文：装进系统提示的那一段）

仓库根目录那个 `skills/` 就是"固定文件夹"：**加一个 skill 就是加一个 md 文件**，
不用动任何代码（这就是"skill 是一段提示词，不是代码"的字面意思）。
读的是**每次调用时的磁盘内容**（按目录与文件的 mtime 缓存），所以改完存盘即生效、
不必重启 —— 边写边调的那十几分钟，全靠这一条。

## 三种形态，可以并存

| 形态 | 它改什么 | 类型 | 怎么用 |
|---|---|---|---|
| **口吻** `tone` | 它**怎么说** | 状态（挂着，持续生效） | 装 / 取 |
| **流程** `flow` | 它**按什么步骤走** | 动作（有开始、有走完） | 发起 / 走完 |
| **纪律** `discipline` | 它**不许做什么** | 状态（挂着，持续生效） | 装 / 取 |

用户（2026-09-25）："流程/口吻/纪律实际上是可以兼容的，我随时也可以取消流程。" ——
所以**没有"同时只能有一条"这种规矩**：挂上的都生效，一条一条都可以随时取下。
真冲突时按 `block()` 末尾那条写明的优先级（纪律压过口吻）。

## 挂在哪、怎么回退

与记号（`places`）同一套存法：一条 `{cid, mid, key, on}` 记录写进设置（前端 `QF.store`
那一侧写，服务端只读 —— 和批注、书签完全一样）。

于是"某个节点上现在挂着什么" = **沿这条链从根往下，每个 key 取最近的那条记录**：

* **沿树继承**是免费得到的（不同的分支本来就走出不同的链）；
* **"走出来"自动回上层**也是免费的（链变了，最近的那条记录就换了）；
* **显式取下**就是那条 `on=False` 的记录；
* `mid` 为空 = 整条对话（第一条消息还没有 mid 可挂）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

#: 固定文件夹：仓库根的 `skills/`（`api/app/skills.py` → 上两级就是仓库根）。
FOLDER = Path(__file__).resolve().parents[2] / "skills"

#: 认得的三种形态，以及**渲染顺序**（流程在前：它是"这一轮在做什么"，
#: 比"怎么说"更要紧，出问题也更容易被发现）。
KINDS: tuple[str, ...] = ("flow", "tone", "discipline")
KIND_LABELS: dict[str, str] = {"flow": "流程", "tone": "口吻", "discipline": "纪律"}

#: 这条链上最多往上找几层（防病态数据把一次生成拖死；与 `recursion` 那几处同一个态度）
_WALK_MAX = 600


@dataclass(frozen=True)
class Skill:
    """一条 skill。`prompt` 就是它的全部实现 —— 加一个能力只是加一个 md。"""

    key: str
    label: str
    kind: str
    hint: str
    prompt: str
    #: 它来自哪个文件（报错、排查、以及"我改的到底是哪一个"都要它）
    source: str = ""


_cache: dict = {"stamp": None, "items": ()}


def _parse(text: str, source: str) -> Skill | None:
    """一个 md → 一条 skill。**不合格的返回 None**（坏一条不该让整个目录空掉）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    key = str(meta.get("key") or "").strip()
    kind = str(meta.get("kind") or "").strip()
    if not key or kind not in KINDS:
        return None
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        return None
    return Skill(
        key=key,
        label=str(meta.get("label") or key).strip(),
        kind=kind,
        hint=str(meta.get("hint") or "").strip(),
        prompt=body,
        source=source,
    )


def _load() -> tuple[Skill, ...]:
    """读那个文件夹（按 mtime 缓存：改完存盘即生效，不必重启）。"""
    if not FOLDER.is_dir():
        return ()
    try:
        files = sorted(one for one in FOLDER.glob("*.md") if one.is_file())
        stamp = tuple((str(one), one.stat().st_mtime_ns) for one in files)
    except OSError:
        return ()
    if _cache["stamp"] == stamp:
        return _cache["items"]
    out = []
    for one in files:
        try:
            skill = _parse(one.read_text(encoding="utf-8"), one.name)
        except OSError:
            continue
        if skill is not None:
            out.append(skill)
    out.sort(key=lambda item: (KINDS.index(item.kind), item.key))
    _cache["stamp"] = stamp
    _cache["items"] = tuple(out)
    return _cache["items"]


def all_skills() -> tuple[Skill, ...]:
    return _load()


def find(word: str) -> Skill | None:
    """`/` 后面那两个字 → 一条 skill（认 key 也认中文名，大小写不敏感）。"""
    raw = str(word or "").strip().lstrip("/").strip()
    if not raw:
        return None
    for one in _load():
        if one.key.lower() == raw.lower() or one.label == raw:
            return one
    return None


def catalog() -> list[dict]:
    """给界面的一份清单（选择器照它渲染）。**不带 `prompt`** —— 那是服务端的事。"""
    return [
        {"key": one.key, "label": one.label, "kind": one.kind, "hint": one.hint, "source": one.source}
        for one in _load()
    ]


def _records(db) -> list[dict]:  # noqa: ANN001
    """设置里那批挂载记录（前端写的，服务端只读）。坏数据一律当没有。"""
    keys = {one.key for one in _load()}
    try:
        from . import settings_store

        conf = settings_store.load(db) or {}
    except Exception:  # noqa: BLE001  skill 是加成，不该让它拖垮一轮生成
        return []
    rows = conf.get("skills")
    if not isinstance(rows, list):
        return []
    out = []
    for one in rows:
        if not isinstance(one, dict):
            continue
        key = str(one.get("key") or "").strip()
        if key in keys:
            out.append(
                {
                    "cid": str(one.get("cid") or ""),
                    "mid": str(one.get("mid") or ""),
                    "key": key,
                    "on": one.get("on") is not False,
                    "at": int(one.get("at") or 0),
                }
            )
    return out


def _parent(db, node):  # noqa: ANN001, ANN202
    """往上走一层（按 `parent_id` 取那一条）。"""
    pid = getattr(node, "parent_id", None)
    if not pid:
        return None
    from .models import Message

    return db.get(Message, int(pid))


def resolve(db, conv, node) -> list[Skill]:  # noqa: ANN001
    """这条链上（根 → `node`）当下挂着哪些 skill。**同一 key 取最近的记录。**

    三种形态一视同仁：**挂上的都生效**（用户："流程/口吻/纪律实际上是可以兼容的"）。
    没有"同时只能有一条流程"这种规矩 —— 取下哪条由他点，不由我们替他收。
    """
    rows = _records(db)
    if not rows:
        return []
    cid = str(getattr(conv, "id", "") or "")
    mine = [one for one in rows if one["cid"] == cid]
    if not mine:
        return []

    by_mid: dict[str, list[dict]] = {}
    for one in mine:
        by_mid.setdefault(one["mid"], []).append(one)

    # 从 node 往上走到根（只取 id —— 这里只需要知道路径上有没有这条记录，不必查正文）
    stack: list[str] = []
    walker = node
    guard = 0
    while walker is not None and guard < _WALK_MAX:
        stack.append(str(getattr(walker, "id", "")))
        walker = _parent(db, walker)
        guard += 1
    stack.reverse()                     # 根 → 节点
    stack.insert(0, "")                 # `mid` 为空 = 整条对话（排在根之前）

    decided: dict[str, bool] = {}
    for mid in stack:                   # 越靠近节点的记录越后写，天然覆盖前面的
        for one in by_mid.get(mid, []):
            decided[one["key"]] = one["on"]

    order = {one.key: index for index, one in enumerate(_load())}
    live = [find(key) for key, on in decided.items() if on]
    live = [one for one in live if one is not None]
    live.sort(key=lambda one: order.get(one.key, 0))
    return live


def block(skills: list[Skill]) -> str:
    """拼成系统提示里那一段。没有就返回空串（调用方据此不加）。"""
    if not skills:
        return ""
    parts = [
        "## 他这一轮给你装了什么（skill）",
        "",
        "他用 `/` 装上的东西在下面。**它们都生效**（可以同时装好几条），"
        "**一条一条都可以随时被他取下** —— 取下之后就别再照它做了，也不要问「还要不要那样」。",
    ]
    for kind in KINDS:
        mine = [one for one in skills if one.kind == kind]
        if not mine:
            continue
        parts.append("")
        parts.append("### " + KIND_LABELS[kind])
        for one in mine:
            parts.append("")
            parts.append("**/" + one.label + "**")
            parts.append(one.prompt)
    parts.append("")
    parts.append(
        "**冲突时怎么排**：纪律压过口吻（「不许做什么」比「怎么说」硬）；"
        "流程要你换一种做法时，按流程来 —— 它管的是这一轮**谁在做主**。"
        "几条之间若实在冲突，按他**最后装上**的那条（`at` 更大的）。"
    )
    return "\n".join(parts)
