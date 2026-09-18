"""笔记与画布的 HTTP 面。

这里**不碰数据库、不做鉴权**：笔记的权威是文件（见 `app/notes.py` 的模块注释），
这一层只是把 `app/notelib.py` 的能力开成接口。

所有写操作都**先快照再落盘**（在 `notelib` 里做），所以界面上永远有一个
「撤销上一次改动」可用 —— 幕布那边是"自动保存 + 撤销"，Obsidian 那边是"文件恢复"，
本地应用里这本来就是同一件事。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import canvas as canvaslib
from .. import notelib

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _run(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """跑一个笔记层操作，把它的错误翻成 HTTP。

    * 找不到 → 404：前端要拿它区分"这篇没了"和"这次操作不合法"；
    * 其余（路径越界、写不动、重名、行号不对）→ 400：是**请求**的问题。

    写成一个小包装而不是每个端点抄一遍 try/except：十几处抄下来，
    迟早有一处忘了接，然后那个错误会以 500 的形式冒出来，看起来像服务器崩了。
    """
    try:
        return call(*args, **kwargs)
    except notelib.NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (notelib.NoteError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _lib(name: str) -> notelib.Library:
    return _run(notelib.library, name or "")


def _text(body: dict, key: str, default: str = "") -> str:
    return str(body.get(key) or default)


# ------------------------------------------------------------------ 读


@router.get("/stats")
def stats() -> dict:
    """体检数字：几个库、多少篇、多少条引用、多少条找不到目标。"""
    return _run(notelib.stats)


@router.get("/tree")
def tree(lib: str = Query(..., description="库名，例如 Math")) -> dict:
    return _run(lambda: notelib.tree(_lib(lib)))


@router.get("/graph")
def graph(lib: str = Query(..., description="库名，例如 Math")) -> dict:
    """文档图谱：一篇笔记一个节点、一条双链一条边（读索引里现成的解析结果）。"""
    return _run(lambda: notelib.link_graph(_lib(lib)))


@router.get("/note")
def note(lib: str, path: str) -> dict:
    """一篇笔记的全貌：正文 + 大纲 + 出链 + 反链（前端一次渲染不用来回问）。"""
    return _run(notelib.read_note, _lib(lib), path)


@router.get("/search")
def search(q: str, lib: str = "", limit: int = 50) -> dict:
    return {"items": _run(notelib.search, q, lib_name=lib, limit=limit)}


@router.get("/tags")
def tags(lib: str = "") -> dict:
    return {"items": _run(notelib.tag_list, lib)}


# ------------------------------------------------------------------ 写


@router.post("/line")
def line(body: dict) -> dict:
    """行级编辑 —— 幕布那半边的动作都在这里。

    `op` 取 `insert`（回车同级）/ `replace` / `replace_range`（块级替换，配 `count`）/
    `delete` / `indent` / `outdent` / `move` / `move_sibling`（拖拽搬整块，配 `target`）。
    """
    lib = _lib(_text(body, "lib"))
    return _run(
        notelib.line_op,
        lib,
        _text(body, "path"),
        op=_text(body, "op"),
        index=int(body.get("index") or 0),
        raw=_text(body, "raw"),
        # `delta` 只给 `move` 用：+1 往下挪一块、-1 往上（Alt+↓ / Alt+↑）
        delta=int(body.get("delta") or 0),
        # `count` 只给 `replace_range` 用：要替换掉几行（默认视图按块改）
        count=int(body.get("count") or 0),
        # `target` 只给 `move_sibling` 用：拖拽时落在了哪一行（子树边界由后端算）
        target=int(body.get("target") if body.get("target") is not None else -1),
    )


@router.post("/body")
def write_body(body: dict) -> dict:
    """整段换正文（文档态保存）。元数据头原样保留 —— 详见 `notes.splice_body`。"""
    lib = _lib(_text(body, "lib"))
    return _run(
        notelib.write_body,
        lib,
        _text(body, "path"),
        _text(body, "body"),
        why="body",
    )


@router.post("/meta")
def set_meta(body: dict) -> dict:
    """改元数据（标题 / 标签 / 自定义属性）。只动那个头，正文一字不碰。"""
    lib = _lib(_text(body, "lib"))
    patch = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    return _run(notelib.set_meta, lib, _text(body, "path"), patch)


@router.post("/create")
def create(body: dict) -> dict:
    """新建一篇笔记或一份大纲。「未解析链接」的一键补齐也走这里。

    `kind` 取 `note`（默认）或 `outline` —— 两者是**不同的文件**（`.md` / `.outline`），
    建的时候就得说清，不能事后靠改名猜。
    """
    lib = _lib(_text(body, "lib"))
    kind = _text(body, "kind") or "note"
    if kind not in ("note", "outline"):
        raise HTTPException(status_code=400, detail=f"不认识的 kind：{kind}")
    return _run(notelib.create_note, lib, _text(body, "folder"), _text(body, "title"), kind=kind)


@router.post("/rename")
def rename(body: dict) -> dict:
    """改名，并**把引用它的地方一起改**（旧编辑器 `alwaysUpdateLinks` 那条）。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.rename_note, lib, _text(body, "path"), _text(body, "to"))


def _suggester():
    """拿 `pipeline.note_suggest`。

    路径引导已经统一在 `app/__init__.py`（原先这里自己补过一次，资料路由漏补就 500 了）。
    这里保留**延迟导入**：这个模块会拖起模型客户端，
    不该让"打开笔记页"为它付启动成本（这是仓库对重依赖的一贯做法）。
    """
    from pipeline import note_suggest  # noqa: PLC0415

    return note_suggest


@router.get("/suggest/candidates")
def suggest_candidates(lib: str, limit: int = 12) -> dict:
    """哪些笔记最该整理（没标签的排前面）。只读清单，不调模型。"""
    module = _suggester()
    return {"items": module.pick_candidates(_lib(lib), limit=limit)}


@router.post("/suggest")
async def suggest(body: dict) -> dict:
    """对指定的几篇跑一遍建议。**只调模型、不落盘** —— 落盘要人点「接受」。"""
    module = _suggester()
    lib = _lib(_text(body, "lib"))
    paths = [str(item) for item in (body.get("paths") or []) if str(item).strip()]
    if not paths:
        return {"items": [], "failed": [], "dropped": 0, "note": "没有指定要整理的笔记"}
    try:
        return await module.suggest(lib, paths)
    except Exception as exc:  # noqa: BLE001 - 模型侧什么都可能抛（没配密钥、超时、额度）
        raise HTTPException(status_code=502, detail=f"模型没能给出建议：{str(exc)[:200]}") from exc


@router.post("/suggest/apply")
def suggest_apply(body: dict) -> dict:
    """把**被接受的那几条**写进文件（写之前照例留快照，可撤销）。"""
    module = _suggester()
    lib = _lib(_text(body, "lib"))
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items 得是个列表")
    result = module.apply(lib, items)
    # 索引是派生的缓存：写完要让它下次重读（不然树里看不到刚补的标签）
    notelib.index(lib).invalidate()
    return result


@router.get("/canvas")
def canvas_read(lib: str, path: str) -> dict:
    """读一块画布：节点 / 边 / 内容包围盒。坏 JSON 与空对象都降级成空画布，不抛给用户。"""
    return _run(canvaslib.read, _lib(lib), path)


@router.post("/canvas")
def canvas_apply(body: dict) -> dict:
    """把**一批**改动一次落盘（一次快照）。

    拖一张卡片前端会攒几十次位移 —— 逐个落盘会把快照轮转冲干净、"撤销"随即失效。
    """
    lib = _lib(_text(body, "lib"))
    ops = body.get("ops")
    if not isinstance(ops, list):
        raise HTTPException(status_code=400, detail="ops 得是个列表")
    return _run(canvaslib.apply, lib, _text(body, "path"), ops)


@router.post("/delete")
def delete(body: dict) -> dict:
    """删除一篇 —— **移到回收站**（`.trash/`），不是真删。见 `notelib.delete_note`。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.delete_note, lib, _text(body, "path"))


@router.post("/move")
def move(body: dict) -> dict:
    """把一篇挪进另一个目录（树里拖拽就是这个）。按路径写的引用会一起改。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.move_note, lib, _text(body, "path"), _text(body, "folder"))


@router.get("/trash")
def trash(lib: str) -> dict:
    return {"items": _run(notelib.trash_list, _lib(lib))}


@router.post("/trash/restore")
def trash_restore(body: dict) -> dict:
    lib = _lib(_text(body, "lib"))
    return _run(notelib.restore_from_trash, lib, _text(body, "name"), _text(body, "folder"))


@router.post("/snapshot")
def snapshot(body: dict) -> dict:
    """手动存一版（命名快照）。它不参与撤销游标，也不会被自动清理掉。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.snapshot_named, lib, _text(body, "path"), _text(body, "name"))


@router.get("/diff")
def diff(lib: str, path: str, name: str) -> dict:
    """当前版与某一版的逐行差异（"改动可对比"）。"""
    return _run(notelib.diff_version, _lib(lib), path, name)


@router.post("/undo")
def undo(body: dict) -> dict:
    """撤销这篇笔记的最近一次改动（撤回前会先把当前状态存一份，于是撤销也可撤销）。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.undo, lib, _text(body, "path"))


@router.get("/snapshots")
def snapshots(lib: str, path: str) -> dict:
    """这篇笔记的改动历史（文件恢复列表）。"""
    return {"items": _run(notelib.snapshots, _lib(lib), path)}


@router.post("/restore")
def restore(body: dict) -> dict:
    """恢复指定的那一版（历史里的某一条）。"""
    lib = _lib(_text(body, "lib"))
    return _run(notelib.restore, lib, _text(body, "path"), _text(body, "name"))
