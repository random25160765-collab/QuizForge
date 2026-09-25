"""skill（`app/skills.py`）—— 三件事：**从 md 装得进来**、认得三种形态、挂在链上取得到。

用户（2026-09-25）两条新规矩，都有用例钉着：

    "流程/口吻/纪律实际上是可以兼容的，我随时也可以取消流程。"

于是**没有"同时只能有一条"**：挂上的都生效，一条一条都可以取下。

挂载那套语义是照 `docs/对话树.md` §十二 来的：

    * **沿树继承**：根上挂的，下游每一条都算挂着；
    * **可回退**：链变了就算回到上层（这里用"另一条分支"验）；
    * **显式取下**：一条 `on: false` 的记录压掉它。
"""

from __future__ import annotations

from app import skills
from app.models import Conversation, Message
from app.routers import chat as chat_router


def _conv(db, title="skill 测试"):  # noqa: ANN001
    conv = Conversation(title=title)
    db.add(conv)
    db.flush()
    return conv


def _say(db, conv, role, text, parent=None):  # noqa: ANN001
    msg = Message(
        conversation_id=conv.id,
        role=role,
        status="ok",
        content=text,
        parent_id=parent.id if parent is not None else None,
    )
    db.add(msg)
    db.flush()
    return msg


def _hang(db, conv, node, key, on=True):  # noqa: ANN001
    """把一条 skill 挂在某个节点上（前端 `QF.store` 那一侧平时就是这么写的）。"""
    from app import settings_store

    rows = list((settings_store.load(db) or {}).get("skills") or [])
    rows.append({"cid": str(conv.id), "mid": str(node.id), "key": key, "on": on})
    settings_store.put(db, skills=rows)


def test_skills_come_from_the_folder_as_separate_markdown_files() -> None:
    """**一个 skill = 一个 md**（用户："skill 应该是独立 md，从一个固定文件夹里装载的"）。

    所以这一条盯的是"装载"这件事本身：文件夹里那些文件都能解析出来，
    三种形态都在，而且**没有康奈尔**（用户点名不要它）。
    """
    items = skills.all_skills()
    assert skills.FOLDER.is_dir(), "固定文件夹不存在：" + str(skills.FOLDER)
    assert len(items) >= 5, "至少该有那几条流程"
    kinds = {one.kind for one in items}
    assert kinds == {"flow", "tone", "discipline"}, "三种形态都该有："
    assert all(one.prompt for one in items), "正文（= 提示词）不能为空"
    assert not any("康奈尔" in one.label for one in items), "康奈尔笔记不要做成 skill"


def test_a_broken_file_is_skipped_and_does_not_kill_the_folder() -> None:
    """坏一条不该让整个目录空掉（写错一个 key 就全部消失，那没法用）。"""
    assert skills._parse("没有 front-matter 的正文", "x.md") is None
    assert skills._parse("---\nkey: x\n---\n", "x.md") is None, "没有 kind"
    assert skills._parse("---\nkey: x\nkind: 乱写的\n---\n正文", "x.md") is None
    assert skills._parse("---\nkind: flow\n---\n正文", "x.md") is None, "没有 key"
    assert skills._parse("---\nkey: x\nkind: flow\n---\n\n", "x.md") is None, "正文是空的"
    good = skills._parse("---\nkey: x\nlabel: 某\nkind: flow\nhint: 一句\n---\n正文", "x.md")
    assert good is not None and good.prompt == "正文"


def test_a_command_is_found_by_key_or_by_its_chinese_name() -> None:
    """`/费曼` 与 `/feynman` 是同一条 —— 他喊的是中文，写的是 key。"""
    assert skills.find("/费曼").key == "feynman"
    assert skills.find("feynman").key == "feynman"
    assert skills.find("  /SQ3R ") is not None
    assert skills.find("/没这个") is None
    assert skills.find("") is None


def test_the_catalog_does_not_carry_the_prompts() -> None:
    """发给界面的清单里**没有 `prompt`** —— 那是服务端拼提示词用的，界面只要名字与说明。"""
    rows = skills.catalog()
    assert rows
    assert all({"key", "label", "kind", "hint"} <= set(row) for row in rows)
    assert all("prompt" not in row for row in rows)


def test_the_three_kinds_coexist_and_each_can_be_taken_off(db_session) -> None:  # noqa: ANN001
    """**挂上的都生效**（用户可以同时装流程 + 口吻 + 纪律），没有"只能有一条"。"""
    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    tail = _say(db_session, conv, "assistant", "回答", root)
    db_session.commit()

    _hang(db_session, conv, root, "feynman")
    _hang(db_session, conv, root, "plain")
    _hang(db_session, conv, root, "no-answer")

    live = skills.resolve(db_session, conv, tail)
    assert [one.key for one in live] == ["feynman", "plain", "no-answer"], "三条一起生效，按形态排"

    _hang(db_session, conv, tail, "plain", on=False)
    live = skills.resolve(db_session, conv, tail)
    assert [one.key for one in live] == ["feynman", "no-answer"], "取下其中一条，其余还在"


def test_a_record_hangs_from_its_node_down(db_session) -> None:  # noqa: ANN001
    """根上挂的，下游每一条都算挂着（沿树继承）；兄弟分支也算 —— 同一个根下面。"""
    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    deep = _say(db_session, conv, "assistant", "回答", root)
    deeper = _say(db_session, conv, "user", "接着问", deep)
    db_session.commit()

    _hang(db_session, conv, root, "feynman")

    assert [one.key for one in skills.resolve(db_session, conv, deeper)] == ["feynman"]
    assert [one.key for one in skills.resolve(db_session, conv, root)] == ["feynman"]


def test_the_nearest_record_wins_per_key(db_session) -> None:  # noqa: ANN001
    """**同一条 key** 上，越靠近当前节点的记录越算数（沿树覆盖 + 可回退）；
    **不同的 key 各算各的** —— 它们本来就该并存（用户："三种形态实际上是可以兼容的"）。"""
    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    mid = _say(db_session, conv, "assistant", "回答", root)
    tail = _say(db_session, conv, "user", "接着问", mid)
    db_session.commit()

    _hang(db_session, conv, root, "feynman")
    _hang(db_session, conv, mid, "plain")
    assert [one.key for one in skills.resolve(db_session, conv, tail)] == ["feynman", "plain"], "两条并存"

    _hang(db_session, conv, mid, "feynman", on=False)   # 同一条 key，更近的记录关掉它
    assert [one.key for one in skills.resolve(db_session, conv, tail)] == ["plain"]
    assert [one.key for one in skills.resolve(db_session, conv, root)] == ["feynman"], "上层的还在"


def test_records_from_another_conversation_do_not_leak(db_session) -> None:  # noqa: ANN001
    """按 `cid` 分开：别处那条对话挂的东西，不会跑到这一条里来。"""
    here = _conv(db_session, "这一条")
    there = _conv(db_session, "另一条")
    node_here = _say(db_session, here, "user", "问")
    node_there = _say(db_session, there, "user", "问")
    db_session.commit()

    _hang(db_session, there, node_there, "feynman")
    assert skills.resolve(db_session, here, node_here) == []


def test_the_block_rides_along_in_both_modes() -> None:
    """它进系统提示，而且**极简模式也带** —— 与挂载无关（那是"你要我怎么陪你"）。"""
    note = skills.block([skills.find("费曼"), skills.find("大白话")])
    assert "/费曼" in note and "/大白话" in note
    assert "流程" in note and "口吻" in note, "按形态分节"

    full = chat_router.build_prompt({"quiz"}, None, ["get_mastery"], "", note)
    assert note in full and chat_router.SKILL_TAIL in full, "末尾那句压过风格要求的提醒要在"

    minimal = chat_router.build_prompt(set(), None, [], "", note)
    assert note in minimal, "极简模式同样带着它"

    assert skills.block([]) == ""
