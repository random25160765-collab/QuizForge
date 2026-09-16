"""知识图谱接口（给「图谱」页面用）。

**为什么这个路由不要登录**：它回的东西与 `dist/` 里那份公开题库完全同源
（概念、关系、考纲、材料名），页面又是给人看/给人演示的 ——
要登录才能打开一张图，只会让人以为它坏了。写接口（归并、改关系）在
`pipeline.graph_build` 里，那侧不受影响。

一次返回整张图：前端要自己算布局，来回分页请求反而更慢。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import graph

router = APIRouter(prefix="/api", tags=["graph"])


@router.get("/graph")
def whole_graph(
    min_weight: float = Query(1.0, ge=0, description="只保留权重 ≥ 此值的共现边"),
    include: str = Query("concept,topic,material", description="要哪些节点：concept/topic/material"),
) -> dict:
    """整张知识图谱（节点 + 边）。"""
    wanted = tuple(part.strip() for part in include.split(",") if part.strip())
    return graph.payload(min_weight=min_weight, include=wanted or graph.DEFAULT_INCLUDE)


@router.post("/graph/diagnose")
def diagnose_wrong(body: dict) -> dict:
    """把一批错题翻译成"该补哪个考点、按什么顺序补"。

    请求：`{"questionIds": ["tt-arch-0012", ...]}`
    返回：错题落在哪些考点、每个考点之前还有哪些前置考点、以及建议的顺序。
    卡在哪一步由客户端结合自己的掌握度决定 —— 服务端只给链，不给判语。
    """
    ids = [str(x) for x in (body or {}).get("questionIds") or [] if str(x).strip()]
    return graph.diagnose(ids)


@router.get("/graph/stats")
def graph_stats() -> dict:
    """一小段统计：节点/边/语义边各多少 —— 页面标题栏用它。"""
    data = graph.payload(with_questions=False)
    return data["stats"]
