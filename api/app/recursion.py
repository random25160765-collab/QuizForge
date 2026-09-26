"""把一条会话的对话树，按「递归学习法」的形状读出来。

## 为什么要单独一个模块

`routers/chat.py` 的 `_messages_of` 给的是**平铺**的整棵树（按 id 升序）。而
「递归学习法」（见 `draft/g.md`）要的恰恰是**形状**：一个概念不懂 → 求讲解 →
讲解里又冒出若干不懂的概念 → **一个一个问（每个是一支）** → 弄明白了回到主线。

平铺的列表里这个形状是看不见的：96 条消息排成一行，8 处下钻淹没在 id 序列里，
「谁是谁的下钻」只能靠人脑重排。所以这里做三件事：

1. **缩进骨架** —— 按 `parent_id` 从根往下走，钻的层次一目了然；
2. **分叉清单** —— 每个"一个回答挂了多个提问"的地方。那正是递归学习法的**动作**
   本身："你这段讲解里，有几个词我不懂，我一个个问"；
3. **把两种分叉分开** —— 见下。

## 两种分叉必须分开（这是一个踩过的坑）

同一个父节点下的兄弟，**内容不同**的是**真下钻**（递归学习法本身）；**内容相近**
的是**改写重发**（"我这句话没说清"或"上一次它没答到"）。两者在结构上长得一模一样，
但含义完全相反。

我自己第一次读这棵树时把后者读成了"对回答不满意"（"隐式踩"）—— 看数据就站不住：
`用联网搜索帮我查一下「Tenstorrent」` 出现两次，第一次的回复是**空的**（当时联网没配），
用户重发了一次而已。**把"重试"读成"差评"，整份复盘就全错了。**
所以这里按相似度把它们分开标出来，不让下游再猜。

## 一条不能替模型下的判断

**"这一支弄明白了" 和 "这一支被丢下了"，从树里看不出来**。用户在子问题上问完，
可以回到主线接着学（那一支就停了），也可以是觉得没讲清先放着 —— 两种在树里
都是"叶子"。所以这里**只标叶子、不判定死活**，让读它的模型结合内容去说。
（这个区分本来就不该由代码下结论：它是"我觉得懂了没有"，只有用户知道。）
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Conversation, Message

#: 长句相似度到多少算"改写重发"（判据见 `same_question`）。取 0.62 是照实测调的：
#: `…那就好了，这个软件有缺陷` 与 `…那就好了。我是这个软件的开发者和第一个使用者…`
#: 是同一句改写（≈0.73）该并；而 `缓存行为分析？` 与 `一旦访问模式变成数据相关…`
#: 是同一轮里两个不同概念（≈0.1），不该并。
SAME_QUESTION = 0.62

#: 短于这个字数的消息**只认"原样或包含"**，不比相似度 —— 理由见 `same_question`：
#: 短句里差一个字往往就是换了个概念（`A 是什么？` / `B 是什么？`），比相似度必然误判。
REWORD_AT_LEAST = 12

#: 骨架里每行预览留多少字。只够认出"这轮在讲什么"，
#: 要看全的就按 id 单独取（这是"骨架 + 按需读"两段式的前半段）。
PREVIEW = 44


def tree(db: Session, conv: Conversation) -> list[Message]:
    """整棵树，按 id 升序（`messages.parent_id` 构成树；根节点 parent_id 为空）。"""
    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.id)
    ).all()
    return list(rows)


def _flat(text: str) -> str:
    """把一句话压成可比对的形状：去空白、去标点，只留实义字符。"""
    return re.sub(r"[\s，。、？！,.?!…—\-—\"'“”‘’（）()\[\]【】:：;；~]+", "", text or "")


def similar(a: str, b: str) -> float:
    """两条消息字面上像不像（0~1）。只作参考 —— **判定请用 `same_question`**。"""
    x, y = _flat(a), _flat(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def same_question(a: str, b: str) -> bool:
    """这两条是不是"同一个问题又发了一遍"（而不是两个不同的概念）。

    判据从硬到软，三档：

    1. 压掉空白标点后**一模一样** —— 原样重发；
    2. 一条**包含**另一条 —— "在原来那句后面又补了一句"。真实例：
       `…差不多可以开始学WCET了，信息凑齐了` → 同一句后面加了"另外你现在有联网能力了"；
    3. 长句（≥ `REWORD_AT_LEAST` 字）且相似度 ≥ `SAME_QUESTION` —— 换了种说法。

    **短句不走第 3 档**，这一条是踩出来的：`A 是什么？` 与 `B 是什么？` 压掉标点后
    相似度 0.86，可它们是**两个不同的概念** —— 而那正是递归学习法最典型的问法
    （"你这段讲解里，这几个词我一个个问"）。放宽就把真实的下钻抹掉了。

    所以取舍是**宁可漏判**：漏判把"重发"当成一次下钻，路径上多一条；
    误判把"下钻"当成重发，路径上**少一支** —— 后者是丢信号。
    """
    x, y = _flat(a), _flat(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, longer = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= 4 and short in longer:
        return True
    if len(short) < REWORD_AT_LEAST:
        return False
    return difflib.SequenceMatcher(None, x, y).ratio() >= SAME_QUESTION


def _depth_and_order(messages: list[Message]) -> list[dict]:
    """按 `parent_id` 从根往下走，给每条算出**真实深度**与**缩进层级**。

    两者**刻意分开**。缩进只在**真分叉**处加深：一条 45 层的直线链每一层都缩两格，
    光缩进就是 90 个空格，而那一层**只有一个孩子** —— 缩进没有区分出任何东西，
    却让骨架从两千多字涨到九千多（实测：这是它的主要体积来源）。
    单传的一层不缩，形状靠**序列本身**表达；有分叉的那一层才缩，那才是形状。

    用显式栈而不是递归：`_chain` 那边已经吃过一次教训（要有防环与上限），
    而这里的树可能很深（实测那条 45 层），递归栈同样会白给一层风险。
    """
    by_parent: dict[int | None, list[Message]] = defaultdict(list)
    for m in messages:
        by_parent[m.parent_id].append(m)

    out: list[dict] = []
    # 根可能不止一个（会话里消息被删过、或数据是从别处并过来的）
    stack = [(m, 0, 0) for m in reversed(by_parent.get(None, []))]
    seen: set[int] = set()
    while stack:
        msg, depth, indent = stack.pop()
        if msg.id in seen or len(out) >= 800:  # 防环 + 防病态数据把复盘卡死
            continue
        seen.add(msg.id)
        out.append({"msg": msg, "depth": depth, "indent": indent})
        kids = by_parent.get(msg.id, [])
        step = 1 if len(kids) > 1 else 0  # 只有分叉才加深缩进
        for kid in reversed(kids):
            stack.append((kid, depth + 1, indent + step))
    out.sort(key=lambda row: row["msg"].id)
    return out


def _beneath(by_parent: dict, mid: int) -> int:
    """从 `mid` 往下还能钻几层（叶子是 0）。用来标"这一支你钻了多深"。"""
    kids = by_parent.get(mid) or []
    if not kids:
        return 0
    return 1 + max(_beneath(by_parent, k.id) for k in kids)


def forks(messages: list[Message]) -> list[dict]:
    """所有分叉点：一个消息下面挂了多个**同角色**的孩子。

    同角色是为了分开两件事：`user` 兄弟 = "从这段讲解里挑出了几个概念，一个个问"
    （递归学习法的动作）；`assistant` 兄弟 = 同一个提问生成了多个回答。
    """
    by_parent: dict[int | None, list[Message]] = defaultdict(list)
    for m in messages:
        by_parent[m.parent_id].append(m)

    out: list[dict] = []
    for parent_id, kids in by_parent.items():
        if parent_id is None:
            continue
        for role in ("user", "assistant"):
            same = [k for k in kids if k.role == role]
            if len(same) < 2:
                continue
            parent = next((m for m in messages if m.id == parent_id), None)
            # 兄弟两两比一遍：谁跟前面某一条是"同一个问题"，谁就是改写重发
            again: set[int] = set()
            for i, one in enumerate(same):
                for other in same[:i]:
                    if same_question(one.content or "", other.content or ""):
                        again.add(one.id)
                        break
            out.append(
                {
                    "parentId": parent_id,
                    "parent": parent,
                    "role": role,
                    "children": same,
                    #: 哪几条是"把同一句话又发了一遍"（不是新概念）
                    "reask": again,
                    #: 真正算"从这段讲解里挑出的概念"的那几条
                    "fresh": [k for k in same if k.id not in again],
                }
            )
    out.sort(key=lambda f: f["parentId"])
    return out


def _preview(text: str, preview: int = PREVIEW) -> str:
    """压成一行预览。`preview` 可调：仪表盘给模型看的那些要更短（它每轮都背着）。"""
    line = " ".join((text or "").split())
    return line[:preview] + ("…" if len(line) > preview else "")


def stamp(at: datetime | None) -> str:
    """一个时间戳，按**这台机器的本地时间**写（`%m-%d %H:%M`）。

    库里存的是 UTC（`UtcDateTime` 的规矩），直接 `strftime` 出来就是 UTC 的墙上时间 ——
    在 UTC+8 的机器上整整差 8 小时。实测就是从这里露出来的：仪表盘把 09-21 04:52 报给
    模型，模型照着说"04:52 开工"，而真实时间是中午 12:52（用户："我不会在凌晨学习，
    肯定数据错了。我记得那天是下午，要么就是晚上"）。

    前端一直是把 `createdAtMs` 交给**浏览器**按本地时区渲染的（见 `chat.py` 的
    `_message_out`），这里做的是同一件事，只是发生在服务端 —— 而这套服务就跑在
    他自己这台机器上。凡是服务端要写给人看的时间，都走这一个函数（别处再抄一份
    时区规矩，迟早有一处漏掉）。
    """
    if not isinstance(at, datetime):
        return ""
    if at.tzinfo is not None:
        at = at.astimezone()
    return at.strftime("%m-%d %H:%M")


def _stamp(m: Message) -> str:
    """一条消息的时间戳（规矩只有一份，见 `stamp`）。"""
    return stamp(m.created_at)


def outline(db: Session, conv: Conversation, *, preview: int = PREVIEW) -> str:
    """把整棵树渲染成模型能读的文本（骨架 + 分叉清单）。

    骨架里 `◆` 标的是"这一处有分叉" —— 也就是递归学习法**下钻发生的地方**。
    没有它就是"顺着主线往下"，不需要特别留意。
    """
    messages = tree(db, conv)
    if not messages:
        return "（这条会话还没有消息）"

    rows = _depth_and_order(messages)
    fork_list = forks(messages)
    #: 两种分叉**分开标**（结构一样、含义相反，混成一个记号就没法只看骨架判断）：
    #:   `◆` 你从这段讲解里挑出了多个概念、分别下钻 —— 递归学习法的动作本身
    #:   `⟳` 同一个提问又生成了一次 —— 重试 / 重新生成，不是新概念
    drill_at = {f["parentId"] for f in fork_list if f["role"] == "user"}
    regen_at = {f["parentId"] for f in fork_list if f["role"] == "assistant"}
    reask_all = {i for f in fork_list for i in f["reask"]}
    drill_list = [f for f in fork_list if f["role"] == "user"]
    regen_list = [f for f in fork_list if f["role"] == "assistant"]

    depth_max = max(r["depth"] for r in rows)
    first, last = messages[0].created_at, messages[-1].created_at
    span = ""
    if isinstance(first, datetime) and isinstance(last, datetime):
        span = "%s → %s" % (first.strftime("%m-%d %H:%M"), last.strftime("%m-%d %H:%M"))

    out: list[str] = []
    out.append("会话「%s」" % (conv.title or "(无题)"))
    out.append(
        "%d 条消息 · %s · 最深 %d 层 · %d 处下钻"
        % (len(messages), span, depth_max, len(fork_list))
    )
    out.append("")
    out.append("## 树的形状")
    out.append("缩进 = 从上一轮的讲解往下钻，**只在分叉处加深**"
               "（单传的一层不缩 —— 否则一条几十层的直线链会被缩进撑爆）")
    out.append("记号：`◆` 下钻 / `⟳` 重新生成 / `↺` 改写重发（两个并排出现 = 两件事同时成立）")
    out.append("行末 `⟨+N⟩` = 这一支往下还钻了 N 层（负号没有，叶子就是不加）")
    out.append("")
    by_parent: dict = defaultdict(list)
    for m in messages:
        by_parent[m.parent_id].append(m)
    for row in rows:
        m = row["msg"]
        pad = "  " * row["indent"]
        # 三种记号**可以同时成立**（真实例：`#515` 既是"同一句话又发了一遍"，
        # 又是"这一轮生成了两次"的父节点）—— 所以是叠加，不是 `elif`。
        # 原先是 `elif`，那条只显示了 `⟳`，于是**树里数得出 3 个 ↺、
        # 而小结说 4 次**，两个数当面打架 —— 读的人会从这里开始不信整份东西。
        marks = ""
        if m.id in drill_at:
            marks += "◆"
        if m.id in regen_at:
            marks += "⟳"
        if m.id in reask_all:
            marks += "↺"
        mark = marks.ljust(2) if marks else "  "
        who = "你" if m.role == "user" else "AI"
        below = _beneath(by_parent, m.id)
        tail = "  ⟨+%d⟩" % below if below else ""
        out.append(
            "%s#%-5d%s %s %s%s" % (pad, m.id, mark, who, _preview(m.content or ""), tail)
        )

    out.append("")
    out.append("## 下钻清单（%d 处 = %d 个新概念 + %d 次改写重发）"
               % (len(drill_list), sum(len(f["fresh"]) for f in drill_list),
                  sum(len(f["reask"]) for f in drill_list)))
    if not drill_list:
        out.append("（这条会话没有下钻 —— 一路顺着主线问下来的）")
    for f in drill_list:
        parent = f["parent"]
        out.append("### 在 #%d 这一轮之后分出 %d 支" % (f["parentId"], len(f["children"])))
        if parent is not None:
            out.append("    上一轮 AI 讲的：%s" % _preview(parent.content or ""))
        for kid in f["children"]:
            tag = "（改写重发）" if kid.id in f["reask"] else ""
            below = _beneath(by_parent, kid.id)
            # "这一支钻了多深"是复盘里最想要的一个数：它直接就是"这个概念的难度"
            out.append(
                "    #%-5d %s %s%s%s"
                % (kid.id, _stamp(kid), _preview(kid.content or ""), tag,
                   "   ↓ 这一支钻了 %d 层" % below if below else "")
            )

    if regen_list:
        out.append("")
        out.append("## 重新生成（%d 处，不是下钻）" % len(regen_list))
        for f in regen_list:
            out.append("    在 #%d 之后生成了 %d 次 → %s"
                       % (f["parentId"], len(f["children"]),
                          "、".join("#%d" % k.id for k in f["children"])))
    return "\n".join(out)


def read_nodes(db: Session, conv: Conversation, ids: list, *, limit: int = 12) -> str:
    """读出指定几条消息的**全文**（骨架里只有预览，要细看某一处用这个）。

    为什么不做成"一次把全文全都给它"：实测那条 100 条消息的会话，全文展开是骨架的
    十几倍，而复盘真正需要逐字看的通常只有几处（分叉点的上一轮、以及他反复下钻的
    那几个词）。所以是两段式：**先看形状，再按 id 取**。
    """
    wanted: list[int] = []
    for one in ids or []:
        try:
            wanted.append(int(one))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return "（没有给出要读的消息 id）"

    rows = {m.id: m for m in tree(db, conv)}
    out: list[str] = []
    for mid in wanted[:limit]:
        m = rows.get(mid)
        if m is None:
            out.append("#%d —— 这条不在该会话里（id 抄错了？）" % mid)
            continue
        out.append("### #%d %s %s" % (mid, "你" if m.role == "user" else "AI", _stamp(m)))
        out.append((m.content or "（这条是空的）").strip())
        out.append("")
    return "\n".join(out)


def brief(db: Session, conv: Conversation) -> dict:
    """给界面/上层用的小结（不做文本渲染，纯数字）。"""
    messages = tree(db, conv)
    fork_list = forks(messages)
    rows = _depth_and_order(messages)
    return {
        "messages": len(messages),
        "depth": max((r["depth"] for r in rows), default=0),
        "forks": len(fork_list),
        #: 真正"从讲解里挑出的新概念"总数 —— 递归学习法的下钻次数
        "drills": sum(len(f["fresh"]) for f in fork_list if f["role"] == "user"),
        #: 同一句话重发了几次（说明没对齐，不是新概念）
        "reasks": sum(len(f["reask"]) for f in fork_list if f["role"] == "user"),
    }

#: 树小于这么多条时，仪表盘直接给骨架 —— 形状是现成的，"一眼看到全貌"比解释一遍便宜。
#: 再长就只给数字与下钻清单：骨架在长对话里能到几千字，而它**每一轮都要背一遍**。
OUTLINE_MAX = 40


def _stops_here(by_parent: dict, root_id: int) -> bool:
    """这一支下面还有没有他的下一条提问（没有 = 问到这儿就停了）。

    **只报事实，不下判断**：停住有三种（故障 / 暂停 / 结束），形状一模一样，
    只有他自己知道是哪种（`docs/对话树.md`「停住不等于逃了」那一节）。
    所以这里只写"没再往下问"，让模型自己别替我盖棺。
    """
    frontier = list(by_parent.get(root_id) or [])
    seen: set[int] = set()
    while frontier:
        one = frontier.pop()
        if one.id in seen:
            continue
        seen.add(one.id)
        if one.role == "user":
            return False
        frontier.extend(by_parent.get(one.id) or [])
    return True


def _recent(db: Session, conv: Conversation, limit: int) -> list[str]:
    """最近还在看的几条对话（长期那一行的料）。"""
    try:
        rows = db.scalars(
            select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit + 1)
        ).all()
    except Exception:  # noqa: BLE001  仪表盘的一条装饰，不值得让整轮失败
        return []
    out = []
    for row in rows:
        if row.id == conv.id:
            continue
        title = _preview(row.title or "未命名")
        stamp = row.updated_at.strftime("%m-%d") if isinstance(row.updated_at, datetime) else ""
        out.append("《%s》%s" % (title, ("（" + stamp + "）") if stamp else ""))
        if len(out) >= limit:
            break
    return out


def _my_marks(db: Session, conv: Conversation, limit: int) -> tuple[dict, list[str]]:
    """他在**这一段对话里**自己留下的记号：批注 / 书签 / 回溯。

    存在设置里（前端 `QF.store` 的 notes / places，见 `settings_store`），所以读一行就够。
    这东西的用法与批注一样：**他自己写下的字，是他最直接的自我报告** ——
    哪句他标了、哪句他写了话，比任何推断都准。
    """
    try:
        from . import settings_store

        conf = settings_store.load(db) or {}
    except Exception:  # noqa: BLE001
        return {}, []
    cid = str(conv.id)
    notes = [one for one in (conf.get("notes") or []) if str(one.get("cid") or "") == cid]
    places = [one for one in (conf.get("places") or []) if str(one.get("cid") or "") == cid]
    counts = {
        "批注": len([one for one in notes if str(one.get("kind") or "note") == "note"]),
        "高亮": len([one for one in notes if str(one.get("kind") or "") == "hl"]),
        "删除线": len([one for one in notes if str(one.get("kind") or "") == "strike"]),
        "下划线": len([one for one in notes if str(one.get("kind") or "") == "underline"]),
        "书签": len([one for one in places if str(one.get("kind") or "back") == "mark"]),
        "回溯": len([one for one in places if str(one.get("kind") or "back") != "mark"]),
    }
    said = [
        _preview(str(one.get("text") or ""), preview=80)
        for one in notes
        if str(one.get("kind") or "note") == "note" and str(one.get("text") or "").strip()
    ]
    return counts, said[:limit]


def dashboard(
    db: Session, conv: Conversation, node: Message | None = None, *, preview: int = PREVIEW
) -> str:
    """**每一轮都挂进系统提示**的「他的学习轨迹」—— 它不是工具，是仪表盘。

    为什么做成常驻而不是工具（用户："我认为它不应该被做成工具，对话轨迹是 llm 长期
    可见的用户信息仪表盘"）：工具是**要它想起来的**东西，而"这个人在怎么学"不是一次
    查询，是它说每句话时都该带着的背景。做成工具的时候，模型只会在被明确要求复盘时
    才去看一眼，平时对这位用户一无所知 —— 于是答案的口径退回成普通聊天。

    四块照 `docs/对话树.md`「仪表盘：给模型看的轨迹」那一节：
      ① 我在哪 ② 我怎么来的 ③ 我探过什么 ④ 我还欠什么
    再加两行长期的：他最近还在看什么、他在这一段里自己留下的记号。

    体积要控住（每一轮都背，不是查一次就丢）：长对话只给数字与清单，
    骨架只在短对话里给（见 `OUTLINE_MAX`）。
    """
    messages = tree(db, conv)
    if not messages:
        return ""
    rows = _depth_and_order(messages)
    by_parent: dict = defaultdict(list)
    for one in messages:
        by_parent[one.parent_id].append(one)
    by_id = {one.id: one for one in messages}
    fork_list = forks(messages)
    title = _preview(conv.title or "未命名", preview=30)

    # ① 我在哪
    here = node if (node is not None and node.id in by_id) else rows[-1]["msg"]
    depth = next((row["depth"] for row in rows if row["msg"].id == here.id), 0)
    block = [
        "## 他的学习轨迹（仪表盘 · 每轮都在，不必去查）",
        "",
        # **这两句是这一版新加的，为的是省掉模型的"对账"**：它原来会拿仪表盘里的条数
        # 与自己手上的历史反复核对（实测的思考记录："13 messages …但我的历史只有 4 条
        # 用户消息…Do I have them in my context?"），因为没人告诉它两者本来就不是一回事。
        "**这份是整棵树**（含你手上那条线以外的分支）；**你手上的历史只是当前那条主线**，"
        "被拍平过 —— 两者条数对不上是正常的，形状以这份为准。",
        "他问「描述我的对话历史 / 我这次学了什么 / 我是不是跑偏了 / 这样学对不对」时，"
        "**照这份直接答**：要的数字、概念、分支都在下面。每个分支这里只有预览，"
        "要某一条的原文就让他指认或把那一段贴上来（已经没有「按 id 取全文」这个动作）。",
        "",
        "**① 我在哪**：这一段《%s》共 %d 条、最深 %d 层、%d 处下钻（新概念，改写重发不算）、"
        "%d 次重新生成；你这一轮回答的是第 %d 层上的（消息 #%d，%s，问的是「%s」）。"
        "他这一段是从 %s 说到现在的。"
        % (
            title,
            len(messages),
            max(r["depth"] for r in rows),
            sum(len(f["fresh"]) for f in fork_list if f["role"] == "user"),
            sum(len(f["children"]) - 1 for f in fork_list if f["role"] == "assistant"),
            depth,
            here.id,
            _stamp(here),
            _preview(here.content, preview),
            # 时间是他自己要看的线索（原话："所以你能看到时间戳吗？如果有时间顺序，
            # 那就好办了"）—— 轨迹少一半意义就没有时间轴。
            _stamp(messages[0]),
        ),
    ]

    # ② 我怎么来的（主线往上的那几步）
    path = []
    walker = by_id.get(here.id)
    guard = 0
    while walker is not None and guard < 500:
        if walker.role == "user" and str(walker.content or "").strip():
            path.append(walker)
        walker = by_id.get(walker.parent_id)
        guard += 1
    path.reverse()
    if path:
        shown = path[-8:]
        block.append(
            "**② 他怎么来的**（主线，最近 %d 步%s）：%s"
            % (
                len(shown),
                "，更早的在历史里" if len(path) > len(shown) else "",
                " → ".join("#%d %s" % (one.id, _preview(one.content, 30)) for one in shown),
            )
        )

    # ③ 他探过什么（下钻清单）+ ④ 还欠什么（没再往下问的那几支）
    drills = []
    for fork in fork_list:
        if fork["role"] != "user":
            continue
        for kid in fork["fresh"]:
            drills.append(
                {
                    "id": kid.id,
                    "what": _preview(kid.content, preview),
                    "size": _beneath(by_parent, kid.id) + 1,
                    "at": _stamp(kid),
                    "open": _stops_here(by_parent, kid.id),
                }
            )
    if drills:
        block.append(
            # 口径写明：这份只数**新概念**（他停下来单独问出去的）。骨架里那份"下钻清单"
            # 把"新概念 + 改写重发"合在一起数，两处数字不同名不同义 —— 不说清，模型会
            # 在两份之间对不上（实测的思考记录就卡在这儿）。
            "**③ 他探过什么**（这一段里他停下主线、单独问出去的**新概念**，共 %d 处；"
            "改写重发与重新生成不算，它们不是新概念）：" % len(drills)
        )
        for one in drills[:10]:
            block.append(
                "  *「%s」（#%d · %s · 这一支 %d 条 · 往下 %d 层）"
            % (one["what"], one["id"], one["at"], one["size"], one["size"] - 1)
            )
        if len(drills) > 10:
            block.append("  *（另有 %d 处，要看全就看下面的骨架）" % (len(drills) - 10))
    idle = [one for one in drills if one["open"]]
    if idle:
        block.append(
            "**④ 他还欠什么**：这几支**问到这儿就停了**：%s。"
            "**停住不等于放弃** —— 停、暂停、结束三种形状一模一样，只有他知道是哪种，"
            "别替他下结论，也别催。"
            % "、".join("「%s」" % one["what"] for one in idle[:6])
        )
    if len(messages) <= OUTLINE_MAX:
        block.append("")
        block.append("**这一段对话的树**（`◆` = 他在那儿下钻）：")
        block.append(outline(db, conv, preview=preview))

    # 长期那两行
    recent = _recent(db, conv, 5)
    if recent:
        block.append("**他最近还在看**：" + "；".join(recent) + "。")
    counts, said = _my_marks(db, conv, 2)
    if counts and any(counts.values()):
        line = " · ".join("%s %d" % (name, num) for name, num in counts.items() if num)
        block.append("**他自己的记号**（就在这一段对话里）：" + line + "。")
        for text in said:
            block.append("  * 他写：「%s」" % text)
    return "\n".join(block).strip()
