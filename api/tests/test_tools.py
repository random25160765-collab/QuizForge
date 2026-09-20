"""工具表、消息零件、上下文预算、以及流式协议里最容易写错的一处。

这些都不是"端点通不通"的测试，而是**契约**测试：

* 工具永远不抛（失败也是一条结果交给模型）
* 零件是真相、正文是投影（旧消息没有零件时要兜得住）
* 预算从**最新**往前算（正在聊的那几句必须在）
* 工具调用在 SSE 流里是**分片**来的，必须按 index 攒起来
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import select

from app import agent_loop, parts, tools
from app.ai_gateway import stream_completion
from app.models import (
    Concept,
    ConceptEdge,
    Conversation,
    KnowledgePoint,
    Material,
    Message,
    PointSource,
    Question,
    QuestionPoint,
    Record,
    User,
)

PASSWORD = "password-1234"


# ------------------------------------------------------------------ 脚手架


def _register(client) -> None:  # noqa: ANN001
    """本机用户就绪（单用户本地形态没有"注册"这回事）。见 `app/deps.py`。"""
    from conftest import local_user_id

    local_user_id()


def _me_id(client) -> str:  # noqa: ANN001
    """本机用户的 id。"""
    from conftest import local_user_id

    return local_user_id()


def _seed_knowledge(db, question_id: str | None) -> tuple[int, str]:  # noqa: ANN001
    """测试库里有题库、没有知识空间（那些在开发库里），所以造最小的一份。

    刻意连着 `point_sources` 与题目的挂接一起造：工具查的就是这两条关联，
    只造一半的话测试会"过"，但过的是错的那半。

    返回 (点 id, 点的 key)。key 每次都不同 —— 测试库是**整个会话共用**的，
    用固定 key 会让前一个用例造的点也落进同一桶里（实测就红过）。
    """
    key = "tt-metal-cb-" + uuid.uuid4().hex[:8]
    material = Material(
        slug="tt-metal-test-" + uuid.uuid4().hex[:6],
        subject="tt-metal",
        title="测试材料",
        source_path="/tmp/does-not-matter.md",
        sha256="0" * 64,
        lines=120,
    )
    db.add(material)
    db.flush()

    concept = Concept(
        key="concept-" + uuid.uuid4().hex[:8],
        name="Circular buffer",
        kind="noun",
        definition="核间通信的运行时队列，生产者写、消费者读。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()

    point = KnowledgePoint(
        material_id=material.id,
        concept_id=concept.id,
        key=key,
        name="Circular buffer 与生产者消费者",
        kind="noun",
        layers=["识记", "理解"],
        thickness=2,
    )
    db.add(point)
    db.flush()
    db.add(PointSource(point_id=point.id, slice_id="sl-001", start_line=10, end_line=22))
    if question_id:
        db.add(QuestionPoint(question_id=question_id, point_id=point.id, is_primary=True))
    db.commit()
    return point.id, key


def _first_question_id(db) -> str:  # noqa: ANN001
    """挑一道**推得动**的题（题卡支持的那几类），而且每次都挑同一道。

    为什么不能只写 `limit(1)`：`push_question` 只推 `CARD_TYPES` 里的四种
    （大题走另一条路径），而库里那 34 道大题照样会被 `limit(1)` 挑中 ——
    挑中时卡片是空的，`test_push_question_*` 那两条于是"单独跑必过、
    整套跑偶尔红"（查询计划一变，"第一道"就换人，像是产品在抖）。
    把契约写死：排序确定 + 只要推得动的类型。
    """
    return db.scalar(
        select(Question.id)
        .where(Question.retired_at.is_(None), Question.type.in_(tools.CARD_TYPES))
        .order_by(Question.id)
        .limit(1)
    )


# ------------------------------------------------------------------ 工具


def test_search_knowledge_finds_concept_and_point(client, db_session) -> None:  # noqa: ANN001
    _register(client)
    _, key = _seed_knowledge(db_session, None)

    ok, payload = tools.call(db_session, db_session.get(User, uuid.UUID(_me_id(client))), "search_knowledge", {"query": "circular"})
    assert ok, payload
    assert [item["key"] for item in payload["concepts"]], "概念层要能搜到"
    assert key in [item["key"] for item in payload["points"]]
    assert any(item["material"].startswith("tt-metal-test-") for item in payload["points"])


def test_point_detail_carries_line_ranges(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """出处必须带行区间 —— 那是"引用可点回原文"的全部依据。"""
    _register(client)
    question_id = _first_question_id(db_session)
    assert question_id, "要有题才测得了挂接"
    _, key = _seed_knowledge(db_session, question_id)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, detail = tools.call(db_session, user, "get_point_detail", {"key": key})
    assert ok, detail
    assert detail["kind_of_node"] == "point"
    assert detail["sources"][0]["startLine"] == 10 and detail["sources"][0]["endLine"] == 22
    assert detail["questions"], "挂着的题要带出来"

    ok, missing = tools.call(db_session, user, "get_point_detail", {"key": "根本没有这个key"})
    assert ok, "查不到也算成功返回，不算异常"
    assert "error" in missing and "similar" in missing, "给线索，而不是干巴巴一句没有"


def test_questions_hide_the_answer_by_default(client, db_session) -> None:  # noqa: ANN001
    """推题时不能剧透；要讲解时模型自己会传 includeAnswer。"""
    _register(client)
    _, key = _seed_knowledge(db_session, _first_question_id(db_session))
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, plain = tools.call(db_session, user, "get_existing_questions", {"pointKey": key})
    assert ok and plain["items"]
    assert "answer" not in plain["items"][0]

    ok, shown = tools.call(
        db_session, user, "get_existing_questions", {"pointKey": key, "includeAnswer": True}
    )
    assert ok and "answer" in shown["items"][0]


def test_mastery_and_due_reviews_read_the_records(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """掌握度与到期复习都从 `records` 算 —— 注意 `streak` 与 `sm2` 在 `patch` 里。"""
    _register(client)
    question_id = _first_question_id(db_session)
    assert question_id, "要有题才有记录"
    _, key = _seed_knowledge(db_session, question_id)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    db_session.add(
        Record(
            user_id=user.id,
            question_id=question_id,
            attempts=3,
            correct=2,
            wrong=1,
            mastered=False,
            flagged=False,
            last_at=now - 86400000,
            first_at=now - 3 * 86400000,
            client_rev=1,
            partial=0,
            last_status="correct",
            last_score=1.0,
            patch={
                "streak": 2,
                "sm2": {"due": now - 1000, "ef": 2.5, "reps": 1, "interval": 1, "lastAt": now - 100000},
            },
        )
    )
    db_session.commit()

    ok, mastery = tools.call(db_session, user, "get_mastery", {"pointKeys": [key]})
    assert ok, mastery
    item = mastery["items"][0]
    assert item["attempts"] == 3 and item["answered"] == 1, "只算挂在这条点上的题"
    assert item["band"] != "new" and 0 < item["score"] <= 100

    ok, due = tools.call(db_session, user, "get_due_reviews", {})
    assert ok, due
    mine = next(item for item in due["items"] if item["questionId"] == question_id)
    assert mine["pointKeys"] == [key]


def test_unknown_tool_returns_an_error_instead_of_raising(client, db_session) -> None:  # noqa: ANN001
    """工具内部出错不能中断这一轮 —— 交给模型一条结果，它可以换个问法。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "nope", {})
    assert ok is False and "没有这个工具" in payload["error"]

    ok, payload = tools.call(db_session, user, "search_knowledge", {})
    assert ok and payload["error"] == "query 不能为空"

    ok, payload = tools.call(db_session, user, "get_point_detail", {"key": "  "})
    assert ok and "不能为空" in payload["error"]


def test_push_question_returns_a_card_without_any_answers(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """题卡里一个答案字段都不能有。

    它既进模型上下文、又会被渲染出来 —— 答案摆在眼前就等于没题可做了。
    所以这不是"少给点"，是硬规矩：前端判分靠的是它自己那份完整题库。
    """
    _register(client)
    question_id = _first_question_id(db_session)
    assert question_id, "要有题才推得出来"
    _, key = _seed_knowledge(db_session, question_id)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "push_question", {"pointKey": key})
    assert ok, payload
    card = payload["card"]
    assert card and card["questionId"] == question_id
    assert card["stem"] and card["type"] in tools.CARD_TYPES

    for forbidden in ("answer", "explanation", "rubric", "reference", "verify_ranges"):
        assert forbidden not in card, "卡里不该有字段：" + forbidden
    assert '"answer"' not in json.dumps(card, ensure_ascii=False)

    if "options" in card:
        assert all(set(item) <= {"key", "text"} for item in card["options"])


def test_push_question_skips_what_he_already_got_right(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """推一道他已经答对的题没有意义。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    question_id = _first_question_id(db_session)
    _, key = _seed_knowledge(db_session, question_id)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    assert tools.call(db_session, user, "push_question", {"pointKey": key})[1]["card"], "先确认能推"

    db_session.add(
        Record(
            user_id=user.id,
            question_id=question_id,
            attempts=2,
            correct=2,
            wrong=0,
            mastered=True,
            flagged=False,
            last_at=now,
            first_at=now,
            client_rev=1,
            partial=0,
            last_status="correct",
            last_score=1.0,
            patch={},
        )
    )
    db_session.commit()

    ok, payload = tools.call(db_session, user, "push_question", {"pointKey": key})
    assert ok and payload["card"] is None, "这个点上只有这一道题，答对了就不该再推"


def test_pushed_ids_read_cards_out_of_the_conversation(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """服务端看得见"这次对话已经推过哪几道" —— 模型看不见（卡片按设计不回放进上下文）。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    conv = Conversation(user_id=user.id, title="推题测试")
    db_session.add(conv)
    db_session.flush()
    db_session.add(
        Message(
            conversation_id=conv.id,
            user_id=user.id,
            role="assistant",
            status="ok",
            parts=[{"type": "card", "kind": "question", "payload": {"questionId": "tt-arch-0001"}}],
        )
    )
    db_session.commit()

    assert tools._pushed_ids(db_session, {"conversationId": str(conv.id)}) == {"tt-arch-0001"}
    assert tools._pushed_ids(db_session, {}) == set(), "没有会话上下文时不该乱猜"


def test_tool_output_for_the_model_does_not_echo_the_card(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """给模型的工具结果里不该有整份题卡。

    它是给**界面**渲染的：题面与四个选项誊进上下文（还会随历史重放）
    实测把第二轮请求拖到 91 秒、撞上超时。模型只需要知道"推了哪道题"。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "push_question", {})
    assert ok and payload["card"]
    text = tools.output_text(payload)
    assert payload["card"]["questionId"] in text, "题号要留着"
    assert payload["card"]["stem"] not in text, "题面不该誊一份给模型"
    assert len(text) < 400, "留给模型的那份要短：实测 " + str(len(text))


def test_proposals_never_write_anything(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """凭条只是凭条：工具跑完，记录里必须一个字节都没变。

    这是这一层的全部承诺 —— AI 可以提建议，但按下去的那一下得是用户自己。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    question_id = _first_question_id(db_session)
    assert question_id

    want = {"flag_question": "flag", "mark_mastered": "mastered"}
    for name, kind in want.items():
        ok, payload = tools.call(db_session, user, name, {"questionId": question_id})
        assert ok, payload
        assert payload["proposal"]["kind"] == kind
        assert payload["proposal"]["questionId"] == question_id
        assert payload["proposal"]["current"] is False, "先说清现在是什么状态"
        assert payload["proposal"]["on"] is True

    # 谁都没写：这条断言是这一层的守门人
    db_session.expire_all()
    assert db_session.scalars(select(Record).where(Record.user_id == user.id)).all() == []


def test_proposal_for_an_unknown_question_says_so(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """题号不存在就说清楚，别造一张没有对象的凭条。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "flag_question", {"questionId": "tt-none-9999"})
    assert ok and "error" in payload and "proposal" not in payload

    ok, payload = tools.call(db_session, user, "mark_mastered", {})
    assert ok and "error" in payload


# ------------------------------------------------------------------ 跑 Python


def test_run_python_hands_the_code_to_the_resident_shell(client, db_session) -> None:  # noqa: ANN001
    """模型只交 Python，样板（加载运行时、接 stdout、回传）由服务端负责 ——
    而且现在这份样板在**常驻运行壳**里（`tools.shell_page()`），零件只带 runId。

    为什么样板不能让模型每次手写：写错一次的表现是**一片空白**，用户看不出哪里坏了。
    为什么壳要常驻：Pyodide 每次启动都要重来（下载 + 初始化 + import 依赖），
    一段脚本一个壳就是"每跑一次等一秒多"（用户："跑 python 脚本非常慢"）。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(
        db_session,
        user,
        "run_python",
        {"title": "斐波那契", "code": "print([1, 1, 2, 3])", "packages": ["numpy"]},
    )
    assert ok, payload
    demo = payload["demo"]
    assert demo["title"] == "斐波那契"
    assert demo["runId"], "零件要有身份：宿主靠它把输出认领回来"
    assert "html" not in demo, "不再一段脚本一个页面（见 tools.py 的 _PYODIDE_SHELL）"

    # 壳里该有的东西：运行时地址（本机那份优先）、预载、就绪信号、按 runId 执行
    shell = tools.shell_page()
    base = tools.pyodide_base()
    if base:
        assert base in shell
    assert "loadPyodide" in shell and "loadPackage" in shell
    assert "runPythonAsync" in shell, "真执行"
    assert "qfCode" in shell, "收宿主派来的脚本"
    assert "parent.postMessage" in shell and "qfRun" in shell and "qfReady" in shell
    assert "setStderr" in shell, "报错也要显示出来"
    assert "__INDEX__" not in shell and "__PACKAGES__" not in shell, "占位符都该替换掉"
    # 按 import 现装包（pandas / matplotlib 那一拨）——不写 import 的人一个字节都不下
    assert "loadPackagesFromImports" in shell
    # matplotlib 必须走 Agg：Pyodide 默认的 canvas 后端要往页面 DOM 插画布，
    # 而壳是 1×1 隐藏的 —— 实测它连 plt.close("all") 都会抛 AttributeError，
    # 图也就收不上来。Agg 纯内存渲染，savefig 直接出 PNG。
    assert 'matplotlib.use("Agg")' in shell
    # 图由壳收走（脚本不用 savefig），随回传的 images 一起走
    assert "collect_figures" in shell and "images" in shell
    # 预载 = **全部**（用户："所有的 python 包都务必在运行壳里自动静默预加载"）：
    # 壳常驻，这笔钱整个会话只付一次；而"用到才装"的代价是第一次 import 等 3.5 秒 ——
    # 那正是用户说的"这次执行速度有点慢"
    assert set(tools.PRELOAD_PACKAGES) == set(tools.PYODIDE_PACKAGES)
    assert {"numpy", "scipy", "pandas", "matplotlib"} <= set(tools.PRELOAD_PACKAGES)


def test_run_python_page_reports_its_output_back(client, db_session) -> None:  # noqa: ANN001
    """沙箱输出要能回到宿主 —— 模型看不见面板，不回传它就只能编。

    实测的编法：它声称 numpy/scipy 可用，而面板上写着 `No module named 'numpy'`。
    回传之后，前端能把输出摆在面板上，也能一键把它发回给模型。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(
        db_session, user, "run_python", {"code": "print(1 + 1)", "packages": ["numpy"]}
    )
    assert ok, payload
    demo = payload["demo"]
    assert demo["runId"], "要有身份，宿主才认得出这是哪一次运行回传的"

    # 回传这条路现在在常驻壳里：跑完（或报错）都要 send 一次，
    # 否则宿主永远停在"运行中…"——那正是用户报过的样子。
    shell = tools.shell_page()
    assert "parent.postMessage" in shell
    assert "qfRun" in shell
    assert shell.count("send({") >= 3, "就绪、成功、失败三条都要回传"


def test_run_python_loads_the_fixed_subset_and_refuses_others(client, db_session) -> None:  # noqa: ANN001
    """沙箱只支持一个写在代码里的子集（numpy / scipy），而且**默认就装好**。

    原先要模型自己在 `packages` 里点名 —— 它一旦忘了（或点了名字单外的），
    表现就是代码里 `import scipy` 失败，而它以为装好了（实测有过
    「numpy 导入成功、scipy 导入失败」）。所以：名单固定、默认加载、
    名单外的明确剔掉并回话。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    # 壳启动时把**整份名单**装好（用户："所有的 python 包都务必在运行壳里自动静默
    # 预加载"）——pandas / matplotlib 以前是"用到才装"，第一次 import 要等 3.5 秒。
    ok, payload = tools.call(db_session, user, "run_python", {"code": "import scipy"})
    assert ok, payload
    shell = tools.shell_page()
    assert '"numpy"' in shell and '"scipy"' in shell, shell[:300]
    # 精确到预载那一行（壳里别处提到这些名字是正常的：正则、注释）
    assert 'var want = ["numpy", "scipy", "pandas", "matplotlib", "sympy", "networkx"]' in shell, (
        "启动就装全"
    )
    # **lock 里没有的包**走另一条路：`make vendor` 把 wheel 放在同一个目录，
    # 壳里用 micropip 从**本机** URL 装（见 EXTRA_WHEELS）。画电路图的 schemdraw
    # 就是这么来的 —— 它不在 Pyodide 那 310 个包里。
    assert "micropip" in shell.upper() or "micropip" in shell, "壳要用 micropip 装额外包"
    assert "schemdraw" in shell, "额外包名单要注进壳里"
    assert "loadPackage('micropip')" in shell, "micropip 自己也得先装上（实测栽过）"
    assert "sys.version.split()" in shell, "版本要报出来（这样装没装上不用猜）"
    assert "__CHECK__" not in shell
    assert "sys.version.split()" in shell, "版本要报出来（这样装没装上不用猜）"
    assert "__CHECK__" not in shell

    # 点名名单外的（torch 这种要编译的）：剔掉，并说清没装（在**回给模型的那条 note**里）
    ok, payload = tools.call(
        db_session,
        user,
        "run_python",
        {"code": "import torch", "packages": ["torch", "numpy", "scipy.signal"]},
    )
    assert ok, payload
    assert "torch" in payload["note"] and "没有装" in payload["note"]
    assert "scipy.signal" not in payload["note"], "带子模块的写法要归到 scipy，不算越界"
    # 名单里的人**不再被当成外人**：pandas 现在"能用，只是要现装" ——
    # 那句"你要的 X 不在这个子集里"只该对 torch 说
    assert "你要的 torch" in payload["note"]
    assert "你要的 pandas" not in payload["note"]


def test_run_python_refuses_empty_and_giant_code(client, db_session) -> None:  # noqa: ANN001
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "run_python", {"code": "   "})
    assert ok and "error" in payload and "demo" not in payload

    ok, payload = tools.call(db_session, user, "run_python", {"code": "x = 1\n" * tools.CODE_MAX_CHARS})
    assert ok and "error" in payload and "demo" not in payload


# ------------------------------------------------------------------ 演示沙箱


def test_render_demo_refuses_a_giant_html(client, db_session) -> None:  # noqa: ANN001
    """演示会落进零件、每次读会话都要发给前端 —— 它必须小，超了就直接拒。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(
        db_session, user, "render_demo", {"html": "<div>" + "x" * tools.DEMO_MAX_CHARS + "</div>"}
    )
    assert ok and "error" in payload and "demo" not in payload
    assert "上限" in payload["error"]

    ok, payload = tools.call(db_session, user, "render_demo", {"title": "T", "html": "<b>hi</b>"})
    assert ok and payload["demo"]["title"] == "T"
    assert "<b>hi</b>" in payload["demo"]["html"], "模型写的东西要原样留着"


def test_a_question_can_be_written_on_the_spot(client, db_session) -> None:  # noqa: ANN001
    """模型自己出一道题：挂成一张**临时题卡**（默认不进题单）。

    这是"只能从题库里选题"那条限制的解药：真实教学里有一半是"就着刚才这段话编一道"。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(
        db_session,
        user,
        "create_question",
        {
            "pointKey": "tt-arch",
            "question": {
                "type": "single",
                "stem": "circular buffer 的同步靠什么？",
                "options": ["软件锁", "硬件元数据同步", "轮询", "中断"],
                "answer": "b",
                "explanation": "材料 L78：implemented in SRAM、hardware metadata synchronization。",
                "layer": "识记",
            },
        },
    )
    assert ok, payload
    draft = payload["draft"]
    assert draft["id"].startswith("draft-")
    assert draft["answer"] == "B"  # 小写会被归一化
    assert [item["key"] for item in draft["options"]] == ["A", "B", "C", "D"]
    assert "临时题卡" in payload["note"]

    # 给模型看的那一份要被裁过，而且**答案要留着**（它讲评时要用）
    seen = json.loads(tools.output_text(payload))
    assert "draft" in seen and seen["draft"]["answer"] == "B"
    assert "options" not in seen["draft"], "整张卡不该随历史反复重放"


def test_a_draft_question_must_be_answerable(client, db_session) -> None:  # noqa: ANN001
    """写不成题的（没题干、答案不在选项里、大题没小问）当场退回，而不是挂一张空卡。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))

    ok, payload = tools.call(db_session, user, "create_question", {"question": {"stem": " "}})
    assert ok and "题干" in payload["error"]

    ok, payload = tools.call(
        db_session,
        user,
        "create_question",
        {"question": {"stem": "题", "type": "single", "options": ["A", "B"], "answer": "D"}},
    )
    assert ok and "answer" in payload["error"]

    ok, payload = tools.call(
        db_session, user, "create_question", {"question": {"stem": "题", "type": "problem"}}
    )
    assert ok and "小问" in payload["error"]


def test_demo_page_carries_the_kit(client, db_session) -> None:  # noqa: ANN001
    """演示页由服务端注入套件。

    让模型自己引库是"能跑但难看"的一半原因：版本猜错、CDN 挂掉、配色与布局
    每次重新发明一遍。套件注入之后就只剩正文要写了。
    """
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    if not tools._demo_kit_ready():
        pytest.skip("演示套件未同步（make vendor）")

    fragment = '<div id="app"></div>\n<script type="text/babel">QFKit.mount(<h1>hi</h1>);</script>'
    ok, payload = tools.call(db_session, user, "render_demo", {"title": "帽子里放什么", "html": fragment})
    assert ok, payload
    page = payload["demo"]["html"]

    assert page.startswith("<!doctype html>"), "片段要包成完整的一页"
    assert "帽子里放什么" in page
    assert '<div id="app">' in page and "QFKit.mount" in page

    for name in ("tailwind.js", "react.js", "react-dom.js", "d3.js", "babel.js", "qf-kit.js"):
        assert "__ORIGIN__/assets/demo-kit/" + name in page, name
    assert "qf-kit.css" in page

    # 顺序：React 在 kit 之前（kit 要用它）；Babel 必须在模型那段 `text/babel` 之前
    assert page.index("react.js") < page.index("react-dom.js") < page.index("qf-kit.js")
    assert page.index("babel.js") < page.index("QFKit.mount")
    # 路径带占位符：沙箱里相对路径解析不了，绝对地址只能由宿主填（见 pyodide_base）
    assert "__ORIGIN__" in page


def test_demo_page_keeps_a_full_document_intact(client, db_session) -> None:  # noqa: ANN001
    """模型给完整 HTML 时，套件插进 `<head>`，它自己写的东西一个都不能丢。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    if not tools._demo_kit_ready():
        pytest.skip("演示套件未同步（make vendor）")

    full = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        "<title>我自己写的标题</title><style>body{color:red}</style></head>"
        '<body><canvas id="c"></canvas></body></html>'
    )
    ok, payload = tools.call(db_session, user, "render_demo", {"title": "演示", "html": full})
    assert ok, payload
    page = payload["demo"]["html"]

    for keep in ("<title>我自己写的标题</title>", "body{color:red}", '<canvas id="c">'):
        assert keep in page, keep
    assert "__ORIGIN__/assets/demo-kit/react.js" in page
    assert page.index("qf-kit.js") < page.index("<body>"), "套件要在 head 里先就位"


# ------------------------------------------------------------------ 图检索


def _seed_concept(db, name: str) -> Concept:  # noqa: ANN001
    concept = Concept(
        key="concept-" + uuid.uuid4().hex[:8],
        name=name,
        kind="noun",
        definition=name + "的定义。",
        material_count=1,
        point_count=1,
    )
    db.add(concept)
    db.flush()
    return concept


def _link(db, source: Concept, target: Concept, edge_type: str) -> None:  # noqa: ANN001
    db.add(
        ConceptEdge(
            from_concept_id=source.id,
            to_concept_id=target.id,
            type=edge_type,
            why="测试造边",
            derived_by="llm",
            weight=1.0,
        )
    )
    db.flush()


def test_explore_graph_reads_prerequisites_in_the_right_direction(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """`A →requires→ B` 读作「A 是 B 的前置」。方向错了，"该先学什么"就会答成"学完之后学什么"。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    basis = _seed_concept(db_session, "队列基础")
    advanced = _seed_concept(db_session, "核间通信")
    _link(db_session, basis, advanced, "requires")
    db_session.commit()

    ok, payload = tools.call(db_session, user, "explore_graph", {"key": advanced.key})
    assert ok, payload
    assert payload["seed"]["key"] == advanced.key
    [neighbor] = payload["neighbors"]
    assert neighbor["key"] == basis.key
    assert neighbor["relation"] == "前置" and neighbor["edge"] == "requires"
    assert neighbor["band"] == "new", "没答过的点也照样报（'这片是空的'本身就是要说的事）"

    ok, payload = tools.call(db_session, user, "explore_graph", {"key": basis.key})
    assert ok and payload["neighbors"][0]["relation"] == "后继"

    ok, payload = tools.call(db_session, user, "explore_graph", {"query": "核间通信"})
    assert ok and payload["seed"]["key"] == advanced.key, "不记得 key 时按名字也能进去"


def test_explore_graph_walks_two_layers_and_orders_prerequisites_first(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """两层能走通；排序把前置摆最前（那一条上挂着"该先学什么"的答案）。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    first = _seed_concept(db_session, "张量轴约定")
    second = _seed_concept(db_session, "切分策略")
    third = _seed_concept(db_session, "核间流水")
    against = _seed_concept(db_session, "另一条线")
    _link(db_session, first, second, "requires")
    _link(db_session, second, third, "requires")
    _link(db_session, third, against, "contrast_with")
    db_session.commit()

    ok, payload = tools.call(db_session, user, "explore_graph", {"key": third.key, "depth": 2})
    assert ok, payload
    by_key = {item["key"]: item for item in payload["neighbors"]}
    assert second.key in by_key and first.key in by_key, "两层都要到"
    assert by_key[first.key]["distance"] == 2
    assert [item["edge"] for item in payload["neighbors"]][0] == "requires", "前置排最前"
    assert against.key not in by_key, "contrast_with 不在默认视图里（它多半是机械派生的）"

    ok, payload = tools.call(
        db_session, user, "explore_graph", {"key": third.key, "kinds": ["contrast_with"]}
    )
    assert ok and [item["key"] for item in payload["neighbors"]] == [against.key], "要看得显式要"


def test_explore_graph_says_when_there_is_nothing(client, db_session, imported_bank) -> None:  # noqa: ANN001
    """图谱还稀：没有边就直说，而不是假装邻域很全。"""
    _register(client)
    user = db_session.get(User, uuid.UUID(_me_id(client)))
    lonely = _seed_concept(db_session, "孤点")
    db_session.commit()

    ok, payload = tools.call(db_session, user, "explore_graph", {"key": lonely.key})
    assert ok and payload["neighbors"] == []
    assert "前置链" in payload["note"], "要说清是「还没接进前置链」，而不是含糊地说「图谱很稀」"

    ok, payload = tools.call(db_session, user, "explore_graph", {"key": "no-such-key"})
    assert ok and "error" in payload


# ------------------------------------------------------------------ 零件


def test_parts_projection_and_legacy_fallback() -> None:
    ps = [
        parts.think_part("先想想"),
        parts.text_part("答案是"),
        parts.text_part("42"),
        parts.tool_part(name="t", output="x" * (parts.MAX_TOOL_OUTPUT + 50)),
    ]
    assert parts.text_of(ps) == "答案是\n42", "正文投影只取 text 零件"
    assert "已截断" in ps[3]["output"], "工具输出要截断（防一次查询撑爆上下文）"

    assert parts.normalize([], "第一版留下的老消息")[0]["text"] == "第一版留下的老消息"
    assert parts.normalize([{"type": ""}, "垃圾", 42], "兜底")[0]["type"] == "text"


# ------------------------------------------------------------------ 预算


def test_budget_keeps_the_newest_and_stays_quiet_about_it() -> None:
    """按预算从最新往回塞（正在聊的必须在窗口里），但**不告诉模型历史被截过**。

    这条用例原来钉的是相反的契约（"模型要知道历史被截过"）。改了是因为那条告知
    会被模型当自己的话复述出来 —— 用户看到的是一句莫名其妙的报备，原话：
    "直接把这个告知删掉，不要再留"。截断是我们自己的事（`dropped` 照旧返回）。
    """
    history = [
        {"role": "user", "content": "旧" * 5000},
        {"role": "assistant", "content": "旧答"},
        {"role": "user", "content": "新问题"},
    ]
    messages, dropped = agent_loop.build_messages("系统提示", history, 500)
    assert dropped >= 1
    assert messages[-1]["content"] == "新问题", "正在聊的必须在窗口里"
    assert messages[-2]["content"] == "旧答", "从最新往前塞"
    assert messages[0]["content"] == "系统提示", "system 一个字的告知都不加"
    assert "没有带进来" not in messages[0]["content"]
    assert "长度限制" not in messages[0]["content"]


def test_budget_counts_cjk_as_one_token() -> None:
    """中日韩字符按 1 个 token 估 —— 低估会让供应商替我们截断，那更糟。"""
    assert agent_loop.estimate_tokens("中文" * 100) >= 200
    assert agent_loop.estimate_tokens("abcd" * 100) < 200


# ------------------------------------------------------------------ 流协议


def test_stream_assembles_tool_call_fragments() -> None:
    """工具调用在流里是分片来的：name 一块、arguments 几个字符一块。

    忘了按 `index` 攒，参数就会缺一半，而模型只会觉得"工具报错了" ——
    这是 OpenAI 兼容协议最容易写错的一处，所以用一个**真的 HTTP 端点**测，
    而不是打桩。
    """
    chunks = [
        # 真实协议里 name 是**完整一次**给的；有些供应商每个块都重复一遍，
        # 所以这里特意重复一次 —— 忘了"覆盖而不是拼接"就会得到 search_knowledgesearch_knowledge
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "search_knowledge"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search_knowledge"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "cb"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # 静音
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conf = {
            "apiKey": "sk-test",
            "baseUrl": "http://127.0.0.1:%d/v1" % server.server_port,
            "model": "test-model",
            "timeoutMs": 5000,
        }
        events = list(
            stream_completion(
                conf, [{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x"}}]
            )
        )
        calls = [value for kind, value in events if kind == "tool_calls"]
        assert calls, "攒完之后要交一次工具调用"
        assert calls[0][0]["id"] == "call_9"
        assert calls[0][0]["name"] == "search_knowledge", "重复给的 name 不能叠起来"
        assert json.loads(calls[0][0]["arguments"]) == {"query": "cb"}, "分片的 arguments 要拼起来"
        assert ("finish", "tool_calls") in events
    finally:
        server.shutdown()
