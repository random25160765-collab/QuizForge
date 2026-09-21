"""材料正文：按坐标回读，以及三路召回里的第一路（规则）。

## 这一层为什么存在

库里**只存坐标**：切片是行区间（`material_slices`），知识点挂的是出处
（`point_sources`），材料的正文始终在文件里（`materials.source_path`）。
这是当初的决定，好处是库小、出处精确到行；代价是"取正文"要回读文件 ——
所以这台机器上必须真有那些文件，路径也一致。

于是这个模块干两件事：

* `read_lines(...)`：按 (材料, 行区间) 把原文读出来。**引用点开看的那几行就是它**。
* `search(...)`：**字面检索**。三路召回（规则 → 图 → 向量）里的第一路 ——
  零成本、可解释、结论能直接指到行号。

## 为什么先做字面，而不是先上向量

材料是技术文档，问法常常就是文档里的原词（`exp_approx_mode`、
`circular buffer`、函数名、配置项），字面命中率本来就高；而且它**可解释**：
命中了哪几行、几次，都能说清。向量只在"他换了个说法问同一件事"时才成为必需 ——
那时它补的是同义改写，不是把字面能做的事再做一遍。

## 代价与上限（说清，而不是假装没有）

一次全库扫是 71M 文本：第一次约两秒，之后靠进程内缓存降到几百毫秒。
所以单次调用**限制扫描的材料数**，并在返回里带上 `scanned` / `total` ——
没扫完就说没扫完。真要大规摸检索，该建的是索引或向量，
而不是想办法把文件读得更快。
"""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import select

from .models import Material

# 一次最多读多少行 / 扫多少份材料：都给个上限，免得一次工具调用把机器钉住
MAX_LINES_PER_READ = 200
MAX_SCAN_MATERIALS = 40
# 命中行的上下文半径（合并相邻命中时也用它）
CONTEXT_LINES = 2
# 每行截断：材料里有整段代码行，不截的话一条命中能上千字
MAX_LINE_CHARS = 300

# 缓存几份材料的正文。71M 全读进来不现实，但"最近问到的几份"很值得驻留：
# 同一轮对话里模型常常连着读同一份材料的邻近几段。
_CACHE_SIZE = 12


class MaterialError(Exception):
    """材料读不到（路径不通 / 行号越界）。文案要能直接给用户看。"""


@lru_cache(maxsize=_CACHE_SIZE)
def _lines(path: str) -> tuple[str, ...]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return tuple(handle.read().splitlines())


@lru_cache(maxsize=_CACHE_SIZE)
def _lower(path: str) -> tuple[str, ...]:
    return tuple(line.lower() for line in _lines(path))


def _checked(material: Material) -> None:
    if not os.path.isabs(material.source_path or ""):
        raise MaterialError("材料路径不是绝对路径：" + str(material.source_path))
    if not os.path.exists(material.source_path):
        raise MaterialError(
            "材料文件不在这台机器上："
            + material.source_path
            + "（库只存坐标、正文在文件里，所以换台机器要确保文件也在）"
        )


def read_lines(material: Material, start, end) -> list[dict]:  # noqa: ANN001
    """按行区间读原文（1-based，含两端）。行号越界就夹到文件范围内。"""
    _checked(material)
    all_lines = _lines(material.source_path)
    total = len(all_lines)

    start = max(1, int(start or 1))
    end = int(end or 0) or start + 59
    end = min(end, total, start + MAX_LINES_PER_READ - 1)
    if end < start:
        raise MaterialError(f"行区间越界：这份材料只有 {total} 行。")

    return [
        {"line": number, "text": all_lines[number - 1][:MAX_LINE_CHARS]}
        for number in range(start, end + 1)
    ]


def _terms(query: str) -> list[str]:
    """查询 → 词。字面检索：不做同义、不做词干，只说"这几个词要一起出现在同一行"。"""
    return [term for term in query.lower().split() if term.strip()]


def _blocks(numbers: list[int], total_lines: int) -> list[tuple[int, int]]:
    """把命中行合并成片段（相距很近的算一段），再各加一点上下文。"""
    blocks: list[tuple[int, int]] = []
    for number in numbers:
        if blocks and number - blocks[-1][1] <= CONTEXT_LINES * 2 + 1:
            blocks[-1] = (blocks[-1][0], number)
        else:
            blocks.append((number, number))
    return [
        (max(1, start - CONTEXT_LINES), min(total_lines, end + CONTEXT_LINES))
        for start, end in blocks
    ]


def search(  # noqa: ANN001
    db,
    query: str,
    *,
    slug: str = "",
    limit: int = 5,
    max_materials: int = MAX_SCAN_MATERIALS,
    depth: str = "",
) -> dict:
    """在材料正文里做字面检索。

    规则一句话说清：**所有词都出现在同一行**才算命中（词与词之间取"与"）。
    这样"circular buffer 的地址"不会因为"地址"烂大街而把整份文档捞出来；
    模型想放宽时，少给一个词就行。

    排序同样可复现：命中行多的材料在前，同分按材料 slug、行号。

    `depth` 按"资料走到哪一步"过滤（`出题` = 我在学的 / `检索` = 我查的）；
    空串 = 不过滤。
    """
    terms = _terms(str(query or ""))
    if not terms:
        return {"error": "给我一个关键词或一句话（字面命中就行，例如 exp_approx_mode）。"}

    stmt = select(Material).order_by(Material.slug)
    if slug:
        stmt = stmt.where(Material.slug == slug)
    if depth:
        stmt = stmt.where(Material.depth == depth)
    materials = db.scalars(stmt).all()
    if not materials:
        if slug:
            return {"error": "没有这份材料：" + slug}
        # 没给 slug 却一份都没选到 —— 那是**筛空了**（比如按 `depth` 过滤之后
        # 一条都没有），不是"这份材料不存在"。报 `没有这份材料：`（slug 还是空的）
        # 会让模型以为自己问错了名字。空结果照常返回，让它自己换一路再试。
        return {"query": query, "scanned": 0, "total": 0, "hits": []}

    scanned = 0
    skipped: list[str] = []
    hits: list[dict] = []
    for material in materials:
        if scanned >= max_materials:
            skipped.append(material.slug)
            continue
        try:
            _checked(material)
            lines = _lower(material.source_path)
        except (MaterialError, OSError):
            skipped.append(material.slug)
            continue
        scanned += 1

        numbers = [
            index + 1
            for index, line in enumerate(lines)
            if all(term in line for term in terms)
        ]
        if not numbers:
            continue

        raw = _lines(material.source_path)
        for start, end in _blocks(numbers, len(raw)):
            inside = [number for number in numbers if start <= number <= end]
            hits.append(
                {
                    "material": material.slug,
                    "title": material.title,
                    "startLine": start,
                    "endLine": end,
                    "matched": len(inside),
                    # 第一条命中行：引文就引它（引用块上那一行，点开才是整段）
                    "firstMatch": inside[0] if inside else start,
                    "quote": raw[(inside[0] if inside else start) - 1][:MAX_LINE_CHARS],
                    "text": "\n".join(
                        f"{number}: {raw[number - 1][:MAX_LINE_CHARS]}"
                        for number in range(start, end + 1)
                    ),
                }
            )

    hits.sort(key=lambda hit: (-hit["matched"], hit["material"], hit["startLine"]))
    result = {
        "query": query,
        "scanned": scanned,
        "total": len(materials),
        "hits": hits[: max(1, int(limit))],
    }
    if skipped:
        result["skipped"] = skipped[:10]
        result["note"] = (
            f"还有 {len(skipped)} 份材料没扫（单次上限 {max_materials} 份）。"
            "要更全的结果就指定 slug，或者换个更特别的词再问一次。"
        )
    return result
