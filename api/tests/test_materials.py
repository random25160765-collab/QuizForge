"""材料正文层：按坐标回读 + 字面检索（三路召回的规则路）。

刻意**不**依赖真实那 71M 材料目录：每个用例自己写一份小文件，材料行指向它。
这些用例要证明的是"行号对不对、缺文件说不说清、命中判得准不准"，
跟材料是 tt-metal 还是别的东西无关 —— 绑上真实目录只会让测试换个环境就红。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app import materials as material_text
from app import tools
from app.models import Material

PW = "password-1234"


def _register(client, *args, **kwargs) -> str:  # noqa: ANN001
    """不再需要做什么 —— 账号系统已整体拆除（2026-09-22，见 `app/models.py` 顶部）。

    保留这个空函数只是为了不动几十个调用点：它在用例里当"开工准备"用，
    而现在没有任何准备工作要做（数据隔离由 conftest 的 autouse fixture 负责）。
    """
    return ""


def _material(db, path: Path, *, lines: int, depth: str = "出题") -> Material:  # noqa: ANN001
    material = Material(
        slug="tt-metal-text-" + uuid.uuid4().hex[:6],
        subject="tt-metal",
        title="正文测试材料",
        source_path=str(path),
        sha256="0" * 64,
        lines=lines,
        # 默认这一档是"我在学的" —— `search_material` 只看它（另一档归 `search_library`）。
        # 夹具不写这一句的话，模型的默认值是 `检索`，那个工具会一条都搜不到。
        depth=depth,
    )
    db.add(material)
    db.flush()
    return material


def _write(path: Path, body: list[str]) -> Path:
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 按坐标回读


def test_read_lines_returns_exactly_that_range(db_session, tmp_path) -> None:  # noqa: ANN001
    """读回来必须是**那几行**，行号也对得上 —— 引用的全部价值在这一条上。"""
    body = ["第 " + str(n) + " 行" for n in range(1, 31)]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=30)

    lines = material_text.read_lines(material, 10, 12)
    assert [(item["line"], item["text"]) for item in lines] == [
        (10, "第 10 行"),
        (11, "第 11 行"),
        (12, "第 12 行"),
    ]


def test_read_lines_clamps_and_caps(db_session, tmp_path) -> None:  # noqa: ANN001
    """越界夹到文件范围内，单次读取有上限（免得一次把整份材料拖进上下文）。"""
    body = ["第 " + str(n) + " 行" for n in range(1, 31)]
    small = _material(db_session, _write(tmp_path / "small.md", body), lines=30)
    assert material_text.read_lines(small, 28, 999)[-1]["line"] == 30

    big_body = ["行 " + str(n) for n in range(1, 1001)]
    big = _material(db_session, _write(tmp_path / "big.md", big_body), lines=1000)
    lines = material_text.read_lines(big, 1, 9999)
    assert len(lines) == material_text.MAX_LINES_PER_READ


def test_a_missing_file_says_so(db_session, tmp_path) -> None:  # noqa: ANN001
    """库只存坐标、正文在文件里 —— 文件不在就要说清，而不是给一段空文本。"""
    material = _material(db_session, tmp_path / "gone.md", lines=10)

    with pytest.raises(material_text.MaterialError) as info:
        material_text.read_lines(material, 1, 5)
    assert "文件不在这台机器上" in str(info.value)

    # 检索遇到读不到的材料是跳过，不是炸掉整次检索
    result = material_text.search(db_session, "随便一个词", slug=material.slug)
    assert result["hits"] == [] and result["scanned"] == 0
    assert material.slug in result["skipped"]


# ------------------------------------------------------------------ 字面检索


def test_search_locates_the_line_and_reports_coverage(db_session, tmp_path) -> None:  # noqa: ANN001
    """命中行要准、要带上下文、要把"扫了多少"说清（没扫完 ≠ 全库没有）。"""
    body = [
        "# 标题",
        "",
        "circular buffer 是核间通信的队列。",
        "生产者写、消费者读。",
        "无关的一行。",
        "circular buffer 的容量固定。",
    ]
    material = _material(db_session, _write(tmp_path / "s.md", body), lines=len(body))

    result = material_text.search(db_session, "circular buffer", slug=material.slug)
    assert result["scanned"] == 1 and result["total"] == 1

    hit = result["hits"][0]
    assert hit["matched"] == 2, "两行都命中"
    assert hit["firstMatch"] == 3
    assert hit["quote"] == body[2], "引文就是第一条命中行"
    assert hit["startLine"] <= 3 <= hit["endLine"]
    assert "3: circular buffer 是核间通信的队列。" in hit["text"]
    # 上下文里带着紧邻的命中行（第 6 行与第 3 行相距不远，合并成一段）
    assert hit["endLine"] >= 6


def test_search_requires_all_terms_on_one_line(db_session, tmp_path) -> None:  # noqa: ANN001
    """词之间是"与"：不同行出现不算命中 —— 否则"地址"这种词会把整份文档捞出来。"""
    body = ["circular buffer 的队列。", "消费者只管读。"]
    material = _material(db_session, _write(tmp_path / "a.md", body), lines=2)

    assert material_text.search(db_session, "circular buffer", slug=material.slug)["hits"]
    assert material_text.search(db_session, "circular 消费者", slug=material.slug)["hits"] == []
    assert "error" in material_text.search(db_session, "   ")


def test_search_stops_at_the_scan_limit(db_session, tmp_path) -> None:  # noqa: ANN001
    """一次调用限制扫描的材料数，并**如实说明**还有多少没扫。"""
    for index in range(3):
        _material(
            db_session,
            _write(tmp_path / f"many-{index}.md", ["共享的词 出现在这里", "第二行"]),
            lines=2,
        )

    result = material_text.search(db_session, "共享的词", max_materials=1)
    assert result["scanned"] == 1 and result["total"] >= 3
    assert result["skipped"] and "没扫" in result["note"]


# ------------------------------------------------------------------ 两个工具


def test_search_material_tool_returns_sources(db_session, tmp_path) -> None:  # noqa: ANN001
    """工具结果要带 `sources` —— router 靠它长引用，零散改动就免了。"""
    body = ["前一行", "exp_approx_mode 控制指数近似", "后一行"]
    _material(db_session, _write(tmp_path / "t.md", body), lines=3)

    ok, payload = tools.call(db_session, "search_material", {"query": "exp_approx_mode"})
    assert ok, payload
    hit = payload["hits"][0]
    assert hit["firstMatch"] == 2

    source = payload["sources"][0]
    assert source["startLine"] == 2
    assert source["quote"] == body[1]
    assert source["material"] == hit["material"]
    # 给模型的那份要短：整段原文属于 read_material 的活
    assert len(tools.output_text(payload)) < 2000


def test_read_material_tool_reads_by_line(db_session, tmp_path) -> None:  # noqa: ANN001
    """模型读到的就是那几行 —— 它看到的与用户点开引用看到的必须是同一份。"""
    body = ["第 " + str(n) + " 行" for n in range(1, 21)]
    material = _material(db_session, _write(tmp_path / "r.md", body), lines=20)

    ok, payload = tools.call(
        db_session, "read_material", {"slug": material.slug, "startLine": 5, "endLine": 7}
    )
    assert ok, payload
    assert payload["startLine"] == 5 and payload["endLine"] == 7 and payload["lines"] == 3
    assert "6: 第 6 行" in payload["text"]
    assert payload["sources"][0]["startLine"] == 5

    ok, payload = tools.call(db_session, "read_material", {"slug": "no-such-material"})
    assert ok and "error" in payload


# ------------------------------------------------------------------ 读接口


def test_material_lines_endpoint(client, db_session, tmp_path) -> None:  # noqa: ANN001
    """前端点开引用走的是这个接口 —— 与模型读的是同一个函数。"""
    _register(client)
    body = ["第一行", "第二行", "第三行"]
    material = _material(db_session, _write(tmp_path / "e.md", body), lines=3)
    db_session.commit()

    response = client.get(f"/api/knowledge/material?slug={material.slug}&start=2&end=3")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [(item["line"], item["text"]) for item in data["lines"]] == [
        (2, "第二行"),
        (3, "第三行"),
    ]
    assert data["sourcePath"] == str(material.source_path)

    assert client.get("/api/knowledge/material?slug=nope").status_code == 404

    # 文件不在：说清原因（503），而不是给一段空文本
    gone = _material(db_session, tmp_path / "gone.md", lines=3)
    db_session.commit()
    response = client.get(f"/api/knowledge/material?slug={gone.slug}")
    assert response.status_code == 503
    assert "文件不在这台机器上" in response.json()["detail"]


# ------------------------------------------------------------------ 两个书架
#
# `materials.depth` 是**按件**的，不是按来源的：`出题` = 我在学的（值得精读与出题），
# `检索` = 我查的（论文 / 白皮书这类"看看就好"的）。两条工具各看一档，
# **正文与检索方式完全一样，差别只有这一个字段**。


def test_the_two_tools_look_at_different_shelves(db_session, tmp_path) -> None:  # noqa: ANN001
    """`search_material` 只看 `出题`、`search_library` 只看 `检索`。

    造两份**一模一样**的材料（同样的正文、同样的词），只差 `depth` ——
    这样任何一条工具能搜到另一档，都是错的。

    断言用**归属**而不是"精确列表"：隔离夹具不清 `materials`（设计如此 ——
    "题库与知识空间用例之间共享"），别的用例留下的材料也可能命中同一个词。
    """
    body = ["circular buffer 是核间通信的队列"]
    on_chain = _material(db_session, _write(tmp_path / "a.md", body), lines=1, depth="出题")
    for_reference = _material(db_session, _write(tmp_path / "b.md", body), lines=1, depth="检索")
    db_session.commit()

    ok, payload = tools.call(db_session, "search_material", {"query": "circular buffer"})
    assert ok, payload
    found = {hit["material"] for hit in payload["hits"]}
    assert on_chain.slug in found, "出题档的材料没被 search_material 找到"
    assert for_reference.slug not in found, "search_material 读到了检索档 —— 两个书架串了"

    ok, payload = tools.call(db_session, "search_library", {"query": "circular buffer"})
    assert ok, payload
    found = {hit["material"] for hit in payload["hits"]}
    assert for_reference.slug in found, "检索档的资料没被 search_library 找到"
    assert on_chain.slug not in found, "search_library 读到了出题档 —— 两个书架串了"


def test_an_empty_shelf_is_not_reported_as_a_missing_file(db_session, tmp_path) -> None:  # noqa: ANN001
    """筛空 = 空结果，**不是**「这份材料不存在」。

    踩过：`materials.search` 那条"一份都没选到"的分支无条件报
    `没有这份材料：`（而 slug 还是空的）—— 按 `depth` 筛之后它被触发到了，
    模型会以为自己问错了名字，而不是"换另一个书架再问一次"。
    """
    body = ["只有检索档里有这一个词 unique_on_theothershelf"]
    _material(db_session, _write(tmp_path / "only-ref.md", body), lines=1, depth="检索")
    db_session.commit()

    ok, payload = tools.call(db_session, "search_material", {"query": "unique_on_theothershelf"})
    assert ok, payload
    assert payload.get("hits") == []
    assert "error" not in payload, payload
