"""lens / 滤镜（`app/lens.py`）—— **一条滤镜就是一个 md**、认得三种形态、挂在链上取得到，
以及它现在**可增可改可删**（存在数据目录里，见 `routers/lens.py`）。

用户（2026-09-25）两条新规矩，都有用例钉着：

    "流程/口吻/纪律实际上是可以兼容的，我随时也可以取消流程。"

于是**没有"同时只能有一条"**：挂上的都生效，一条一条都可以取下。

挂载那套语义是照 `docs/对话树.md` §十二 来的：

    * **沿树继承**：根上挂的，下游每一条都算挂着；
    * **可回退**：链变了就算回到上层（这里用"另一条分支"验）；
    * **显式取下**：一条 `on: false` 的记录压掉它。
"""

from __future__ import annotations

import pytest

from app import lens
from app.models import Conversation, Message
from app.routers import chat as chat_router


def _conv(db, title="lens 测试"):  # noqa: ANN001
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
    """把一条滤镜挂在某个节点上（前端 `QF.store` 那一侧平时就是这么写的）。"""
    from app import settings_store

    rows = list((settings_store.load(db) or {}).get("lens") or [])
    rows.append({"cid": str(conv.id), "mid": str(node.id), "key": key, "on": on})
    settings_store.put(db, lens=rows)


def test_lenses_come_from_the_folder_as_separate_markdown_files() -> None:
    """**一条滤镜 = 一个 md**，放在数据目录的 `lens/` 里。

    开发机上那十条是仓库 `lens/` 的预置（第一次读时拷进数据目录）。
    这一条盯的是"装载"这件事本身：文件都在、三种形态都有、正文都不空。
    """
    items = lens.all_lenses()
    assert lens.folder().is_dir(), "滤镜文件夹不存在：" + str(lens.folder())
    assert len(items) >= 5, "至少该有那几条流程"
    kinds = {one.kind for one in items}
    assert kinds == {"flow", "tone", "discipline"}, "三种形态都该有"
    assert all(one.prompt for one in items), "正文（= 提示词）不能为空"
    assert not any("康奈尔" in one.label for one in items), "康奈尔笔记不要做成滤镜"


def test_a_broken_file_is_skipped_and_does_not_kill_the_folder() -> None:
    """坏一条不该让整个目录空掉（写错一个 key 就全部消失，那没法用）。"""
    assert lens._parse("没有 front-matter 的正文", "x.md") is None
    assert lens._parse("---\nkey: x\n---\n", "x.md") is None, "没有 kind"
    assert lens._parse("---\nkey: x\nkind: 乱写的\n---\n正文", "x.md") is None
    assert lens._parse("---\nkind: flow\n---\n正文", "x.md") is None, "没有 key"
    assert lens._parse("---\nkey: x\nkind: flow\n---\n\n", "x.md") is None, "正文是空的"
    good = lens._parse("---\nkey: x\nlabel: 某\nkind: flow\nhint: 一句\n---\n正文", "x.md")
    assert good is not None and good.prompt == "正文"


def test_a_lens_is_found_by_key_or_by_its_chinese_name() -> None:
    """`费曼` 与 `feynman` 是同一条 —— 他喊的是中文，写的是 key。

    `/` 命令已经删了（2026-11-06）：带斜杠的词不再被认成滤镜。
    """
    assert lens.find("费曼").key == "feynman"
    assert lens.find("feynman").key == "feynman"
    assert lens.find("  SQ3R ") is not None
    assert lens.find("没这个") is None
    assert lens.find("/费曼") is None, "带斜杠的词不再被认成滤镜"
    assert lens.find("") is None


def test_the_catalog_does_not_carry_the_prompts() -> None:
    """发给界面的清单里**没有 `prompt`** —— 那是服务端拼提示词用的，界面只要名字与说明。"""
    rows = lens.catalog()
    assert rows
    assert all({"key", "label", "kind", "hint"} <= set(row) for row in rows)
    assert all("prompt" not in row for row in rows)


def test_a_new_lens_is_written_as_one_file_and_can_be_deleted_again() -> None:
    """**界面上能加、能删**：加一条 = 落一个 md；删一条 = 那个文件没了。

    用的是能一眼认出、删得掉的 key（`zz-` 开头），不碰预置那几条。
    """
    key = "zz-test-lens"
    try:
        one = lens.save(key=key, label="测试用", kind="tone", hint="一句话", prompt="你现在要简短。")
        assert (lens.folder() / f"{key}.md").is_file(), "应当落成一个 md"
        assert [row["key"] for row in lens.catalog() if row["key"] == key] == [key]
        assert lens.find(key).prompt == "你现在要简短。"
        # 改：同一个 key 再存一次 = 覆盖，不是新增一条
        lens.save(key=key, label="测试用（改过）", kind="tone", hint="", prompt="你现在要更简短。")
        assert lens.find(key).label == "测试用（改过）"
        assert lens.find(key).prompt == "你现在要更简短。"
        assert len([row for row in lens.catalog() if row["key"] == key]) == 1
    finally:
        assert lens.delete(key) is True
    assert lens.find(key) is None, "删掉之后清单里就不该有它了"
    assert lens.delete(key) is False, "再删一次是 404 那种情况，不是崩溃"


def test_saving_refuses_an_empty_prompt_and_an_unknown_kind() -> None:
    """空正文 / 乱写的形态都该被拒 —— 这类"半条滤镜"进了系统提示只会添乱。"""
    with pytest.raises(ValueError):
        lens.save(label="空的", kind="tone", prompt="   ")
    with pytest.raises(ValueError):
        lens.save(label="", kind="tone", prompt="正文")
    with pytest.raises(ValueError):
        lens.save(label="形态乱写", kind="随便", prompt="正文")


def test_the_three_kinds_coexist_and_each_can_be_taken_off(db_session) -> None:  # noqa: ANN001
    """**挂上的都生效**（用户可以同时装流程 + 口吻 + 纪律），没有"只能有一条"。"""
    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    tail = _say(db_session, conv, "assistant", "回答", root)
    db_session.commit()

    _hang(db_session, conv, root, "feynman")
    _hang(db_session, conv, root, "plain")
    _hang(db_session, conv, root, "no-answer")

    live = lens.resolve(db_session, conv, tail)
    assert [one.key for one in live] == ["feynman", "plain", "no-answer"], "三条一起生效，按形态排"

    _hang(db_session, conv, tail, "plain", on=False)
    live = lens.resolve(db_session, conv, tail)
    assert [one.key for one in live] == ["feynman", "no-answer"], "取下其中一条，其余还在"


def test_a_record_hangs_from_its_node_down(db_session) -> None:  # noqa: ANN001
    """根上挂的，下游每一条都算挂着（沿树继承）；兄弟分支也算 —— 同一个根下面。"""
    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    deep = _say(db_session, conv, "assistant", "回答", root)
    deeper = _say(db_session, conv, "user", "接着问", deep)
    db_session.commit()

    _hang(db_session, conv, root, "feynman")

    assert [one.key for one in lens.resolve(db_session, conv, deeper)] == ["feynman"]
    assert [one.key for one in lens.resolve(db_session, conv, root)] == ["feynman"]


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
    assert [one.key for one in lens.resolve(db_session, conv, tail)] == ["feynman", "plain"], "两条并存"

    _hang(db_session, conv, mid, "feynman", on=False)   # 同一条 key，更近的记录关掉它
    assert [one.key for one in lens.resolve(db_session, conv, tail)] == ["plain"]
    assert [one.key for one in lens.resolve(db_session, conv, root)] == ["feynman"], "上层的还在"


def test_records_from_another_conversation_do_not_leak(db_session) -> None:  # noqa: ANN001
    """按 `cid` 分开：别处那条对话挂的东西，不会跑到这一条里来。"""
    here = _conv(db_session, "这一条")
    there = _conv(db_session, "另一条")
    node_here = _say(db_session, here, "user", "问")
    node_there = _say(db_session, there, "user", "问")
    db_session.commit()

    _hang(db_session, there, node_there, "feynman")
    assert lens.resolve(db_session, here, node_here) == []


def test_records_written_under_the_old_name_still_count(db_session) -> None:  # noqa: ANN001
    """老数据还认：这批记录原先存在 `skills` 这个键上（那时它叫 skill）。

    升级之后"我已经装上的那些"不该凭空消失 —— 读的时候两处都看，写只写 `lens`。
    """
    from app import settings_store

    conv = _conv(db_session)
    root = _say(db_session, conv, "user", "第一问")
    db_session.commit()
    settings_store.put(
        db_session,
        skills=[{"cid": str(conv.id), "mid": str(root.id), "key": "feynman", "on": True}],
    )
    assert [one.key for one in lens.resolve(db_session, conv, root)] == ["feynman"]


def test_the_block_rides_along_in_both_modes() -> None:
    """它进系统提示，而且**极简模式也带** —— 与挂载无关（那是"你要我怎么陪你"）。"""
    note = lens.block([lens.find("费曼"), lens.find("大白话")])
    assert "**费曼**" in note and "**大白话**" in note
    assert "流程" in note and "口吻" in note, "按形态分节"

    full = chat_router.build_prompt({"quiz"}, None, ["get_mastery"], "", note)
    assert note in full and chat_router.LENS_TAIL in full, "末尾那句压过风格要求的提醒要在"

    minimal = chat_router.build_prompt(set(), None, [], "", note)
    assert note in minimal, "极简模式同样带着它"

    assert lens.block([]) == ""
