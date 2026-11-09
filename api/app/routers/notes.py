"""笔记的 HTTP 面：给**没有 Obsidian 的设备**（手机）读写笔记用。

为什么现在才开：`vault.py` 的降级（桥不在 → `notes` 表）2026-11-09 就写好了，
但 HTTP 上只有一个 `/api/obsidian/status`。桌面端确实不需要这些端点 ——
QF 直连文件、LLM 走工具层；手机端没有文件系统，是唯一必须走网络的人。

端点全是 `vault` 的同形包装，不在这里做业务判断：

* `GET  /api/notes/libraries`  库清单（桥在 = vault 名与篇数，桥不在 = 云副本）
* `GET  /api/notes/tree`       目录树（`lib`、`dir`）
* `GET  /api/notes/note`       读一篇（`lib`、`path`）
* `GET  /api/notes/search`     搜索（`q`、`lib`、`folder`、`limit`）
* `POST /api/notes/note`       新建（`path`+`body`，或 `folder`+`title`）
* `PUT  /api/notes/note`       整篇换正文（`path`、`body`）

错误口径只有两条：`NoteNotFound` → 404，其余 `NoteError` → 400，`detail` 是
中文原话 —— 手机上没地方看控制台，"还没配 Obsidian"这句话要能直接显示在界面上。

**乐观锁不在这里做**：`vault` 进程内记着上次读到那篇的 hash（`_HASH`），
「先 GET 读、再 PUT 写」这一对天然串得上；中间别处改过会 400 而不是覆盖。
这里再存一份 hash 就是第二份事实，迟早对不上。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import notelib, vault
from ..deps import DbSession

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _guard(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把 NoteError 翻成 HTTP —— 六个端点共用一处，口径不会漂。"""
    try:
        return call(*args, **kwargs)
    except vault.NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except vault.NoteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _shape(lib: notelib.Library) -> dict[str, Any]:
    return {
        "name": lib.name,
        "notes": lib.notes,
        "canvases": lib.canvases,
        "external": lib.external,
    }


def _library(name: str) -> notelib.Library:
    return _guard(vault.library, name)


@router.get("/libraries")
def list_libraries(db: DbSession) -> dict:
    vault.configure(db)
    return {"items": [_shape(lib) for lib in _guard(vault.libraries)]}


@router.get("/tree")
def get_tree(db: DbSession, lib: str = "", dir: str = "") -> dict:
    vault.configure(db)
    return _guard(vault.tree, _library(lib), dir)


@router.get("/note")
def read_note(db: DbSession, path: str, lib: str = "") -> dict:
    vault.configure(db)
    return _guard(vault.read_note, _library(lib), path)


@router.get("/search")
def search(
    db: DbSession,
    q: str,
    lib: str = "",
    folder: str = "",
    limit: int = 50,
) -> dict:
    """`limit` 夹在 1..200：手机传个 100000 不该把库扫穿（云副本走 LIKE，上限有意义）。"""
    vault.configure(db)
    size = max(1, min(int(limit or 50), 200))
    items = _guard(vault.search, q, lib_name=lib, limit=size, folder=folder)
    return {"items": items, "count": len(items)}


@router.post("/note")
def create_note(db: DbSession, body: dict) -> dict:
    """新建。两种写法二选一：给完整 `path`，或给 `folder` + `title`。"""
    vault.configure(db)
    target = _library(str(body.get("lib") or ""))

    rel = str(body.get("path") or "").strip()
    if rel:
        made = _guard(vault.create_at, target, rel, str(body.get("body") or ""))
        return {**made, "lib": target.name}

    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="新建笔记要给 title，或者直接给完整的 path")
    made = _guard(
        vault.create_note,
        target,
        str(body.get("folder") or ""),
        title,
        str(body.get("kind") or "note"),
    )
    return {**made, "lib": target.name}


@router.put("/note")
def write_note(db: DbSession, body: dict) -> dict:
    """整篇换正文（不是补丁）。路径必须已存在 —— 新建走 POST。"""
    vault.configure(db)
    rel = str(body.get("path") or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="path 不能空")
    text = body.get("body")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="body 必须是字符串（整篇正文）")

    target = _library(str(body.get("lib") or ""))
    saved = _guard(vault.write_body, target, rel, text, why=str(body.get("why") or "编辑"))
    return {**saved, "lib": target.name}
