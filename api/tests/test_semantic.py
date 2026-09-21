"""向量检索层：打包 / 余弦 / RRF 融合，以及**没有向量时的降级**。

刻意**不依赖任何真实通道**：向量在用例里直接写进库（"假通道"）。要证明的是
**融合与排序的规矩**，不是"某家的 embedding 好不好" —— 绑上真通道只会让用例
变成"今天网通不通"（与 `test_materials` 不绑真实材料目录是同一条理由）。

其中一条用例钉的是用户提的那件真事：

> 技术领域的近义词可多着呢，你也不能要求我每次都原原本本说出原词来啊。

所以这里必须证明：**原词一个都不出现，照样能找对那一片**。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app import materials as material_text
from app import semantic
from app.models import Material, MaterialSlice, SliceEmbedding


def _write(path: Path, body: list[str]) -> Path:
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _material(db, path: Path, *, lines: int) -> Material:  # noqa: ANN001
    """slug 带随机后缀：材料与切片表**不在隔离夹具的清表名单里**，
    所以每个用例自己造一份、并且处处按 slug 过滤，互不打扰。"""
    material = Material(
        slug="sem-vec-" + uuid.uuid4().hex[:6],
        subject="tt-metal",
        title="向量测试材料",
        source_path=str(path),
        sha256="0" * 64,
        lines=lines,
    )
    db.add(material)
    db.flush()
    return material


def _slice(db, material, *, start: int, end: int, summary: str = "", slice_id: str = "sl-001"):  # noqa: ANN001
    row = MaterialSlice(
        material_id=material.id,
        slice_id=slice_id,
        path=[],
        start_line=start,
        end_line=end,
        tokens="",
        figures=[],
        summary=summary,
    )
    db.add(row)
    db.flush()
    return row


def _vector(db, slice_row, vec, *, model: str = "fake-embed", ordinal: int = 1,
            start: int = 0, end: int = 0):  # noqa: ANN001
    """假通道的产物：直接把**一个窗口**的向量写进库。

    **这就是"不依赖 provider"那一半** —— 用例不需要联网、不需要那 600MB 模型，
    要证明的是融合与排序的规矩。
    """
    row = SliceEmbedding(
        slice_id=slice_row.id,
        ordinal=ordinal,
        start_line=start or slice_row.start_line,
        end_line=end or slice_row.end_line,
        model=model,
        dim=len(vec),
        vec=semantic.pack(vec),
        text_hash="f" * 32,
    )
    db.add(row)
    db.flush()
    return row


# ------------------------------------------------------------------ 存与算


def test_pack_unpack_round_trip() -> None:
    """float32 紧凑打包：一个维度 4 字节，往返不丢精度（到 float32 的精度为止）。"""
    vec = [0.1, -0.25, 1.0, 0.0, 3.5]
    blob = semantic.pack(vec)
    assert len(blob) == 4 * len(vec)
    assert semantic.unpack(blob) == pytest.approx(vec)
    assert semantic.unpack(b"") == []


def test_cosine_gives_one_zero_and_minus_one() -> None:
    """余弦的三个基本值，以及两种**不该崩**的输入。"""
    assert semantic.cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert semantic.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert semantic.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # 零向量没有方向：给 0 而不是 NaN，也不抛 —— 它不该让整次检索崩掉
    assert semantic.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    # 维度对不上（换过模型）：当"不可比"，不当错误
    assert semantic.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_rrf_prefers_being_on_both_lists_over_topping_one() -> None:
    """RRF 的意义：**只用名次，不用分数**。

    B 在两条榜上都排第 2；A 在第一条排第 1、第二条排第 3 —— 用分数加权时
    谁赢取决于归一化怎么调，用名次时**两路都认可**就该赢。
    """
    scores = dict(semantic.rrf(["A", "B"], ["C", "B"]))
    assert scores["B"] > scores["A"]
    assert scores["B"] > scores["C"]


def test_rrf_is_reproducible_on_ties() -> None:
    """同分按 key 定序 —— 否则"哪条在前"会随 dict 顺序漂，同一份数据两次不一样。"""
    first = semantic.rrf(["B", "A"], ["A", "B"])
    second = semantic.rrf(["B", "A"], ["A", "B"])
    assert first == second
    assert [key for key, _score in first] == ["A", "B"]


# ------------------------------------------------------------ 语义那一路本身


def test_a_paraphrase_finds_the_slice_where_no_word_matches(db_session, tmp_path) -> None:  # noqa: ANN001
    """**用户提的那条真问题**：换了个说法问同一件事。

    材料里写的是 `causal mask`，问的是「上三角屏蔽」—— 字面一个词都不沾。
    字面那路返回空；向量那路（假通道按同义给出）找对了。
    **这条就是这一整层存在的理由。**
    """
    body = ["# 注意力遮蔽", "因果掩码把上三角位置置为负无穷。"] + [
        "填充行 " + str(n) for n in range(3, 13)
    ]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=len(body))
    row = _slice(db_session, material, start=1, end=len(body), summary="注意力掩码")
    _vector(db_session, row, [1.0, 0.0, 0.0])

    # ① 字面：一个词都不沾 → 一条都没有
    literal = material_text.search(db_session, "上三角屏蔽", slug=material.slug)
    assert literal["hits"] == []

    # ② 向量：照样找对那一片
    hits = semantic.search(db_session, [1.0, 0.0, 0.0], slug=material.slug)
    assert [hit["slice"] for hit in hits] == [row.id]
    assert hits[0]["material"] == material.slug
    # 行区间是**切片上读出来的坐标**，不是向量算出来的
    assert (hits[0]["startLine"], hits[0]["endLine"]) == (1, len(body))


def test_vectors_from_another_model_are_skipped_not_mixed(db_session, tmp_path) -> None:  # noqa: ANN001
    """换过模型：维度对不上就**跳过**，绝不把两组向量混着算。

    混着算会给出"看着合理、其实无意义"的分数 —— 那比报错难查得多。
    """
    body = ["一行"] * 5
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=5)
    row = _slice(db_session, material, start=1, end=5)
    _vector(db_session, row, [1.0, 0.0, 0.0])  # 3 维（旧模型）

    assert [hit["slice"] for hit in semantic.search(db_session, [1.0, 0.0, 0.0], slug=material.slug)] == [row.id]
    assert semantic.search(db_session, [1.0, 0.0, 0.0, 0.0], slug=material.slug) == []


# ------------------------------------------------------------------- 融合


def test_fused_ranks_both_paths_above_one_path(db_session, tmp_path) -> None:  # noqa: ANN001
    """被**两路都找到**的，排在只被一路找到的前面 —— 融合的全部意义。

    造法：A 片含原词（字面第一），B 片的向量最像（向量第一）。
    A 出现在两条榜上 → RRF 之后 A 在前。
    """
    body = ["circular buffer 的地址对齐"] + ["填充 " + str(n) for n in range(2, 13)]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=len(body))
    both = _slice(db_session, material, start=1, end=5, summary="两边都沾")
    vector_only = _slice(db_session, material, start=6, end=len(body), summary="只有向量", slice_id="sl-002")
    _vector(db_session, both, [0.9, 0.1, 0.0])  # 像，但不如 B
    _vector(db_session, vector_only, [1.0, 0.0, 0.0])  # 最像

    result = semantic.search_fused(
        db_session, "circular buffer", query_vec=[1.0, 0.0, 0.0], slug=material.slug, limit=5
    )
    assert result["semantic"] is True
    assert [hit["slice"] for hit in result["hits"]][:2] == ["两边都沾", "只有向量"]
    via = {hit["slice"]: hit["via"] for hit in result["hits"]}
    assert via["两边都沾"] == "字面+向量"
    assert via["只有向量"] == "向量"


def test_a_literal_hit_lands_on_the_slice_that_covers_the_line(db_session, tmp_path) -> None:  # noqa: ANN001
    """字面那路给的是**行**，融合要在**切片**上做 —— 得先归位。

    命中落在第二片里，融合结果就该是第二片。这正是"不另切一遍"的好处：
    两路共用一个粒度。
    """
    body = ["前一片的内容 " + str(n) for n in range(1, 6)] + ["unique_keyword 在后一片"]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=len(body))
    _slice(db_session, material, start=1, end=5, summary="前一片")
    second = _slice(db_session, material, start=6, end=len(body), summary="后一片", slice_id="sl-002")

    result = semantic.search_fused(db_session, "unique_keyword", slug=material.slug)
    assert [hit["slice"] for hit in result["hits"]] == ["后一片"]
    assert result["hits"][0]["startLine"] == 6
    assert result["hits"][0]["firstMatch"] == 6
    assert second.start_line == 6  # 明确：坐标来自切片，不是猜的


def test_a_literal_hit_survives_when_the_material_has_no_slices(db_session, tmp_path) -> None:  # noqa: ANN001
    """**材料没有切片时，字面命中一条都不能少。**

    这条是拿一次真实翻车补的：融合层原先只认"落在切片上的命中"，材料没有切片时
    （资料模块的条目就不经切片流水线）字面命中会被**整个丢掉** —— 检索静默变少，
    而"搜不到"在模型眼里就是"这件事不存在"。所以没有切片可依时，
    它该保留**自己那一段**的行区间。
    """
    body = ["前一行", "exp_approx_mode 控制指数近似", "后一行"]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=3)

    result = semantic.search_fused(db_session, "exp_approx_mode", slug=material.slug)
    # 命中的还是那几行，而且**口径原样保留字面那一路的**：
    # startLine 是"块 + 上下文"的起点，firstMatch 才是精确命中的那一行
    assert [hit["firstMatch"] for hit in result["hits"]] == [2]
    assert result["hits"][0]["startLine"] == 1
    assert result["hits"][0]["endLine"] == 3
    assert result["hits"][0]["quote"] == body[1]
    assert result["hits"][0]["via"] == "字面"


def test_without_a_query_vector_it_degrades_and_says_so(db_session, tmp_path) -> None:  # noqa: ANN001
    """没给查询向量 = 只走字面。**必须说出来。**

    悄悄退回去，会让"通道没配好"表现成"这个工具搜不准" —— 那是最难查的一类
    失败。所以返回里带 `semantic: false` 和一句人话。
    """
    body = ["circular buffer 的地址对齐"]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=1)
    _slice(db_session, material, start=1, end=1, summary="那一片")

    result = semantic.search_fused(db_session, "circular buffer", query_vec=None, slug=material.slug)
    assert result["semantic"] is False
    assert result["hits"], "降级之后字面那路照样要能用"
    assert "字面" in result["note"] and "向量" in result["note"]


def test_the_fused_shape_stays_compatible_with_the_literal_path(db_session, tmp_path) -> None:  # noqa: ANN001
    """融合层**不许改调用方认识的字段** —— 换检索策略不该改工具的描述。

    `tools.search_material` 靠这几个键拼引用零件；少一个，引用就断了。
    """
    body = ["circular buffer"]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=1)
    _slice(db_session, material, start=1, end=1, summary="那一片")

    hit = semantic.search_fused(db_session, "circular buffer", slug=material.slug)["hits"][0]
    for key in ("material", "title", "startLine", "endLine", "firstMatch", "quote", "text"):
        assert key in hit, key


# ------------------------------------------------------------------- 现状


def test_a_window_carries_its_own_line_range(db_session, tmp_path) -> None:  # noqa: ANN001
    """向量找到的是**窗口**，行区间取窗口自己的 —— 比整片精确得多。

    这条是那次"切片太长"翻车的直接补丁：以前一片一条向量，命中的区间只能是整片
    （`L1-84` 这种），引用点开是一片的开头，**不是真正讲到那件事的地方**。
    """
    body = ["行 " + str(n) for n in range(1, 21)]
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=20)
    row = _slice(db_session, material, start=1, end=20)
    # 这一片里的第 2 个窗口：L11-15
    _vector(db_session, row, [1.0, 0.0], ordinal=2, start=11, end=15)

    hits = semantic.search(db_session, [1.0, 0.0], slug=material.slug)
    assert [(hit["startLine"], hit["endLine"]) for hit in hits] == [(11, 15)]
    assert hits[0]["slice"] == row.id  # 仍指得回它属于哪一片


def test_state_tells_apart_not_configured_from_not_run(db_session, tmp_path) -> None:  # noqa: ANN001
    """"还没嵌过"与"嵌过了"必须分得开 —— 两者在界面上都只是"搜不准"。"""
    body = ["一行"] * 4
    material = _material(db_session, _write(tmp_path / "m.md", body), lines=4)
    row = _slice(db_session, material, start=1, end=4)

    before = semantic.state(db_session)
    assert before["windows"] == 0 and before["ready"] is False
    assert before["slices"] >= 1 and before["covered"] == 0

    _vector(db_session, row, [1.0, 0.0])
    after = semantic.state(db_session)
    assert after["windows"] == 1 and after["ready"] is True
    assert after["covered"] == 1
    assert after["dims"] == [2]
    assert after["models"] == ["fake-embed"]
    assert after["coverage"] > before["coverage"]
