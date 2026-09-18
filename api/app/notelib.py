"""笔记库服务：目录、索引、写入与快照。

分工：`app/notes.py` 是**纯函数**（进出都是字符串，不碰文件）；这一层才是那个"状态" ——
库在哪、索引怎么缓存、写之前先快照、改名怎么把入链一起改。

权威仍在文件（见 `notes.py` 的模块注释）：这里没有表、没有隐藏状态 ——
索引是派生物，整个丢掉、重启重建，结果一样。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_settings
from .notes import (
    CANVAS_SUFFIX,
    GENERATED_KEY,
    MD_SUFFIX,
    build_index,
    detect_indent_unit,
    iter_inline_tags,
    iter_links,
    minimal_meta,
    outline,
    read_text,
    render,
    resolve,
    rewrite_links,
    splice_body,
    split_lines,
    split_text,
)

#: 每篇笔记留几份快照（文件恢复）。本地应用留够"手滑几次"的量就够 ——
#: 再多会变成一个没人管的黑洞目录
MAX_SNAPSHOTS = 50

#: 笔记根目录下的两个隐藏目录：快照（可撤销）与导入清单
SNAPSHOT_DIR = ".snapshots"
IMPORT_DIR = ".import"
#: 不该出现在笔记树里的目录
SKIP_DIRS = frozenset({SNAPSHOT_DIR, IMPORT_DIR, ".obsidian", ".trash", ".git", "__pycache__"})

NOTE_SUFFIXES = (MD_SUFFIX, CANVAS_SUFFIX)

#: 文件名里不许出现的东西（新建笔记时用标题当文件名）
_BAD_NAME = re.compile(r"[\\/:*?\"<>|\n\r\t]+")


class NoteError(Exception):
    """笔记库层面的错误（路径越界、写不动、重名）。路由层把它翻成 4xx。"""


class NoteNotFound(NoteError):
    pass


# ------------------------------------------------------------------ 结构


@dataclass
class Library:
    name: str
    root: Path
    notes: int = 0
    canvases: int = 0


@dataclass
class Ref:
    """一处引用。记下**在哪一行**，反链才能直接跳到那一行。"""

    target: str
    line: int
    snippet: str
    resolved: str | None = None
    #: `note` = 指向另一篇笔记（反链的来源）；`asset` = 指向附件；
    #: 空串 = 真的找不到 —— 那才是断链
    kind: str = ""


@dataclass
class Entry:
    """索引里的一篇笔记。"""

    rel: str
    title: str
    tags: list[str]
    refs: list[Ref]
    body: str
    meta: dict[str, Any]
    generated_header: bool
    mtime: float
    size: int
    indent_unit: str

    @property
    def resolved(self) -> list[str]:
        return [ref.resolved for ref in self.refs if ref.resolved]

    @property
    def unresolved(self) -> list[str]:
        return [ref.target for ref in self.refs if not ref.resolved]


@dataclass
class _Raw:
    """索引里的一行原始文件（`mtime`/`size` 用于判断要不要重读）。"""

    mtime: float
    size: int


# ------------------------------------------------------------------ 库


def notes_root() -> Path:
    """笔记根目录：数据目录下的 `notes/`（开发时是仓库的 `data/notes/`）。"""
    return get_settings().data_dir / "notes"


def safe_path(lib: Library, rel: str) -> Path:
    """把"库内相对路径"变成真实路径，并**挡住越界**。

    这条路径来自 HTTP 请求（`?path=../../etc/passwd`）。本地应用也不能开这个口子：
    一个手滑或恶意构造的请求就能读写数据目录外面的文件。所以解析之后必须仍在库内。
    """
    cleaned = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not cleaned:
        raise NoteError("路径不能为空")
    root = lib.root.resolve()
    candidate = (root / cleaned).resolve()
    if candidate != root and root not in candidate.parents:
        raise NoteError("路径越界（必须在库内）")
    return candidate


def _scan(root: Path) -> tuple[int, int]:
    notes = canvases = 0
    if not root.is_dir():
        return 0, 0
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() == MD_SUFFIX:
            notes += 1
        elif path.suffix.lower() == CANVAS_SUFFIX:
            canvases += 1
    return notes, canvases


def libraries() -> list[Library]:
    """有哪些库：笔记根目录下的一级子目录（`Math` / `Tech` / …）。"""
    root = notes_root()
    if not root.is_dir():
        return []
    found: list[Library] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        notes, canvases = _scan(path)
        found.append(Library(name=path.name, root=path, notes=notes, canvases=canvases))
    return found


def library(name: str) -> Library:
    for item in libraries():
        if item.name == name:
            return item
    raise NoteNotFound(f"没有这个库：{name}")


# ------------------------------------------------------------------ 索引


class Index:
    """一个库的内存索引：标题、标签、引用（→ 反链）、正文（→ 检索）。

    **按 `(mtime, size)` 增量重读**：改一篇笔记不该让整个库重算 ——
    1113 篇的库每次编辑都全量重读，界面会一顿一顿的。
    整套索引是派生物，丢掉重建的代价只是多读一遍文件。
    """

    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self._entries: dict[str, Entry] = {}
        self._raw: dict[str, _Raw] = {}
        self._built_at: float = 0.0

    # ---- 构建

    def refresh(self) -> None:
        everything = self._list_files()
        # 解析引用要用**全部**文件（含附件）：`![[1.4 极限的运算.pdf]]` 指向的是附件，
        # 只拿笔记当参照的话，25 条指向附件的引用会被报成"断链"—— 实测就是这么错的
        rels = set(everything)
        by_name = build_index(rels)
        notes = {rel: path for rel, path in everything.items() if path.suffix.lower() in NOTE_SUFFIXES}

        for rel, path in notes.items():
            stat = path.stat()
            known = self._raw.get(rel)
            if known and known.mtime == stat.st_mtime and known.size == stat.st_size:
                continue
            self._entries[rel] = self._build_entry(rel, path, by_name, rels)
            self._raw[rel] = _Raw(mtime=stat.st_mtime, size=stat.st_size)

        # 文件没了（改名 / 删除）就把它的条目也丢掉
        for rel in [item for item in self._entries if item not in notes]:
            del self._entries[rel]
            self._raw.pop(rel, None)

        # 别人的引用可能变了 → 已读过的那批按同样的解析规则重算引用关系。
        # （不重读文件内容，只重算"指向谁"：新增或改名的那篇会让旧引用有新的解析结果）
        #
        # **画布要单独走**：它的引用来自 JSON 里的 `file` 字段、不在正文里，
        # 拿 `_reparse_refs`（按正文扫）去重算会把它扫成空 —— 实测就是这么丢的：
        # 直接调 `_canvas_refs` 有结果，条目上却是空的。
        for rel, entry in self._entries.items():
            if entry.rel not in notes:
                continue
            if Path(entry.rel).suffix.lower() == CANVAS_SUFFIX:
                fresh = self._canvas_refs(notes[rel], by_name, rels)
            else:
                fresh = self._reparse_refs(entry, by_name, rels)
            if fresh != entry.refs:
                entry.refs = fresh

    def _list_files(self) -> dict[str, Path]:
        """库里**所有**文件，不只是笔记。

        解析引用必须拿这一份当参照：`![[1.4 极限的运算.pdf]]` 指向的是附件，
        只把笔记算进来，那些引用会被报成"断链" —— 实测 32 条里有 25 条是这种误报。
        笔记（`.md` / `.canvas`）才进索引，附件只参与"能不能解析到"。
        """
        files: dict[str, Path] = {}
        if not self.lib.root.is_dir():
            return files
        for path in sorted(self.lib.root.rglob("*")):
            if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
                continue
            files[path.relative_to(self.lib.root).as_posix()] = path
        return files

    def _build_entry(self, rel: str, path: Path, by_name: dict, rels: set) -> Entry:
        text = read_text(path)
        if path.suffix.lower() == CANVAS_SUFFIX:
            # 画布只记"存在 + 标题"，正文解析在 A3；但它指向笔记的卡片也算引用，
            # 否则"谁引用了这篇笔记"会漏掉画布 —— 而画布恰恰是最容易漏的那种引用
            return Entry(
                rel=rel,
                title=path.stem,
                tags=[],
                refs=self._canvas_refs(path, by_name, rels),
                body="",
                meta={},
                generated_header=False,
                mtime=path.stat().st_mtime,
                size=path.stat().st_size,
                indent_unit="",
            )
        note = split_text(text)
        entry = Entry(
            rel=rel,
            title=str(note.meta.get("title") or path.stem),
            tags=_tag_list(note.meta, note.body),
            refs=[],
            body=note.body,
            meta=note.meta,
            generated_header=bool(note.meta.get(GENERATED_KEY)),
            mtime=path.stat().st_mtime,
            size=path.stat().st_size,
            indent_unit=detect_indent_unit(note.body),
        )
        entry.refs = self._reparse_refs(entry, by_name, rels)
        return entry

    def _canvas_refs(self, path: Path, by_name: dict, rels: set) -> list[Ref]:
        """画布上指向笔记的卡片也算引用。

        否则"谁引用了这篇笔记"会漏掉画布 —— 而画布恰恰是最容易被忘掉的那种引用：
        它不在正文里，是一条 JSON 字段。
        """
        import json  # noqa: PLC0415

        try:
            payload = json.loads(read_text(path) or "{}")
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        refs: list[Ref] = []
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict) or not isinstance(node.get("file"), str):
                continue
            found = resolve(node["file"], by_name=by_name, rels=rels, current_dir="")
            refs.append(
                Ref(
                    target=node["file"],
                    line=0,
                    snippet="画布卡片",
                    resolved=found.rel,
                    kind="" if not found.rel else (
                        "note" if found.rel.endswith(NOTE_SUFFIXES) else "asset"
                    ),
                )
            )
        return refs


    def _reparse_refs(self, entry: Entry, by_name: dict, rels: set) -> list[Ref]:
        current_dir = str(Path(entry.rel).parent)
        refs: list[Ref] = []
        for index, line in enumerate(entry.body.splitlines()):
            for link in iter_links(line):
                found = resolve(
                    link.target, by_name=by_name, rels=rels, current_dir=current_dir
                )
                refs.append(
                    Ref(
                        target=link.target,
                        line=index,
                        snippet=line.strip()[:160],
                        resolved=found.rel,
                        kind="" if not found.rel else (
                            "note" if found.rel.endswith(NOTE_SUFFIXES) else "asset"
                        ),
                    )
                )
        return refs

    # ---- 查询

    def invalidate(self, rel: str = "") -> None:
        """丢掉缓存（不带参数 = 全丢）。

        为什么不是"只丢这一篇"就够：改名的连带影响面跨文件，而重读一遍 91 篇
        是毫秒级的事 —— 为省这点开销引入一个"谁该失效"的推理，不值。
        """
        if rel:
            self._raw.pop(rel, None)
        else:
            self._raw.clear()

    def entries(self) -> list[Entry]:
        return [self._entries[rel] for rel in sorted(self._entries)]

    def entry(self, rel: str) -> Entry | None:
        return self._entries.get(rel)

    def backlinks(self, rel: str) -> list[dict[str, Any]]:
        """谁引用了我 —— 幕布那边没有、Obsidian 那边最值钱的那一面。"""
        found: list[dict[str, Any]] = []
        for entry in self.entries():
            for ref in entry.refs:
                if ref.resolved == rel:
                    found.append(
                        {
                            "path": entry.rel,
                            "title": entry.title,
                            "line": ref.line,
                            "snippet": ref.snippet,
                        }
                    )
        return found

    def outlinks(self, rel: str) -> list[dict[str, Any]]:
        entry = self._entries.get(rel)
        if entry is None:
            return []
        out: list[dict[str, Any]] = []
        for ref in entry.refs:
            target = self._entries.get(ref.resolved or "")
            out.append(
                {
                    "target": ref.target,
                    "line": ref.line,
                    "resolved": ref.resolved,
                    "kind": ref.kind or "missing",
                    "title": target.title if target else None,
                }
            )
        return out

    def tag_counts(self) -> list[dict[str, Any]]:
        counts: dict[str, list[str]] = {}
        for entry in self.entries():
            for tag in entry.tags:
                counts.setdefault(tag, []).append(entry.rel)
        return [
            {"tag": tag, "count": len(paths), "notes": sorted(paths)[:100]}
            for tag, paths in sorted(counts.items(), key=lambda item: (-len(item[1]), item[0]))
        ]

    def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """全文检索：标题命中排在正文命中前面。

        没做倒排索引 —— 1113 篇、单用户、点一下搜一次，直接扫全文就够快；
        先上倒排会把"索引与文件不同步"这类 bug 引进来，而收益在这个规模上是零。
        """
        needle = query.strip().lower()
        if not needle:
            return []
        title_hits: list[dict[str, Any]] = []
        body_hits: list[dict[str, Any]] = []
        for entry in self.entries():
            if needle in entry.title.lower():
                title_hits.append(
                    {
                        "path": entry.rel,
                        "title": entry.title,
                        "line": 0,
                        "snippet": entry.title,
                        "where": "标题",
                    }
                )
            for index, line in enumerate(entry.body.splitlines()):
                if needle in line.lower():
                    body_hits.append(
                        {
                            "path": entry.rel,
                            "title": entry.title,
                            "line": index,
                            "snippet": line.strip()[:160],
                            "where": "正文",
                        }
                    )
                    break
            if len(title_hits) + len(body_hits) >= limit * 4:
                break
        return (title_hits + body_hits)[:limit]


_INDEXES: dict[str, Index] = {}
_LOCK = threading.RLock()


def index(lib: Library) -> Index:
    """拿到（并刷新）一个库的索引。"""
    with _LOCK:
        found = _INDEXES.get(lib.name)
        if found is None or found.lib.root != lib.root:
            found = Index(lib)
            _INDEXES[lib.name] = found
    found.refresh()
    return found


def reset_index() -> None:
    """把索引全丢掉（测试用；也是"索引坏了"时的兜底手段）。"""
    with _LOCK:
        _INDEXES.clear()


# ------------------------------------------------------------------ 快照


def _snapshot_dir(lib: Library, rel: str) -> Path:
    return notes_root() / SNAPSHOT_DIR / lib.name / Path(rel)


def snapshot(lib: Library, rel: str, data: bytes, *, why: str = "") -> str | None:
    """写之前先把内容存一份。返回快照文件名。

    为什么快照要带 `why`：撤销的时候至少能看出"这一版是什么原因被换掉的"，
    比一个光秃秃的时间戳有用。

    空文件也要存 —— 一篇空笔记被写进内容，撤销的锚点正是那个"空"。
    """
    folder = _snapshot_dir(lib, rel)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"{stamp}{'-' + why if why else ''}.snap"
    (folder / name).write_bytes(data)
    _rotate(folder)
    return name


def _rotate(folder: Path) -> None:
    items = sorted(folder.glob("*.snap"))
    for old in items[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)


def snapshots(lib: Library, rel: str) -> list[dict[str, Any]]:
    folder = _snapshot_dir(lib, rel)
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.snap"), reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size": stat.st_size,
                "why": path.stem.split("-", 3)[-1] if path.stem.count("-") >= 3 else "",
            }
        )
    return out


def undo(lib: Library, rel: str) -> dict[str, Any]:
    """把最近一次改动撤回。

    撤回之前**先给当前状态存一份** —— 于是"撤销"本身也能被撤销：
    点错了不会把内容一次性抹掉，这是文件恢复该有的样子。
    """
    folder = _snapshot_dir(lib, rel)
    items = sorted(folder.glob("*.snap")) if folder.is_dir() else []
    if not items:
        raise NoteError("这篇笔记还没有可撤销的改动")
    path = safe_path(lib, rel)
    latest = items[-1]
    restored = latest.read_bytes()
    if path.is_file():
        snapshot(lib, rel, path.read_bytes(), why="undo")
    _write_atomic(path, restored)
    latest.unlink(missing_ok=True)
    _invalidate(lib)
    return {"restored": latest.name, "bytes": len(restored)}


# ------------------------------------------------------------------ 写


def _write_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再改名：中途失败不会留下半份笔记（笔记是权威，不能有半份）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _invalidate(lib: Library) -> None:
    """让索引下次刷新时重读（时间戳可能同秒 —— 靠 mtime 判断会漏掉刚写完的那篇）。"""
    with _LOCK:
        found = _INDEXES.get(lib.name)
    if found is not None:
        found.invalidate()


def ensure_note(lib: Library, rel: str) -> tuple[Path, str, Any]:
    """读一篇笔记的原文与解析结果（写操作都要先过这道）。"""
    path = safe_path(lib, rel)
    if not path.is_file():
        raise NoteNotFound(f"没有这篇笔记：{rel}")
    if path.suffix.lower() != MD_SUFFIX:
        raise NoteError(f"这个操作只支持 Markdown 笔记（当前是 {path.suffix}）")
    text = read_text(path)
    note = split_text(text)
    if note.broken_header:
        raise NoteError("这篇笔记的元数据头没有闭合，先修好再编辑（免得越改越乱）")
    return path, text, note


def write_body(lib: Library, rel: str, body: str, *, why: str) -> dict[str, Any]:
    path, text, note = ensure_note(lib, rel)
    if note.had_header:
        new_text = splice_body(text, body)
    else:
        from .notes import render as _render  # noqa: PLC0415

        new_text = _render(minimal_meta(body, path.stem), body)
    snapshot(lib, rel, text.encode("utf-8"), why=why)
    _write_atomic(path, new_text.encode("utf-8"))
    _invalidate(lib)
    return read_note(lib, rel)


def set_meta(lib: Library, rel: str, patch: dict[str, Any]) -> dict[str, Any]:
    """改元数据头（标题 / 标签 / 自定义属性）。

    这里**必须重新 dump 那个头**（改的就是它），所以只动这一块，正文一字不碰。
    """
    path, text, note = ensure_note(lib, rel)
    meta = dict(note.meta)
    for key, value in patch.items():
        if value in (None, ""):
            meta.pop(key, None)
        else:
            meta[key] = value
    snapshot(lib, rel, text.encode("utf-8"), why="meta")
    new_text = render(meta, note.body)
    _write_atomic(path, new_text.encode("utf-8"))
    _invalidate(lib)
    return read_note(lib, rel)


def _sibling_line(body: str, index: int) -> str:
    """幕布式回车：在目标行下面新建一个"同级空条目"。

    规则跟着目标行长什么样：
    * 项目符号 → 同样的缩进 + 同样的符号（有序列表统一用 `-`，不猜下一个编号）；
    * 标题 → 新建一个比它低一层的条目（标题下面当然是内容，不是另一个同级标题）；
    * 其它（段落等）→ 同样缩进的一个空行。
    """
    lines = body.split("\n")
    if index < 0:
        return ""
    raw = lines[index] if 0 <= index < len(lines) else ""
    header = re.match(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])(?P<space>\s+)", raw)
    if header:
        return f"{header.group('indent')}- "
    if re.match(r"^#{1,6}\s+", raw):
        return "- "
    indent = raw[: len(raw) - len(raw.lstrip(" \t"))]
    return indent


def _move(body: str, index: int, delta: int) -> str:
    """上/下挪一块（幕布的 Alt+↑ / Alt+↓）：与相邻的兄弟块交换位置。

    刻意**不暴露** "挪到第几行" 那种接口：那要求调用方自己算子树边界，算错一次就把内容
    挪进别的条目里。这里只收 ±1 —— 少一种用法，就没有那种错法。
    """
    from .notes import block_extent, move_block, split_lines  # noqa: PLC0415

    lines, _ = split_lines(body)
    if not 0 <= index < len(lines):
        raise NoteError(f"第 {index} 行不存在（共 {len(lines)} 行）")
    _, end = block_extent(body, index)
    if delta > 0:
        if end >= len(lines):
            return body                      # 已经是最后一块，没得换
        return move_block(body, index, block_extent(body, end)[1])
    base = len(lines[index]) - len(lines[index].lstrip(" \t"))
    cursor = index - 1
    while cursor >= 0:
        raw = lines[cursor]
        if raw.strip() and len(raw) - len(raw.lstrip(" \t")) <= base:
            break
        cursor -= 1
    return body if cursor < 0 else move_block(body, index, cursor)


def line_op(
    lib: Library,
    rel: str,
    *,
    op: str,
    index: int,
    raw: str = "",
    delta: int = 0,
) -> dict[str, Any]:
    """行级操作 —— 幕布那半边的核心（回车同级、Tab 缩进、Alt+↑ 挪块）。"""
    from .notes import delete_line, insert_line, move_block, replace_line, shift_line  # noqa: PLC0415

    path, text, note = ensure_note(lib, rel)
    body = note.body
    if op == "insert":
        body = insert_line(body, index, raw or _sibling_line(body, index))
    elif op == "replace":
        body = replace_line(body, index, raw)
    elif op == "delete":
        body = delete_line(body, index)
    elif op in ("indent", "outdent"):
        body = shift_line(body, index, 1 if op == "indent" else -1, detect_indent_unit(body))
    elif op == "move":
        body = _move(body, index, delta or 1)
    else:
        raise NoteError(f"不认识的编辑操作：{op}")

    new_text = splice_body(text, body) if note.had_header else render(
        minimal_meta(body, path.stem), body
    )
    snapshot(lib, rel, text.encode("utf-8"), why=op)
    _write_atomic(path, new_text.encode("utf-8"))
    _invalidate(lib)
    return read_note(lib, rel)


def create_note(lib: Library, folder: str, title: str) -> dict[str, Any]:
    """新建一篇笔记（「未解析链接」一键补齐也走这里）。"""
    clean = _BAD_NAME.sub(" ", title).strip()
    if not clean:
        raise NoteError("标题不能为空")
    folder_rel = (folder or "").replace("\\", "/").strip("/")
    rel = f"{folder_rel}/{clean}.md" if folder_rel else f"{clean}.md"
    path = safe_path(lib, rel)
    if path.exists():
        raise NoteError(f"已经有一篇叫这个的了：{rel}")
    # 新建的笔记不标 `qf_generated`：头是应用写的，但内容是作者写的 ——
    # 那个标记的语义是"这个头是导入器补的"，别把它稀释掉
    meta: dict[str, Any] = {"title": clean, "tags": []}
    _write_atomic(path, render(meta, "").encode("utf-8"))
    _invalidate(lib)
    return read_note(lib, rel)


# ------------------------------------------------------------------ 改名


def _link_style(target: str, new_rel: str) -> str:
    """照原样保留链接的写法：原来只写文件名就还只写文件名，原来带路径就带路径。

    不这么做的话，一次改名会把整篇笔记里的链接全"升级"成完整路径 ——
    diff 里一片红，用户下次就不敢用改名了。
    """
    bare = new_rel[:-3] if new_rel.endswith(MD_SUFFIX) else new_rel
    if Path(target).suffix:
        return new_rel
    return bare if "/" in target else Path(bare).name


def rename_note(lib: Library, rel: str, new_rel: str) -> dict[str, Any]:
    """改名，并把**引用它的地方一起改**（旧编辑器 `alwaysUpdateLinks: true` 那条）。

    顺序是刻意的：**先写引用者、最后才动文件名**。反过来的话，中途失败会留下一堆
    指向不存在文件的链接；而这个顺序最坏也只是"链接都改好了、文件还没改名"，
    再跑一次就对上了。
    """
    old_path = safe_path(lib, rel)
    new_path = safe_path(lib, new_rel)
    if not old_path.is_file():
        raise NoteNotFound(f"没有这篇笔记：{rel}")
    if new_path.exists():
        raise NoteError(f"目标已存在：{new_rel}")

    found = index(lib)
    old_stem = Path(rel).stem
    plan: list[tuple[str, str, str]] = []       # (rel, 旧文本, 新文本)
    for entry in found.entries():
        if entry.rel == rel or not entry.refs:
            continue
        path = safe_path(lib, entry.rel)
        text = read_text(path)

        def replace_target(target: str) -> str | None:
            # 认两种写法：按路径写的（`data/x/旧名`）与按名字写的（`[[旧名]]`）。
            # 只比较**文件名那一截** —— 库内同名文件本来就少见，而指望链接里写全路径不现实
            if Path(target).stem != old_stem and target not in (rel, Path(rel).name):
                return None
            return _link_style(target, new_rel)

        new_text, count = rewrite_links(text, replace_target)
        if count:
            plan.append((entry.rel, text, new_text))

    written: list[tuple[str, str]] = []
    try:
        for other, text, new_text in plan:
            snapshot(lib, other, text.encode("utf-8"), why="rename")
            _write_atomic(safe_path(lib, other), new_text.encode("utf-8"))
            written.append((other, text))
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
    except OSError as exc:
        # 把已经改过的引用者还原回去：宁可"什么都没发生"，也不要留一堆断链
        for other, text in written:
            _write_atomic(safe_path(lib, other), text.encode("utf-8"))
        raise NoteError(f"改名失败，已回滚：{exc}") from exc

    _invalidate(lib)
    return {
        "from": rel,
        "to": new_rel,
        "updated_notes": [item[0] for item in written],
        "note": read_note(lib, new_rel),
    }


# ------------------------------------------------------------------ 读


def _tag_list(meta: dict[str, Any], body: str) -> list[str]:
    """标签 = 元数据头里的 `tags` + 正文里的 `#标签`（Obsidian 两种写法都认）。"""
    out: list[str] = []
    raw = meta.get("tags")
    if isinstance(raw, str):
        out.extend(part.strip() for part in raw.split(",") if part.strip())
    elif isinstance(raw, (list, tuple)):
        out.extend(str(item).strip() for item in raw if str(item).strip())
    out.extend(iter_inline_tags(body))
    seen: dict[str, None] = {}
    for tag in out:
        tag = tag.strip().lstrip("#")
        if tag and tag not in seen:
            seen[tag] = None
    return list(seen)


def read_note(lib: Library, rel: str) -> dict[str, Any]:
    """读一篇笔记：正文 + 大纲 + 引用 + 反链，一次给全（前端一次渲染不用来回问）。"""
    path = safe_path(lib, rel)
    if not path.is_file():
        raise NoteNotFound(f"没有这篇笔记：{rel}")

    found = index(lib)
    if path.suffix.lower() == CANVAS_SUFFIX:
        entry = found.entry(rel)
        return {
            "lib": lib.name,
            "path": rel,
            "kind": "canvas",
            "title": path.stem,
            "body": "",
            "outline": [],
            "tags": [],
            "meta": {},
            "generated_header": False,
            "linked": [ref.target for ref in (entry.refs if entry else [])],
            "backlinks": found.backlinks(rel),
            "unresolved": [],
            "can_undo": bool(snapshots(lib, rel)),
            "snapshots": len(snapshots(lib, rel)),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "message": "画布（Canvas）的渲染与编辑在下一步做，这里先作为笔记树里的一等条目出现",
        }

    text = read_text(path)
    note = split_text(text)
    entry = found.entry(rel)
    if entry is None:
        # 索引刚被清过（比如刚写完）—— 就地重算一条，免得界面拿到空数据
        found.refresh()
        entry = found.entry(rel)

    body = note.body
    return {
        "lib": lib.name,
        "path": rel,
        "kind": "note",
        "title": str(note.meta.get("title") or path.stem),
        "meta": note.meta,
        "tags": _tag_list(note.meta, body),
        "generated_header": bool(note.meta.get(GENERATED_KEY)),
        "broken_header": note.broken_header,
        "body": body,
        "outline": [asdict(item) for item in outline(body)],
        "lines": len(split_lines(body)[0]),
        "indent_unit": detect_indent_unit(body),
        "outlinks": found.outlinks(rel),
        "backlinks": found.backlinks(rel),
        "unresolved": entry.unresolved if entry else [],
        "snapshots": len(snapshots(lib, rel)),
        "can_undo": bool(snapshots(lib, rel)),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "bytes": path.stat().st_size,
    }


# ------------------------------------------------------------------ 树 / 检索


def tree(lib: Library) -> dict[str, Any]:
    """笔记树：目录 + 文件（带标题与标签，前端不用再逐篇问）。"""
    found = index(lib)
    root: dict[str, Any] = {"name": lib.name, "path": "", "dirs": {}, "files": []}
    for entry in found.entries():
        parts = Path(entry.rel).parts
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(
                part, {"name": part, "path": "", "dirs": {}, "files": []}
            )
        node["files"].append(
            {
                "path": entry.rel,
                "name": parts[-1],
                "title": entry.title,
                "kind": "canvas" if entry.rel.endswith(CANVAS_SUFFIX) else "note",
                "tags": entry.tags,
                "mtime": entry.mtime,
            }
        )

    def finalize(node: dict[str, Any], prefix: str) -> dict[str, Any]:
        here = f"{prefix}/{node['name']}" if prefix else node["name"]
        dirs = [finalize(child, here) for _, child in sorted(node["dirs"].items())]
        files = sorted(node["files"], key=lambda item: item["path"])
        return {
            "name": node["name"],
            "path": here if prefix or node["name"] != lib.name else "",
            "dirs": dirs,
            "files": files,
            "count": len(files) + sum(child["count"] for child in dirs),
        }

    return finalize(root, "")


def search(query: str, *, lib_name: str = "", limit: int = 50) -> list[dict[str, Any]]:
    targets = [library(lib_name)] if lib_name else libraries()
    out: list[dict[str, Any]] = []
    for item in targets:
        for hit in index(item).search(query, limit=limit):
            out.append({"lib": item.name, **hit})
            if len(out) >= limit:
                return out
    return out


def tag_list(lib_name: str = "") -> list[dict[str, Any]]:
    targets = [library(lib_name)] if lib_name else libraries()
    out: list[dict[str, Any]] = []
    for item in targets:
        for row in index(item).tag_counts():
            out.append({"lib": item.name, **row})
    return sorted(out, key=lambda row: (-row["count"], row["tag"]))


def stats() -> dict[str, Any]:
    """笔记模块的体检数字：几个库、多少篇、多少断链（界面上一眼看清）。"""
    libs = libraries()
    total_unresolved = 0
    total_links = 0
    for item in libs:
        for entry in index(item).entries():
            total_links += len(entry.refs)
            total_unresolved += len(entry.unresolved)
    return {
        "root": str(notes_root()),
        "libraries": [asdict(item) for item in libs],
        "notes": sum(item.notes for item in libs),
        "canvases": sum(item.canvases for item in libs),
        "links": total_links,
        "unresolved": total_unresolved,
    }


__all__ = [
    "MAX_SNAPSHOTS",
    "Entry",
    "Library",
    "NoteError",
    "NoteNotFound",
    "Ref",
    "create_note",
    "index",
    "libraries",
    "library",
    "line_op",
    "notes_root",
    "read_note",
    "rename_note",
    "reset_index",
    "safe_path",
    "search",
    "set_meta",
    "snapshot",
    "snapshots",
    "stats",
    "tag_list",
    "tree",
    "undo",
    "write_body",
]
