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


def _classifier():
    """拿 `pipeline.doc_classify`。

    惰性导入（与笔记那套建议 `_suggester` 一样）：这一层依赖模型客户端，
    而 `/roots`、`/items` 这些纯本地读的接口不该因为它而变慢或者起不来。
    """
    from pipeline import doc_classify  # noqa: PLC0415

    return doc_classify


@router.get("/kinds")
def kinds() -> dict:
    """类别表。**界面标签从这里取**，别在前端再抄一份 —— 抄一份就会有一天对不上。"""
    return {"kinds": [{"key": key, "label": lib.KIND_LABEL.get(key, key)} for key in lib.KINDS]}


@router.get("/classify/candidates")
def classify_candidates(user: CurrentUser, db: DbSession, limit: int = 24) -> dict:
    """哪些条目该归类（人定过、模型定过的不在里面）。只读清单，不调模型。"""
    roots = roots_for(_settings_row(db, user.id))
    module = _classifier()
    picked = module.pick_candidates(roots, _meta_dir(), _text_dir(), limit=max(1, min(limit, 100)))
    return {
        "items": [
            {k: one[k] for k in ("citekey", "rel", "title", "kind", "topics", "year")} for one in picked
        ],
        "count": len(picked),
    }


@router.post("/classify")
async def classify(body: dict, user: CurrentUser, db: DbSession) -> dict:
    """让模型判类型与主题。**只调模型、不落盘** —— 落盘要人点「接受」。"""
    roots = roots_for(_settings_row(db, user.id))
    citekeys = [str(item) for item in (body.get("citekeys") or []) if str(item).strip()]
    module = _classifier()
    try:
        return await module.classify(roots, _meta_dir(), _text_dir(), citekeys)
    except Exception as exc:  # noqa: BLE001 - 模型侧什么都可能抛（没配密钥、超时、额度）
        raise HTTPException(status_code=502, detail=f"模型没能给出归类：{str(exc)[:200]}") from exc


@router.post("/classify/apply")
def classify_apply(body: dict) -> dict:
    """把**被接受的那几条**写进元数据（记为 `origin: llm`）。

    人定过的（`manual`）在这里会被再挡一次 —— 一次模型调用不该冲掉人手填的东西。
    """
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items 得是个列表")
    return _run(_classifier().apply, _meta_dir(), items)


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
    for key, value in patch.items():
        if key == "citekey":
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            # `null` / 空串 = **把这个字段去掉**，而不是写一个空值进去。
            # 实测踩过：清空年份之后元数据里留下 `year: 0`，列表里于是显示"0 年"。
            merged.pop(key, None)
            continue
        merged[key] = value
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
        # **判据是"这份有没有冻结元数据"**，不是"抽出来空不空"，也不是"键在不在已知表里"。
        # 踩过两次：先是拿 `text_state != 'none'` 判，于是抽不出正文的 md / py 每批重抓；
        # 后来拿 `key in known` 判，于是**撞键被改名的那一份**永远对不上、每轮重抓一次
        # （"还剩 0"却永远抓不完）。`entry.frozen` 是列表那一步算好的、唯一可靠的判据。
        if entry.frozen and not force:
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
            if fresh["citekey"] != key:
                # 键定了就跟着挪缓存，别让"缓存键"和"条目键"分家
                # （分家的表现是：条目一直显示"没抓过"，队列反复重抓同几份）
                _run(lib.rename_text, _text_dir(), key, fresh["citekey"])
            # 走 freeze 而不是直接 save：它会在**撞键时去重**，
            # 否则两个条目会冻结成同一个键、后写的覆盖前一份，前者下一轮又变成"没抓过"
            frozen_meta = _run(lib.freeze, _meta_dir(), _text_dir(), fresh, entry.item.rel, origin="inferred")
            fresh = frozen_meta
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
