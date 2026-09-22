"""资料库的 HTTP 面。

三件必须说清的边界：

* **往资料根里写的只有三个动作，都得用户明确做**：新建文件夹（`/mkdir`）、
  移动条目（`/move`）、把拖进来的文件放进某个目录（`/upload`）。三者都钉在根内、
  都不覆盖同名（同名自动编号）。除此之外源目录只读 —— 元数据与派生缓存才随时可写。
* **元数据一冻结就不被覆盖**。索引动作只给"还没有元数据"的条目补一份，
  人改过的（`origin: manual`）与模型填过的（`llm`）一律不动 —— 不然跑一次索引
  就把人手工补的作者冲掉了，而且没有任何提示。
* **根目录清单落 `app_settings`**（单用户下的单行设置），文件本身不动。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import ai_gateway as gateway
from .. import attachments as attach
from .. import desktop
from .. import library as lib
from .. import library_meta
from .. import notelib
from ..deps import DbSession
from ..models import AppSettings
from ..settings_store import row as settings_row

# 前缀跟着全仓约定走 `/api/...`（别的路由也都是 `/api` 或 `/api/进度` 这种）。
# 写成 `/library` 的后果实测过：路由挂上了，但 `/api/library/roots` 一律 404。
router = APIRouter(prefix="/api/library", tags=["library"])

#: 设置里存根目录清单的键。
ROOTS_KEY = "library_roots"

#: 环境变量：用于测试与"我就是不想用默认根"的场合。
ROOTS_ENV = "QF_LIBRARY_ROOTS"


#: 一次 `/index` 里最多问几次模型。判元数据是一次网络往返（实测一秒左右一条），
#: 现在是**并发**发出去的，所以批量可以大一些：一批 12 条并发跑，通常两三秒。
#: 再大就没意义了 —— 前端是循环调用，一批一批推进反而看得见进度。
LLM_BATCH = 12

#: 判元数据时的并发宽度。上限 6：再高是拿上游的限流换速度（真被限流，整批更慢）。
LLM_WORKERS = 6


def _judge_meta(item: Any, head: str, conf: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    """问模型要这份资料的元数据。

    返回 `(元数据, 空串)` 或 `(机械兜底, 原因)`。**机械兜底里没有猜**：
    只有文件名派生的标题与扩展名决定的类型 —— 判不出来就让人去补，
    不拿一个错的作者填进去（它是引用格式、检索、去重的地基）。
    """
    plain: dict[str, Any] = {"title": lib._pretty(item.stem), "kind": item.kind}  # noqa: SLF001
    if conf is None:
        return plain, "还没配模型（设置 → AI），元数据先留空"
    try:
        return library_meta.infer(item, head, conf), ""
    except library_meta.MetaFailed as exc:
        return plain, exc.message


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


def roots_for(stored_settings: AppSettings | None) -> list[Path]:
    """用哪些根：设置里有就用设置里的（可以是空列表 = 明确不要任何根）。"""
    if (
        stored_settings is not None
        and isinstance(stored_settings.data, dict)
        and ROOTS_KEY in stored_settings.data
    ):
        stored = stored_settings.data.get(ROOTS_KEY) or []
        if isinstance(stored, list):
            return [Path(str(part)) for part in stored if str(part).strip()]
    return default_roots()


def _save_roots(db: DbSession, paths: list[Path]) -> None:
    row = settings_row(db, create=True)
    data = dict(row.data or {})
    data[ROOTS_KEY] = [str(path) for path in paths]
    row.data = data
    db.commit()


@router.get("/roots")
def roots(db: DbSession) -> dict:
    """当前根 + 默认根 + 规模。前端拿它画左栏的树。"""
    row = settings_row(db)
    active = roots_for(row)
    items = [item for root in active for item in lib.scan(root)]
    media = [path for root in active for path in lib.scan_media(root)]
    return {
        "roots": [str(path) for path in active],
        "default": [str(path) for path in default_roots()],
        "fromSettings": bool(row and isinstance(row.data, dict) and ROOTS_KEY in row.data),
        "items": len(items),
        # 附属资源不单独成条目，但要报个数：不然"图去哪儿了"没人答得上来
        "media": len(media),
        # 顺带报一句"文件处理这层缺什么"：界面初始化时就拿到了，
        # 抽不出文字时能直接说清原因（不用再问一次接口）
        "toolchain": attach.capabilities(),
        "missing": [str(path) for path in active if not path.is_dir()],
    }


@router.post("/roots")
def edit_roots(body: dict, db: DbSession) -> dict:
    """加一个根 / 去掉一个根。**只改清单，不动任何文件。**"""
    action = str(body.get("action") or "add")
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="得给一个目录")
    row = settings_row(db)
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
    _save_roots(db, current)
    return {"roots": [str(path) for path in current], "added": action == "add"}


def _root_of(roots: list[Path], target: Path) -> Path:
    """这个绝对路径属于哪个资料根。**不属于任何一个就拒绝** —— 写操作只能落在根里。"""
    resolved = Path(target).expanduser().resolve()
    for root in roots:
        base = Path(root).expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return Path(root)
    raise HTTPException(status_code=400, detail=f"这个位置不在资料根里：{target}")


@router.post("/mkdir")
def mkdir(body: dict, db: DbSession) -> dict:
    """新建文件夹。在**用户的真实目录**里真的建一个 —— 组织树就是磁盘上的目录树。"""
    dir_path = str(body.get("dir") or "").strip()
    if not dir_path:
        raise HTTPException(status_code=400, detail="得给一个目录")
    roots = roots_for(settings_row(db))
    root = _root_of(roots, Path(dir_path))
    return {"dir": _run(lib.mkdir, root, dir_path)}


@router.post("/move")
def move(body: dict, db: DbSession) -> dict:
    """把条目挪进另一个目录（左栏树上拖拽就是这个）。**真的移动文件**。"""
    path = str(body.get("path") or "").strip()
    to_dir = str(body.get("to") or "").strip()
    if not path or not to_dir:
        raise HTTPException(status_code=400, detail="得给 path 与 to")
    roots = roots_for(settings_row(db))
    root = _root_of(roots, Path(path))
    # 目标也要在同一根内，否则就是把文件挪出资料库
    _root_of(roots, Path(to_dir))
    return {"rel": _run(lib.move, root, path, to_dir)}


#: 一次上传的上限。超了就回滚删掉半个文件 —— 别把磁盘和内存一起吃掉。
MAX_UPLOAD_BYTES = 256 * 1024 * 1024


@router.post("/upload")
async def upload(
    db: DbSession,
    dir: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    """把**系统里拖进来的文件**放进指定目录 —— 真的落盘。

    为什么按目录而不是按"库"：用户的心智是"把这个 PDF 放进 cuda 那个文件夹"，
    与树上的位置一一对应。所以这里要的是**目标目录的绝对路径**（树上那个节点知道），
    再校验它确实在某个资料根里。

    流式写：一次 1MB。大 PDF 走这条路不会把整个文件读进内存。
    """
    raw = str(dir or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="得给一个目标目录")
    roots = roots_for(settings_row(db))
    root = _root_of(roots, Path(raw))
    folder = _run(lib.within, root, Path(raw))
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=f"这儿不是目录：{raw}")

    dest = _run(lib.unique_in, folder, _run(lib.safe_name, file.filename or ""))
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"这个文件太大了（上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB）",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)   # 别留半个文件在用户的目录里
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"写不进去：{exc}") from exc

    base = Path(root).expanduser().resolve()
    return {
        "name": dest.name,
        "path": str(dest),
        "rel": dest.relative_to(base).as_posix(),
        "bytes": written,
    }


@router.get("/items")
def items(db: DbSession, q: str = "", limit: int = 200) -> dict:
    """条目列表。带 `q` 时走**与笔记同一套表达式**检索。"""
    roots = roots_for(settings_row(db))
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


@router.get("/toolchain")
def toolchain_state() -> dict:
    """文件处理这层的外部工具现状（缺哪个、从哪来的）。只看，不动。"""
    return attach.capabilities()


@router.get("/kinds")
def kinds() -> dict:
    """类别表。**界面标签从这里取**，别在前端再抄一份 —— 抄一份就会有一天对不上。"""
    return {"kinds": [{"key": key, "label": lib.KIND_LABEL.get(key, key)} for key in lib.KINDS]}


@router.get("/classify/candidates")
def classify_candidates(db: DbSession, limit: int = 24) -> dict:
    """哪些条目该归类（人定过、模型定过的不在里面）。只读清单，不调模型。"""
    roots = roots_for(settings_row(db))
    module = _classifier()
    picked = module.pick_candidates(roots, _meta_dir(), _text_dir(), limit=max(1, min(limit, 100)))
    return {
        "items": [
            {k: one[k] for k in ("citekey", "rel", "title", "kind", "topics", "year")} for one in picked
        ],
        "count": len(picked),
    }


@router.post("/classify")
async def classify(body: dict, db: DbSession) -> dict:
    """让模型判类型与主题。**只调模型、不落盘** —— 落盘要人点「接受」。"""
    roots = roots_for(settings_row(db))
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
def item(citekey: str, db: DbSession, text: int = 0) -> dict:
    """一个条目的详情：元数据 + 附属资源（`files` 带角色）+ 正文质量 + **谁引用了它** + BibTeX。"""
    roots = roots_for(settings_row(db))
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
    # 契约里的 `files`：主文件 + 附属资源，各带 `role`。列表页只要数量，
    # 详情页要"这一条到底包含哪几个文件"，所以这里给全。
    detail["files"] = [
        {"path": str(entry.item.path), "role": "primary"},
        *({"path": str(path), "role": "asset"} for path in entry.assets),
    ]
    if text:
        state = entry.text_state
        detail["text"] = {
            "state": str(state.get("state") or "none"),
            "chars": int(state.get("chars") or 0),
            "ratio": float(state.get("ratio") or 0.0),
            "head": lib.text_of(_text_dir(), citekey, limit=max(200, min(text, 20000))),
        }
    return detail


def _entry_file(citekey: str, index: int, db):  # noqa: ANN001, ANN202
    """按引用键 + 序号取一个**条目里的文件**。

    序号 `-1` 是主文件，`>=0` 是第几个附属资源。**路径来自扫描结果，不来自请求参数** ——
    所以这里不存在"用户拼一个路径来读机器上任意文件"这回事（资料目录是只读的，
    但"只读"不等于"随便读"）。
    """
    roots = roots_for(settings_row(db))
    entry = next((one for one in lib.entries(roots, _meta_dir(), _text_dir()) if one.citekey == citekey), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"没有这个条目：{citekey}")
    if index < 0:
        path = entry.item.path
    elif 0 <= index < len(entry.assets):
        path = entry.assets[index]
    else:
        raise HTTPException(status_code=404, detail=f"这个条目没有第 {index} 个文件")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不在了：{path.name}")
    return entry, Path(path)


@router.get("/file")
def file_raw(citekey: str, db: DbSession, index: int = -1):
    """把条目里的一个文件原样给出去（PDF 与图片靠它显示）。

    `inline` 而不是 `attachment`：PDF 要在浏览器自带的阅读器里翻页，
    不该点一下就变成"下载"。字节流的 Range 由 `FileResponse` 处理 ——
    几百 MB 的规范书能直接跳到第 300 页，靠的就是它。
    """
    _entry, path = _entry_file(citekey, index, db)
    return FileResponse(
        path,
        media_type=attach.mime_of(path.name),
        filename=path.name,
        content_disposition_type="inline",
    )


@router.get("/view")
def file_view(citekey: str, db: DbSession, index: int = -1) -> dict:
    """这份文件该怎么看。

    返回 `kind` 与对应的内容：

    * `pdf` / `image` → 只给 `raw`（那个地址），前端用 iframe / img 显示；
    * `markdown` / `text` → 给 `text`，前端自己渲染（与笔记页同一套渲染器）；
    * `docx` → 给 `html`（pandoc 转的，带结构）；
    * `pptx` → 给 `slides`（每页的标题与要点）；
    * `legacy` / `binary` → 什么都不给，界面照实说看不了、给个下载。
    """
    entry, path = _entry_file(citekey, index, db)
    kind = attach.view_kind(path.name)
    out: dict = {
        "kind": kind,
        "name": path.name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "citekey": entry.citekey,
        "index": index,
        "raw": f"/api/library/file?citekey={entry.citekey}&index={index}",
    }
    if kind in ("markdown", "text"):
        try:
            out["text"] = path.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"读不动这个文件：{exc}") from exc
    elif kind == "docx":
        out["html"] = attach.to_html(path)
        if not out["html"]:
            out["note"] = "没转出内容：这台机器上没有 pandoc（或者这份文档是空的）。"
    elif kind == "pptx":
        out["slides"] = attach.slides_of(path)
        if not out["slides"]:
            out["note"] = "没解出幻灯片：这份 pptx 可能不是常规结构（或者它就是空的）。"
    elif kind == "legacy":
        out["note"] = "老式二进制格式（.doc / .ppt / .xls）抽不出文字，这个版本看不了。"
    elif kind == "binary":
        out["note"] = "这个格式没有内建查看器。"
    return out


@router.post("/meta")
def edit_meta(body: dict, db: DbSession) -> dict:
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
def index(body: dict, db: DbSession) -> dict:
    """抽一批正文并**冻结**元数据（只给还没有元数据的条目写）。

    分批是刻意的：全量抽取是这一层唯一的重 IO（实测 55 个 PDF、282MB），
    一次请求跑完会让界面一直转圈、也没法显示进度。前端按 `remaining` 循环调用。

    跳过判据是"**这份正文抽过没有**"（文本缓存里的 `at` 标记），**不是**"元数据冻结没有"。
    实测踩过：把两者当成一件事，于是手工补过作者年份的条目（`origin: manual`）
    被判成"已处理"，**永远不抽正文** —— 症状是那几份搜不到、正文面板一直是空的，
    而且怎么点"建索引"都没用。
    """
    limit = int(body.get("limit") or LLM_BATCH)
    force = bool(body.get("force"))
    # 判元数据要问模型：拿不到配置就**整批先不判**（正文照样抽，检索照样能用），
    # 并把原因原样带回去给界面显示 —— 沉默着不判，用户只会以为索引坏了。
    conf: dict[str, Any] | None
    hint = ""
    try:
        conf = gateway.resolve_config(db)
    except HTTPException as exc:
        conf, hint = None, str(exc.detail)
    if conf is not None:
        limit = max(1, min(limit, LLM_BATCH))
    roots = roots_for(settings_row(db))
    known = lib.load_all_metadata(_meta_dir())
    done: list[dict[str, Any]] = []
    remaining = 0
    # 分两步：先挑出这一批要处理的条目（挑的同时抽正文，那是本地 IO），
    # 再**并发**问模型。串行问的话，一批 12 条就是十几秒，界面只能一格一格挪。
    todo: list[tuple[Any, str, dict[str, Any], str]] = []
    for entry in lib.entries(roots, _meta_dir(), _text_dir()):
        key = entry.citekey
        # **判据是"这份正文抽过没有"**，不是"抽出来空不空"，也不是"键在不在已知表里"，
        # 更不是"元数据冻结没有"。四次取材的教训：
        # * 拿 `text_state != 'none'` 判 → 抽不出正文的 md / py 每批重抓；
        # * 拿 `key in known` 判 → **撞键被改名的那一份**永远对不上、每轮重抓（"还剩 0"却抓不完）；
        # * 拿 `entry.frozen`（元数据冻结）判 → 手工补过元数据的条目**永远不抽正文**（本轮实测）；
        # * `at` 才是那个标记本身：它区分的是"没抓过"与"抓过但没有正文"（`brief()` 里也这么用）。
        if entry.text_state.get("at") and not force:
            continue
        if len(todo) >= max(1, min(limit, 50)):
            remaining += 1
            continue
        state = _run(lib.extract_text, entry.item, _text_dir(), key, force=force)
        # **抽完再定键**：作者与年份就在正文里。踩过两次才写对 ——
        # 第一次是先定键（`arxiv220514135` 本可以是 `dao2022flashattention`），
        # 第二次是注释说要抽完再定、代码却仍然把键设成了抽之前的那个。
        todo.append((entry, key, state, lib.head_text_for(entry.item, _text_dir(), key)))

    judged_all = library_meta.judge_many([(entry.item, head) for entry, _k, _s, head in todo], conf, workers=LLM_WORKERS) \
        if conf is not None else [(entry.item, None, hint or "还没配模型") for entry, _k, _s, _h in todo]

    for (entry, key, state, _head), (_item, judged, why) in zip(todo, judged_all, strict=False):
        frozen = key in known
        fresh = dict(judged or {"title": lib._pretty(entry.item.stem), "kind": entry.item.kind})  # noqa: SLF001
        fresh.setdefault("source", str(entry.item.path))
        fresh["origin"] = "llm" if not why else "pending"
        if why:
            fresh["why"] = why
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
            frozen_meta = _run(
                lib.freeze, _meta_dir(), _text_dir(), fresh, entry.item.rel, origin=str(fresh["origin"])
            )
            fresh = frozen_meta
        done.append(
            {
                "citekey": fresh["citekey"],
                "was": key if fresh["citekey"] != key else "",
                "state": state.get("state"),
                "chars": state.get("chars"),
            }
        )
    return {"done": done, "remaining": remaining, "metaDir": str(_meta_dir()), "hint": hint}


@router.post("/pick-folder")
def pick_folder() -> dict:
    """弹一次**系统**文件夹选择器，把用户选中的目录给回来。

    路径由用户在本机选，服务端只把结果送回去 —— 客户端传来的路径不会当成"要打开的目录"
    去使用，它只是"要**加进清单**的那一个"（加清单要经过 `/roots` 的校验）。
    """
    return desktop.pick_folder()


@router.post("/meta/llm")
def meta_llm(body: dict, db: DbSession) -> dict:
    """用模型重判一批资料的元数据。

    为什么单独一个接口：`/index` 只管"还没有元数据的条目"，而这一次要修的是
    **已经写坏的那批**（用户："元数据提取这块你整个就是糊弄"）——
    它们的标题里混着目录行、作者里混着地名与出版社。

    三件必须钉住的事：

    * **citekey 一律不动**。它是身份，笔记里可能已经按它引用了（`referencing_notes`），
      重建等于断引用。键好不好看，比断引用次要得多。
    * **只动模型该判的那几个字段**：title / authors / year / kind / topics / origin。
      `source`、`assets`、人手工补过的（`origin: manual`）都不碰。
    * 判不出来的条目**保留原样**并把原因报回去，不写一个更差的进去。
    """
    conf: dict[str, Any] | None
    try:
        conf = gateway.resolve_config(db)
    except HTTPException as exc:
        raise HTTPException(503, str(exc.detail)) from exc

    scope = str(body.get("scope") or "pending")
    # 并发之后一批能给大些：默认 12 条，上限 40（前端循环调用，一批一批推进）
    limit = max(1, min(int(body.get("limit") or 12), 40))
    force = bool(body.get("force"))
    roots = roots_for(settings_row(db))
    meta_dir, text_dir = _meta_dir(), _text_dir()
    known = lib.load_all_metadata(meta_dir)

    picked: list[tuple[Any, dict[str, Any], str]] = []
    remaining = 0
    for entry in lib.entries(roots, meta_dir, text_dir):
        meta = dict(entry.meta or {})
        origin = str(meta.get("origin") or "")
        if origin == "manual" and not force:
            continue
        if scope != "all" and not library_meta.looks_unjudged(meta):
            continue
        if len(picked) >= limit:
            remaining += 1
            continue
        picked.append((entry, meta, lib.head_text_for(entry.item, text_dir, entry.citekey)))

    judged = library_meta.judge_many(
        [(entry.item, head) for entry, _meta, head in picked], conf, workers=LLM_WORKERS
    )
    changed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    # 落盘按原顺序单线程做：写文件是快的，顺序写让改动一眼看得清（并发写没有收益还有风险）
    for (entry, meta, _head), (_item, fresh, why) in zip(picked, judged, strict=False):
        key = entry.citekey
        if fresh is None:
            failed.append({"citekey": key, "why": why})
            continue
        for field in ("title", "authors", "year", "kind", "topics"):
            if fresh.get(field):
                meta[field] = fresh[field]
            elif field in ("authors", "topics"):
                meta[field] = []
        meta["origin"] = "llm"
        meta["metaRev"] = library_meta.META_REV
        meta["citekey"] = key                     # 身份不动
        meta.pop("why", None)
        lib.save_metadata(meta_dir, meta, origin="llm")
        changed.append({"citekey": key, "title": str(meta.get("title") or ""), "authors": meta.get("authors") or []})
    return {"changed": changed, "failed": failed, "remaining": remaining}


__all__ = ["default_roots", "roots_for", "router"]
