"""向量检索层：窗口的向量、余弦，以及和字面那一路的融合（RRF）。

## 这一层回答什么、不回答什么（`docs/检索与向量化.md` §3.2）

| 谁回答什么问题 | 用什么 |
|---|---|
| 这句话跟哪一段讲的是**同一件事** | **向量**（对称的"像不像"） |
| A 和 B 是什么关系、先学哪个 | **图**（反对称、要可解释）—— 不在这里 |
| 这句话在原文的**哪几行** | **行区间**（唯一权威）—— 不在这里 |

三者是**串联**的：向量把范围收窄到几个窗口 → 图给出结构（这段属于哪个概念、
它的前置是什么）→ 行区间给出可点开的原文。

## 一条不能破的规矩

**向量只说"像不像"，绝不说"在哪一行"。** 出处永远只有 `point_sources` /
`material_slices` / `slice_embeddings` 的行区间，回读仍是 `materials.read_lines`
（与前端"点开引用"是同一个函数）。一旦让向量去"生成"出处，引用就不能再当证据了
（§3.3，也是整个流水线的地基）。

## 检索单位是「窗口」不是「切片」

§3.1 那条"粒度直接用切片"**对 embedding 是错的**：切片平均 6979 字，
而模型的可用窗口是 1024 token —— 一片一条向量只会嵌进开头一小段，
于是「前言」那种短切片赢过所有长切片（实测：中文问句全部返回前言）。
窗口是切片内按行切的，**带自己的行区间**，所以出处照样是行区间、而且更精确。

## 降级是刻意的，而且要说出来

没有向量（本地模型还没下载 / 还没跑 `pipeline.embed`）时，`search_fused` 退回
**纯字面**，并在返回里带 `semantic: false`。悄悄退回去，会让"没配好"表现成
"搜不准" —— 那是最难查的一类失败。
"""

from __future__ import annotations

import math
import struct

from sqlalchemy import func, select

from . import materials
from .models import Material, MaterialSlice, SliceEmbedding

#: RRF 的平滑常数。60 是那篇原论文给的默认值：它把"名次靠前"的收益压得不那么陡
#: （第 1 名与第 2 名只差 1/61 与 1/62），于是「只被一路排第一」不会碾压
#: 「被两路都排进前五」—— 后者其实更可信。
RRF_K = 60


# --------------------------------------------------------------- 向量的存与算


def pack(vec) -> bytes:  # noqa: ANN001
    """float32 紧凑打包。**小端定死**：库文件是本地一个文件，但它可能被拷到
    别的架构上（`db/quizforge.db.gz` 就会跨机器），所以不能让字节序跟着机器走。"""
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])


def unpack(blob: bytes) -> list[float]:  # noqa: ANN001
    """还原 `pack` 的产物。维度从长度推（float32 = 4 字节）—— 不必再存一份。"""
    if not blob:
        return []
    return list(struct.unpack("<%df" % (len(blob) // 4), blob))


def cosine(left: list[float], right: list[float]) -> float:  # noqa: ANN001
    """余弦相似度。**不假设已归一化**（归一化的活儿留给算的人，这里只管对）。

    任一向量为零向量时返回 0.0：那种向量没有方向，和谁都不像 ——
    返回 0 比抛异常合适，它不该让整次检索崩掉。
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_l = 0.0
    norm_r = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_l += a * a
        norm_r += b * b
    if norm_l <= 0.0 or norm_r <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_l * norm_r)


def _cosines(query: list[float], blobs: list[bytes]) -> list[float]:  # noqa: ANN001
    """一次算完所有余弦（同维度、同序）。

    **numpy 优先，纯 Python 兜底**，这是刻意的双路而不是偷懒：numpy 是**本地模型
    带进来的**（装在那个缓存目录里），所以它可能不在（模型还没下载 / 被删了）。
    实测差别很大：930 窗 × 1024 维，纯 Python 循环约 **1.4 秒**，
    numpy 是几毫秒。而这一层是**每次检索都要走**的。

    维度不一致的在调用方已经滤掉了，所以这里可以按同一维度打包。
    """
    if not blobs:
        return []
    try:
        from . import local_embed

        local_embed.activate()  # 幂等：把模型缓存目录挂进 sys.path
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return [cosine(query, unpack(blob)) for blob in blobs]

    matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), -1)
    vector = np.asarray(query, dtype="<f4")
    query_norm = float(np.linalg.norm(vector))
    if query_norm <= 0.0:
        return [0.0] * len(blobs)
    dots = matrix @ vector
    norms = np.linalg.norm(matrix, axis=1) * query_norm
    return (dots / np.maximum(norms, 1e-9)).tolist()


# --------------------------------------------------------------------- 现状


def state(db) -> dict:  # noqa: ANN001
    """向量这一层的现状：多少个**窗口**有向量、盖住几片切片、什么模型、多少维。

    设置面板与工具都要它。**"模型还没下载"与"下载了但还没跑 embed"在界面上
    都只是"搜不准"** —— 不把这两个分开报出来，就只能靠猜。
    """
    windows = db.scalar(select(func.count()).select_from(SliceEmbedding)) or 0
    slices = db.scalar(select(func.count()).select_from(MaterialSlice)) or 0
    covered = db.scalar(select(func.count(func.distinct(SliceEmbedding.slice_id)))) or 0
    meta = db.execute(
        select(SliceEmbedding.model, SliceEmbedding.dim).distinct()
    ).all()
    return {
        "ready": windows > 0,
        "windows": int(windows),
        "slices": int(slices),
        "covered": int(covered),
        "coverage": round(covered / slices, 4) if slices else 0.0,
        "models": sorted({str(model or "") for model, _dim in meta}),
        "dims": sorted({int(dim or 0) for _model, dim in meta}),
    }


# ----------------------------------------------------------------- 语义那一路


def search(db, query_vec, *, limit: int = 8, slug: str = "", depth: str = "") -> list[dict]:  # noqa: ANN001
    """按余弦找最像的几个**窗口**。

    返回的是窗口（含它自己的行区间），调用方不必认识"切片"这一层。
    行区间是**从库里读出来的坐标**，不是向量算出来的 —— 出处只认行区间。

    实现是**暴力全扫**，但算余弦走 `_cosines`（numpy 优先）。实测：930 窗 ×
    1024 维，纯 Python 循环约 **1.4 秒/次**，numpy 几毫秒 —— 而这是每次检索
    都要走的，所以那条快路是必需的，慢路只是"模型没下载时不许挂"的兜底。
    """
    if not query_vec:
        return []
    want = len(query_vec)
    stmt = (
        select(SliceEmbedding, MaterialSlice, Material)
        .join(MaterialSlice, MaterialSlice.id == SliceEmbedding.slice_id)
        .join(Material, Material.id == MaterialSlice.material_id)
    )
    if slug:
        stmt = stmt.where(Material.slug == slug)
    if depth:
        stmt = stmt.where(Material.depth == depth)

    rows = [
        (window, slice_row, material)
        for window, slice_row, material in db.execute(stmt).all()
        # 换过模型：维度对不上就跳过。这不是错误 —— `state()` 会把"有几种维度"
        # 报出来，谁该重算一目了然。
        if not window.dim or window.dim == want
    ]
    if not rows:
        return []
    scores = _cosines(query_vec, [window.vec for window, _s, _m in rows])
    scored = [
        (score, material.slug, window.start_line, window, slice_row, material)
        for score, (window, slice_row, material) in zip(scores, rows)
    ]
    # 同分也要有确定顺序（可复现）：slug → 起始行 → 窗口 id
    scored.sort(key=lambda row: (-row[0], row[1], row[2], row[3].id))
    return [
        {
            "window": window.id,
            "slice": slice_row.id,
            "material": material.slug,
            "title": material.title,
            "startLine": window.start_line,
            "endLine": window.end_line,
            "summary": slice_row.summary,
            "score": round(score, 6),
        }
        for score, _slug, _start, window, slice_row, material in scored[: max(1, int(limit))]
    ]


# --------------------------------------------------------------------- 融合


def rrf(*rankings, k: int = RRF_K) -> list[tuple]:  # noqa: ANN001
    """Reciprocal Rank Fusion：**只用名次，不用分数**。

    为什么名次就够了：两路的分数量纲根本不可比 —— 余弦是 0~1 的相似度，
    字面那路是"命中了几行"。要把它们加权相加就得先归一化，而归一化要调参、
    还要随语料重新调；名次不需要，而且**新加一路不用重新调参**
    （三路召回 + RRF 是 2026-09-16 就定下的，见 `docs/STATUS.md` §十）。

    `rankings` 是若干个"按好坏排好的 key 序列"。返回 `[(key, score)]`，
    按分降序、同分按 key 升序（**可复现**，不依赖 dict 顺序）。
    """
    scores: dict = {}
    for ranking in rankings or ():
        for rank, key in enumerate(ranking or (), start=1):
            if key is None:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


# ------------------------------------------------------------ 字面 → 窗口 的归位


def _window_index(db, slug: str = "", depth: str = "") -> tuple[dict, dict, dict, dict]:  # noqa: ANN001
    """把窗口与切片索引进内存，返回 `(按窗口 id, 窗口按材料, 切片按材料)`。

    千级窗口全量载入是微不足道的，换来的是融合那一步**不必为每个命中再查库**
    （否则就是个 N+1）。

    **为什么要同时索引切片**：材料还没向量化时它一片窗口都没有（模型没下载 /
    还没跑 embed 就是这种状态），而段标题（`summary`）对模型判断"这段讲什么"
    仍然有用 —— 只按窗口找会让那个字段凭空消失。
    """
    stmt = (
        select(SliceEmbedding, MaterialSlice, Material)
        .join(MaterialSlice, MaterialSlice.id == SliceEmbedding.slice_id)
        .join(Material, Material.id == MaterialSlice.material_id)
        .order_by(Material.slug, SliceEmbedding.start_line)
    )
    if slug:
        stmt = stmt.where(Material.slug == slug)
    if depth:
        stmt = stmt.where(Material.depth == depth)
    by_id: dict = {}
    windows: dict = {}
    for window, slice_row, material in db.execute(stmt).all():
        by_id[window.id] = (window, slice_row, material)
        windows.setdefault(material.slug, []).append(
            (window.start_line, window.end_line, window.id)
        )

    slice_stmt = (
        select(MaterialSlice, Material)
        .join(Material, Material.id == MaterialSlice.material_id)
        .order_by(Material.slug, MaterialSlice.start_line)
    )
    if slug:
        slice_stmt = slice_stmt.where(Material.slug == slug)
    if depth:
        slice_stmt = slice_stmt.where(Material.depth == depth)
    slices: dict = {}
    slice_by_id: dict = {}
    for slice_row, material in db.execute(slice_stmt).all():
        slices.setdefault(material.slug, []).append(
            (slice_row.start_line, slice_row.end_line, slice_row.id)
        )
        slice_by_id[slice_row.id] = (slice_row, material)
    return by_id, windows, slices, slice_by_id


def _covering(windows: dict, slices: dict, slug: str, line) -> tuple | None:  # noqa: ANN001
    """这一行落在哪个**窗口**里；没窗口就退到**切片**。

    字面检索给的是行，而融合要在窗口上做（窗口是两路共有的粒度）。
    材料没有窗口时按切片归位 —— 这样段标题还在，只是精度粗一点。
    """
    try:
        line = int(line or 0)
    except (TypeError, ValueError):
        return None
    for start, end, window_id in windows.get(slug or "", ()):
        if start <= line <= end:
            return ("window", window_id)
    for start, end, slice_id in slices.get(slug or "", ()):
        if start <= line <= end:
            return ("slice", slice_id)
    return None


def _quote_of(material, start_line: int) -> dict:  # noqa: ANN001
    """只被向量找到的那个窗口：给它配一句引言。

    取**窗口第一行**就够 —— 目的是让模型看见"这一段讲的什么"，而不是把整窗塞进
    上下文。真要读，它该去调 `read_material` 按行区间读（那才是引用同源的那条路）。
    """
    blank = {"firstMatch": start_line, "quote": "", "text": "", "matched": 0}
    try:
        rows = materials.read_lines(material, start_line, start_line)
    except (materials.MaterialError, OSError):
        return blank
    if not rows:
        return blank
    return {
        "firstMatch": start_line,
        "quote": rows[0]["text"],
        "text": f"{rows[0]['line']}: {rows[0]['text']}",
        "matched": 0,
    }


def search_fused(  # noqa: ANN001
    db,
    query: str,
    *,
    query_vec=None,
    slug: str = "",
    limit: int = 5,
    k: int = RRF_K,
    depth: str = "",
) -> dict:
    """检索入口：**字面 + 向量**两路融合（第三路"图"不在这里，它回答的是另一个问题）。

    返回形状与 `materials.search` **兼容**（`hits` 里仍是 material / title /
    startLine / endLine / firstMatch / quote / text），另加 `via`（哪几路找到的）、
    `score`（RRF 分）、`slice`（那一段的标题）。这样调用方不必认识向量这一层 ——
    换检索策略不该改工具的描述。

    `query_vec` 没给就等于"只走字面"（本地模型没就绪时就是这样），
    并且会在 `semantic: false` 里说明，不假装。
    """
    window = max(int(limit) * 3, 8)
    literal = materials.search(db, query, slug=slug, limit=window, depth=depth)
    if literal.get("error"):
        return literal  # 空查询之类：它的文案是写给模型看的，原样交回

    literal_hits = literal.get("hits") or []
    vector_hits = (
        search(db, query_vec, slug=slug, limit=window, depth=depth) if query_vec else []
    )

    by_id, windows, slices, slice_by_id = _window_index(db, slug, depth)

    # 两路各自排成"名次表"。**键统一成 `(类型, 标识)`**：字面那一路给的是**行**、
    # 向量那一路给的是**窗口**，融合要求两边可比 ——
    #   * 落在某个窗口里 → 按**窗口**记，两路才会撞在一起（这才是融合的价值）；
    #   * 只落在切片里（那份材料还没向量化）→ 按**切片**记，段标题还在；
    #   * 两者都沾不上 → 按"材料 + 行"自成一条。
    #     **一条命中都不该因为"没有窗口"而消失**：那会让检索静默变少，
    #     而"搜不到"在模型眼里就是"这件事不存在"（实测踩到过）。
    literal_rank: list = []
    literal_of: dict = {}
    for hit in literal_hits:
        row_slug = str(hit.get("material") or "")
        key = _covering(windows, slices, row_slug, hit.get("firstMatch"))
        if key is None:
            key = ("line", row_slug, int(hit.get("firstMatch") or 0))
        if key in literal_of:
            continue
        literal_rank.append(key)
        literal_of[key] = hit

    vector_rank: list = []
    vector_of: dict = {}
    for hit in vector_hits:
        key = ("window", hit["window"])
        if key in vector_of:
            continue
        vector_rank.append(key)
        vector_of[key] = hit

    hits = []
    for key, score in rrf(literal_rank, vector_rank, k=k):
        window_row = slice_row = material = None
        if key[0] == "window":
            window_row, slice_row, material = by_id[key[1]]
        elif key[0] == "slice":
            slice_row, material = slice_by_id[key[1]]
        base = literal_of.get(key)
        if base is None and slice_row is not None:
            anchor = window_row.start_line if window_row is not None else slice_row.start_line
            base = _quote_of(material, anchor)

        if window_row is not None:
            # 行区间一律取**窗口**的 —— 它是读出来的坐标，不是向量算出来的
            start_line, end_line = window_row.start_line, window_row.end_line
        elif slice_row is not None:
            # 没窗口可依（这份材料还没向量化）：退到切片自己的区间
            start_line, end_line = slice_row.start_line, slice_row.end_line
        else:
            # 连切片也沾不上：用字面自己那一段的区间
            start_line, end_line = base.get("startLine"), base.get("endLine")
        label = (slice_row.summary or slice_row.slice_id) if slice_row is not None else ""

        paths = (["字面"] if key in literal_of else []) + (["向量"] if key in vector_of else [])
        hits.append(
            {
                "material": material.slug if material is not None else base.get("material"),
                "title": material.title if material is not None else base.get("title"),
                "startLine": start_line,
                "endLine": end_line,
                "firstMatch": base.get("firstMatch") or start_line,
                "quote": base.get("quote") or "",
                "text": base.get("text") or "",
                "matched": base.get("matched") or 0,
                "slice": label,
                "score": round(score, 6),
                "via": "+".join(paths) if paths else "字面",
            }
        )

    result = {
        "query": query,
        "semantic": bool(query_vec),
        "hits": hits[: max(1, int(limit))],
        "scanned": literal.get("scanned"),
        "total": literal.get("total"),
    }

    notes = []
    if literal.get("note"):
        notes.append(str(literal["note"]))
    if not query_vec:
        notes.append(
            "这次只走了**字面**这一路（向量没参与）—— 换了个说法问同一件事时它接不住。"
            "向量要本地小模型就绪（见 docs/检索与向量化.md）。"
        )
    if notes:
        result["note"] = " ".join(notes)
    return result
