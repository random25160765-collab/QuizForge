"""对话树的读法（`app/recursion.py`）与那个工具。

这批测试盯的是**「递归学习法」的形状能不能被读出来**，以及一件更要紧的事：
**两种长得一样、含义相反的分叉不能被混成一个**。

（那个混法是有来历的：读第一遍真实会话时，我把"同一句话重发"读成了"对回答不满意"
—— 而那次的上下文是**联网没配、AI 回了个空**，用户重发了一次而已。把"重试"读成
"差评"，整份复盘就全错了。所以这条必须有测试钉住。）
"""

from __future__ import annotations

from app import recursion, tools
from app.models import Conversation, Message


def _conv(db, title="复盘测试"):  # noqa: ANN001
    conv = Conversation(title=title)
    db.add(conv)
    db.flush()
    return conv


def _say(db, conv, role, text, parent=None):  # noqa: ANN001
    """挂一条消息。`parent` 收的是**上一条消息对象**（不是 id）—— 树就是这么长出来的。"""
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


def test_two_kinds_of_forks_are_told_apart(db_session) -> None:  # noqa: ANN001
    """**同一句话重发**和**从一段讲解里挑出多个概念**，必须分开。

    两者结构一模一样（同一父节点下的两个 user 兄弟），含义却相反：前者是
    "我上一句没说清 / 上一次它没答到"，后者才是递归学习法的下钻动作。
    """
    conv = _conv(db_session)
    lesson = _say(db_session, conv, "assistant", "讲了一段，里面提到 A、B 两个词")
    first = _say(db_session, conv, "user", "用联网搜索帮我查一下 Tenstorrent", lesson)
    again = _say(db_session, conv, "user", "用联网搜索帮我查一下 Tenstorrent", lesson)
    other = _say(db_session, conv, "user", "WCET 全称是什么？", lesson)
    db_session.commit()

    forks = recursion.forks(recursion.tree(db_session, conv))
    assert len(forks) == 1
    assert forks[0]["reask"] == {again.id}, "原样重发的那条要认出来（这里存的是 id）"
    assert {k.id for k in forks[0]["fresh"]} == {first.id, other.id}, "另两个是各自的新概念"


def test_same_question_is_conservative_on_short_ones() -> None:
    """**短句差一个字就不是同一个问题** —— 这是递归学习法最典型的问法。

    `A 是什么？` 与 `B 是什么？` 压掉标点后相似度 0.86，可它们是两个不同的概念。
    按相似度一刀切会把真实的下钻判成"重发"，学习路径上就少了一支 —— 那是丢信号。
    所以短句只认"原样或包含"，宁可把重发漏判成一次下钻（多一条，无害）。
    """
    # 只差标点空格 → 是同一句
    assert recursion.same_question("WCET全称是什么？", "WCET 全称是什么")
    # 同一句后面补了一段 → 是同一句（真实的"我又想起来一句"）
    assert recursion.same_question("信息凑齐了", "信息凑齐了 另外你现在有联网能力了")
    # **换了个概念 → 不是同一句**（哪怕字面很像）
    assert not recursion.same_question("A 是什么？", "B 是什么？")
    assert not recursion.same_question("carveout是什么意思？", "这个bank是什么？")
    # 长句改写 → 是同一句
    assert recursion.same_question(
        "唉，你说的东西里面好多我不会的知识点 要是能一边聊天一边建知识图谱那就好了，这个软件有缺陷",
        "唉，你说的东西里面好多我不会的知识点 要是能一边聊天一边建知识图谱那就好了。"
        "我是这个软件的开发者和第一个使用者，能给我提点建议吗",
    )


def test_indent_only_deepens_at_real_forks(db_session) -> None:  # noqa: ANN001
    """单传的一层**不加深缩进** —— 缩进只在真分叉处才有意义。

    实测代价：真实那条会话最深 45 层，每层缩两格就是 90 个空格，骨架因此从两千多字
    涨到九千多，而那些缩进**没有区分出任何东西**（那一层只有一个孩子）。
    真实深度照样要算对（`depth`），只是不再拿它当缩进用。
    """
    conv = _conv(db_session)
    parent = None
    for index in range(12):  # 一条 12 层的直线链
        parent = _say(
            db_session, conv,
            "user" if index % 2 == 0 else "assistant",
            "第 %d 层" % index, parent,
        )
    db_session.commit()

    rows = recursion._depth_and_order(recursion.tree(db_session, conv))
    assert max(r["depth"] for r in rows) == 11, "真实深度要算对"
    assert max(r["indent"] for r in rows) == 0, "一路单传，不该有任何缩进"


def test_each_branch_reports_how_deep_it_went(db_session) -> None:  # noqa: ANN001
    """每支要标出它往下钻了几层 —— 那是"这个概念有多难"最直接的数。"""
    conv = _conv(db_session)
    lesson = _say(db_session, conv, "assistant", "讲解：里面冒出了两个词")
    _say(db_session, conv, "user", "浅的这个词是什么？", lesson)
    deep = _say(db_session, conv, "user", "深的那个词是什么？", lesson)
    # 给"深的"那一支挂三层
    node = deep
    for index in range(3):
        node = _say(db_session, conv, "assistant" if index == 0 else "user",
                    "深入第 %d 层" % index, node)
    db_session.commit()

    text = recursion.outline(db_session, conv)
    assert "这一支钻了 3 层" in text
    assert text.count("这一支钻了 0 层") == 0, "叶子不该标 0"


def test_outline_counts_drills_and_reasks_separately(db_session) -> None:  # noqa: ANN001
    """小结里"下了几次钻"和"重发了几次"是两个数，不能混成一个"分叉数"。"""
    conv = _conv(db_session)
    lesson = _say(db_session, conv, "assistant", "讲解")
    _say(db_session, conv, "user", "A 是什么？", lesson)
    _say(db_session, conv, "user", "B 是什么？", lesson)
    _say(db_session, conv, "user", "B 是什么？", lesson)  # 重发
    db_session.commit()

    stat = recursion.brief(db_session, conv)
    assert stat["drills"] == 2, "两个新概念"
    assert stat["reasks"] == 1, "一次重发"


def test_empty_conversation_is_not_an_error(db_session) -> None:  # noqa: ANN001
    conv = _conv(db_session)
    db_session.commit()
    assert "还没有消息" in recursion.outline(db_session, conv)


def test_tool_reads_the_conversation_it_was_told_about(db_session) -> None:  # noqa: ANN001
    """工具靠 `ctx` 里的 `conversationId` 认会话（主对话每轮都会带上它）。"""
    conv = _conv(db_session)
    _say(db_session, conv, "user", "开始学缓存")
    db_session.commit()

    ok, payload = tools.call(
        db_session, "read_learning_tree", {}, {"conversationId": str(conv.id)}
    )
    assert ok and not payload.get("error")
    assert "## 树的形状" in payload["tree"]
    assert "开始学缓存" in payload["tree"]


def test_tool_says_so_when_there_is_no_conversation(db_session) -> None:  # noqa: ANN001
    """没带会话时**说清楚**，不要猜一条来读。"""
    ok, payload = tools.call(db_session, "read_learning_tree", {}, {})
    assert ok and payload.get("error")

    ok, payload = tools.call(
        db_session, "read_learning_tree",
        {}, {"conversationId": "00000000-0000-0000-0000-000000000000"},
    )
    assert ok and payload.get("error"), "不存在的会话同样是错误，不是空树"


def test_tool_can_read_named_messages_in_full(db_session) -> None:  # noqa: ANN001
    """骨架只给预览；要逐字看的按 id 点名取（两段式）。"""
    conv = _conv(db_session)
    long_text = "这句话很长，" * 20
    msg = _say(db_session, conv, "user", long_text)
    db_session.commit()

    ok, payload = tools.call(
        db_session, "read_learning_tree",
        {"ids": [msg.id]}, {"conversationId": str(conv.id)},
    )
    assert ok and long_text in payload["tree"], "点名要的是全文，不是预览"


def test_the_tree_reaches_the_model_as_text_not_json(db_session) -> None:  # noqa: ANN001
    """树交到模型手上必须是**原样文本**：它的缩进就是它的意思。

    塞进 JSON 会把换行与缩进转义成 `\\n` 和字面空格，整棵树塌成一行 ——
    那正好把它唯一的价值（形状）毁掉。
    """
    conv = _conv(db_session)
    lesson = _say(db_session, conv, "assistant", "讲解")
    _say(db_session, conv, "user", "A 是什么？", lesson)
    _say(db_session, conv, "user", "B 是什么？", lesson)
    db_session.commit()

    _, payload = tools.call(
        db_session, "read_learning_tree", {}, {"conversationId": str(conv.id)}
    )
    text = tools.output_text(payload)
    assert "\n#" in text, "骨架的行还在"
    assert "\\n" not in text, "没有被 JSON 转义"
