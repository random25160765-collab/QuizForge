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


def _read(db, name, args, ctx=None):  # noqa: ANN001, ANN202
    """按"查询模式"调一次 `trees` 组的工具（挂载了、只读档）。"""
    return tools.call(db, name, args, ctx or {}, mounts={"trees"}, allow=("read",))


def test_the_trees_group_lists_and_finds_other_conversations(db_session) -> None:  # noqa: ANN001
    """`trees` 那一组：先看有哪些、再按内容找、然后看形状、最后取原文。

    对应的设计是 `docs/对话树.md` §三：**森林靠工具访问，不靠预先建边** ——
    "我上次是不是钻过这个"不是预先建好的链接，是一次**当场查**。
    """
    here = _conv(db_session, "现在这一条")
    _say(db_session, here, "user", "我们现在在聊缓存行")

    other = _conv(db_session, "上次那一条")
    lesson = _say(db_session, other, "assistant", "讲了一段，里面提到 WCET 与 must/may")
    _say(db_session, other, "user", "WCET 全称是什么？", lesson)
    _say(db_session, other, "user", "must 和 may 有什么区别？", lesson)
    db_session.commit()
    ctx = {"conversationId": str(here.id)}

    ok, listed = _read(db_session, "list_conversations", {}, ctx)
    assert ok and not listed.get("error")
    titles = [one["title"] for one in listed["items"]]
    assert "上次那一条" in titles and "现在这一条" in titles
    mine = [one for one in listed["items"] if one["title"] == "现在这一条"][0]
    assert mine["current"] is True, "要标出'你们现在正说着的'是哪一条"

    ok, found = _read(db_session, "search_conversations", {"query": "WCET"}, ctx)
    assert ok and found["items"], "另一个对话里的那句应该搜得到"
    hit = found["items"][0]
    assert hit["conversationId"] == str(other.id)
    assert hit["current"] is False
    assert "WCET" in hit["snippet"]

    ok, seen = _read(db_session, "read_tree", {"conversationId": str(other.id)}, ctx)
    assert ok
    assert "WCET" in tools.output_text(seen), "骨架要能看到'在哪儿下钻过'"

    ok, full = _read(db_session, "read_tree_nodes", {"conversationId": str(other.id), "ids": [lesson.id]}, ctx)
    assert ok
    assert "must/may" in tools.output_text(full), "点名要的是全文，不是预览"


def test_trees_say_so_when_the_conversation_is_not_there(db_session) -> None:  # noqa: ANN001
    """认不出会话就把话说清楚 —— 不猜一条来读。"""
    ok, payload = _read(db_session, "read_tree", {"conversationId": "00000000-0000-0000-0000-000000000000"})
    assert ok and payload.get("error")

    ok, payload = _read(db_session, "read_tree_nodes", {"ids": [1]}, {})
    assert ok and payload.get("error"), "连当前是哪条都不知道时，同样是错误，不是空树"


def test_trees_stay_shut_when_the_group_is_not_mounted(db_session) -> None:  # noqa: ANN001
    """没挂这一组，即使模型把名字报出来也不执行（纵深防御，见 `call`）。"""
    conv = _conv(db_session)
    _say(db_session, conv, "user", "随便一句")
    db_session.commit()

    ok, payload = tools.call(
        db_session, "list_conversations", {}, {"conversationId": str(conv.id)},
        mounts={"notes"}, allow=("read",),
    )
    # 被闸门拦下时 `call` 给的是 `(False, ...)` —— 与"工具自己回了句错误"的 `(True, ...)`
    # 分开：前者是"这个模块这次不在这儿"，后者是"在，但这次没查到"。
    assert not ok, "没挂这一组就不该执行"
    assert "对话树" in str(payload.get("error")), "说清是哪个模块没挂"


def test_timestamps_are_written_in_local_time() -> None:
    """时间戳按**这台机器的本地时间**写，不是 UTC 的墙上时间。

    库里存的是 UTC（`UtcDateTime`），直接 `strftime` 出来就是 UTC —— 在 UTC+8 的机器上
    整整差 8 小时。实测就是从这儿露出来的：模型照着仪表盘说"04:52 开工"，而真实时间是
    中午 12:52（用户："我不会在凌晨学习，肯定数据错了。我记得那天是下午，要么就是晚上"）。
    """
    from datetime import UTC, datetime, timedelta

    at = datetime(2026, 9, 21, 4, 52, tzinfo=UTC)
    assert recursion.stamp(at) == at.astimezone().strftime("%m-%d %H:%M")
    # 本机就是 UTC 时两者当然一样；别的时区必须真的跟着变（否则这条测试是空转的）
    if datetime.now().astimezone().utcoffset() != timedelta(0):
        assert recursion.stamp(at) != "09-21 04:52", "没有被换算成本地时间"


def test_dashboard_reads_the_conversation_it_was_given(db_session) -> None:  # noqa: ANN001
    """仪表盘读的是**交到手上的这条会话**，不是"最近那条"。

    它原先是个工具，靠 `ctx` 里的 `conversationId` 认会话；现在由 `_stream` 直接
    把 `conv` 递进来（用户："对话轨迹是 llm 长期可见的用户信息仪表盘"）。
    """
    conv = _conv(db_session)
    _say(db_session, conv, "user", "开始学缓存")
    db_session.commit()

    text = recursion.dashboard(db_session, conv)
    assert "## 他的学习轨迹" in text
    assert "开始学缓存" in text


def test_dashboard_is_quiet_on_an_empty_conversation(db_session) -> None:  # noqa: ANN001
    """还没有消息时给空串（而不是一段"暂无数据"的废话）：每轮都要背的东西，能省就省。"""
    conv = _conv(db_session)
    db_session.commit()
    assert recursion.dashboard(db_session, conv) == ""


def test_dashboard_keeps_long_messages_as_previews(db_session) -> None:  # noqa: ANN001
    """长消息只进预览 —— 它是**每轮**都要背的，塞全文等于每轮都拖着一篇文章走。"""
    conv = _conv(db_session)
    long_text = "这句话很长，" * 40
    _say(db_session, conv, "user", long_text)
    db_session.commit()

    text = recursion.dashboard(db_session, conv)
    assert "这句话很长" in text
    assert long_text not in text, "骨架里不该出现全文"
    assert "…" in text, "截断要有记号"


def test_read_nodes_still_gives_full_text(db_session) -> None:  # noqa: ANN001
    """骨架只给预览；要逐字看的按 id 点名取（两段式的前半段没了，后半段留着 ——
    留给"把某一支带进这轮上下文"那个旋钮，见 docs/对话树.md §七）。"""
    conv = _conv(db_session)
    long_text = "这句话很长，" * 20
    msg = _say(db_session, conv, "user", long_text)
    db_session.commit()

    assert long_text in recursion.read_nodes(db_session, conv, [msg.id])


def test_dashboard_reaches_the_prompt_as_text(db_session) -> None:  # noqa: ANN001
    """它进的是**系统提示**，而且是原样文本。

    两件事都要守住：① 仪表盘不是工具，所以**极简模式也带**（极简只是没有工具，
    不是不认识这位用户）；② 缩进就是它的意思，别被 JSON 转义掉。
    """
    from app.routers import chat as chat_router

    conv = _conv(db_session)
    lesson = _say(db_session, conv, "assistant", "讲解")
    _say(db_session, conv, "user", "A 是什么？", lesson)
    _say(db_session, conv, "user", "B 是什么？", lesson)
    db_session.commit()
    trail = recursion.dashboard(db_session, conv)

    with_tools = chat_router.build_prompt({"quiz"}, None, ["get_mastery"], trail)
    assert trail in with_tools
    assert "\\n" not in trail, "没有被 JSON 转义（检查的是字面反斜杠 n）"

    minimal = chat_router.build_prompt(set(), None, [], trail)
    assert trail in minimal, "极简模式同样带着仪表盘"
