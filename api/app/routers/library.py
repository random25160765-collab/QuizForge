"""资料库的 HTTP 面。

三件必须说清的边界：

* **源目录只读**。这里没有任何"往资料根里写"的接口 —— 只有元数据与派生缓存可写。
* **元数据一冻结就不被覆盖**。索引动作只给"还没有元数据"的条目补一份，
  人改过的（`origin: manual`）与模型填过的（`llm`）一律不动 —— 不然跑一次索引
  就把人手工补的作者冲掉了，而且没有任何提示。
* **根目录清单落 `user_settings`**（单用户下的单行设置），文件本身不动。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from .. import library as lib
from .. import notelib
from ..deps import CurrentUser, DbSession
from ..models import UserSettings

# 前缀跟着全仓约定走 `/api/...`（别的路由也都是 `/api` 或 `/api/进度` 这种）。
# 写成 `/library` 的后果实测过：路由挂上了，但 `/api/library/roots` 一律 404。
router = APIRouter(prefix="/api/library", tags=["library"])

#: 设置里存根目录清单的键。
ROOTS_KEY = "library_roots"

#: 环境变量：用于测试与"我就是不想用默认根"的场合。
ROOTS_ENV = "QF_LIBRARY_ROOTS"


def _run(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """跑一个资料层操作，把错误翻成 HTTP（与笔记路由同一套约定）。

    找不到 → 404；其余（路径不对、写不动、引用键冲突）→ 400。
    """
    try:
        return call(*args, **kwargs)
    except lib.LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except notelib.NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _data_dir() -> Path:
    from ..config import get_settings

    return Path(get_settings().data_dir)


def _meta_dir() -> Path:
    return _data_dir() / "library"


def _text_dir() -> Path:
    return _meta_dir() / lib.TEXT_DIR


def default_roots() -> list[Path]:
    """默认资料根。

    优先 `QF_LIBRARY_ROOTS`（冒号或分号分隔，测试与"另有安排"时用），
    否则用仓库里的 `reference/`（开发环境它是指向 `F:\\Documents` 的软链），
    再否则用 `F:/Documents`（Windows 上的正式默认）。
    """
    env = os.environ.get(ROOTS_ENV, "").strip()
    if env:
        return [Path(part) for part in env.replace(";", os.pathsep).split(os.pathsep) if part.strip()]
    link = Path(__file__).resolve().parents[3] / "reference"
    if link.is_dir():
        return [link]
    if Path("F:/Documents").is_dir():
        return [Path("F:/Documents")]
    return []


def roots_for(user: UserSettings | None) -> list[Path]:
    """这个人用哪些根：设置里有就用设置里的（可以是空列表 = 明确不要任何根）。"""
    if user is not None and isinstance(user.data, dict) and ROOTS_KEY in user.data:
        stored = user.data.get(ROOTS_KEY) or []
        if isinstance(stored, list):
            return [Path(str(part)) for part in stored if str(part).strip()]
    return default_roots()


def _settings_row(db: DbSession, user_id: Any) -> UserSettings | None:
    return db.get(UserSettings, user_id)


def _save_roots(db: DbSession, user_id: Any, paths: list[Path]) -> None:
    row = _settings_row(db, user_id)
    data = dict(row.data or {}) if row else {}
    data[ROOTS_KEY] = [str(path) for path in paths]
    if row:
        row.data = data
    else:
        db.add(UserSettings(user_id=user_id, data=data))
    db.commit()


@router.get("/roots")
def roots(user: CurrentUser, db: DbSession) -> dict:
    """当前根 + 默认根 + 规模。前端拿它画左栏的树。"""
    row = _settings_row(db, user.id)
    active = roots_for(row)
    items = [item for root in active for item in lib.scan(root)]
    return {
        "roots": [str(path) for path in active],
        "default": [str(path) for path in default_roots()],
        "fromSettings": bool(row and isinstance(row.data, dict) and ROOTS_KEY in row.data),
        "items": len(items),
        "missing": [str(path) for path in active if not path.is_dir()],
    }


@router.post("/roots")
def edit_roots(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """加一个根 / 去掉一个根。**只改清单，不动任何文件。**"""
    action = str(body.get("action") or "add")
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="得给一个目录")
    row = _settings_row(db, user.id)
    current = roots_for(row)
    target = Path(raw).expanduser()
    if action == "add":
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"这个目录不存在：{raw}")
        resolved = target.resolve()
        if any(path.resolve() == resolved for path in current):
            return {"roots": [str(path) for path in current], "added": False}
        current = current + [target]
    elif action == "remove":
        resolved = target.resolve() if target.exists() else target
        current = [path for path in current if path.resolve() != resolved]
    else:
        raise HTTPException(status_code=400, detail=f"不认识的动作：{action}")
    _save_roots(db, user.id, current)
    return {"roots": [str(path) for path in current], "added": action == "add"}


@router.get("/items")
def items(user: CurrentUser, db: DbSession, q: str = "", limit: int = 200) -> dict:
    """条目列表。带 `q` 时走**与笔记同一套表达式**检索。"""
    roots = roots_for(_settings_row(db, user.id))
    if q.strip():
        found = _run(lib.search, roots, _meta_dir(), _text_dir(), q, limit=max(1, min(limit, 500)))
        return {"items": found, "query": q, "count": len(found)}
    listed = [entry.brief() for entry in lib.entries(roots, _meta_dir(), _text_dir())]
    listed.sort(key=lambda item: (str(item.get("year") or ""), str(item.get("title") or "")))
    return {"items": listed[: max(1, min(limit, 1000))], "count": len(listed)}


@router.get("/item")
def item(citekey: str, user: CurrentUser, db: DbSession, text: int = 0) -> dict:
    """一个条目的详情：元数据 + 附属资源 + 正文质量 + **谁引用了它** + BibTeX。"""
    roots = roots_for(_settings_row(db, user.id))
    found = [entry for entry in lib.entries(roots, _meta_dir(), _text_dir()) if entry.citekey == citekey]
    if not found:
        raise HTTPException(status_code=404, detail=f"没有这个条目：{citekey}")
    entry = found[0]
    needles = [entry.citekey, entry.item.path.name, entry.item.stem]
    # 「谁引用了它」跨**所有**笔记库看（引用可能写在任何一个库里）。
    # 画布也算一篇，但画布的 `body` 是空的（它的引用在 `file` 节点里，指向的是笔记不是资料），
    # 所以画布实际上不会命中 —— 这是**已知且合理**的，不是漏了。
    notes: list[dict[str, Any]] = []
    for note_lib in notelib.libraries():
        notes.extend(
            {"path": note.rel, "title": note.title, "body": note.body}
            for note in notelib.index(note_lib).entries()
        )
    detail = entry.brief()
    detail["bibtex"] = lib.bibtex(entry.meta)
    detail["citedBy"] = lib.referencing_notes(needles, notes)
    detail["assets"] = [str(path) for path in entry.assets]
    if text:
        state = entry.text_state
        detail["text"] = {
            "state": str(state.get("state") or "none"),
            "chars": int(state.get("chars") or 0),
            "ratio": float(state.get("ratio") or 0.0),
            "head": lib.text_of(_text_dir(), citekey, limit=max(200, min(text, 20000))),
        }
    return detail


@router.post("/meta")
def edit_meta(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """手工补/改元数据。**这就是"人是权威"的入口** —— 改完 `origin` 变成 `manual`。"""
    citekey = str(body.get("citekey") or "").strip()
    patch = body.get("meta")
    if not citekey or not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="得给 citekey 与 meta")
    known = lib.load_all_metadata(_meta_dir())
    merged = dict(known.get(citekey) or {})
    merged.update({key: value for key, value in patch.items() if key != "citekey"})
    merged["citekey"] = citekey
    saved = _run(lib.save_metadata, _meta_dir(), merged, origin="manual")
    return {"citekey": saved["citekey"], "meta": saved}


@router.post("/index")
def index(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """抽一批正文并**冻结**元数据（只给还没有元数据的条目写）。

    分批是刻意的：全量抽取是这一层唯一的重 IO（实测 55 个 PDF、282MB），
    一次请求跑完会让界面一直转圈、也没法显示进度。前端按 `remaining` 循环调用。
    """
    limit = int(body.get("limit") or 8)
    force = bool(body.get("force"))
    roots = roots_for(_settings_row(db, user.id))
    known = lib.load_all_metadata(_meta_dir())
    done: list[dict[str, Any]] = []
    remaining = 0
    for entry in lib.entries(roots, _meta_dir(), _text_dir()):
        key = entry.citekey
        if key in known and not force:
            state = entry.text_state
            if state.get("state") != "none":
                continue
        if len(done) >= max(1, min(limit, 50)):
            remaining += 1
            continue
        frozen = key in known
        state = _run(lib.extract_text, entry.item, _text_dir(), key, force=force)
        # **抽完再定键**：作者与年份就在正文里。踩过两次才写对 ——
        # 第一次是先定键（`arxiv220514135` 本可以是 `dao2022flashattention`），
        # 第二次是注释说要抽完再定、代码却仍然把键设成了抽之前的那个。
        fresh = lib.infer(entry.item, head_text=lib.head_text_for(entry.item, _text_dir(), key))
        fresh.setdefault("source", str(entry.item.path))
        if frozen:
            fresh["citekey"] = key                      # 已冻结的键不动（它可能已经被引用）
        else:
            fresh["citekey"] = lib.citekey_for(fresh, fallback=entry.item.rel)
            _run(lib.save_metadata, _meta_dir(), fresh, origin="inferred")
        done.append(
            {
                "citekey": fresh["citekey"],
                "was": key if fresh["citekey"] != key else "",
                "state": state.get("state"),
                "chars": state.get("chars"),
            }
        )
    return {"done": done, "remaining": remaining, "metaDir": str(_meta_dir())}


__all__ = ["default_roots", "roots_for", "router"]
