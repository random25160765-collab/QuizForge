"""笔记库服务：目录、索引、写入与快照。

分工：`app/notes.py` 是**纯函数**（进出都是字符串，不碰文件）；这一层才是那个"状态" ——
库在哪、索引怎么缓存、写之前先快照、改名怎么把入链一起改。

权威仍在文件（见 `notes.py` 的模块注释）：这里没有表、没有隐藏状态 ——
索引是派生物，整个丢掉、重启重建，结果一样。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
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
    parse_query,
    read_text,
    render,
    resolve,
    rewrite_links,
    search_row,
    splice_body,
    split_lines,
    split_text,
)

#: 每篇笔记留几份快照（文件恢复）。本地应用留够"手滑几次"的量就够 ——
#: 再多会变成一个没人管的黑洞目录
MAX_SNAPSHOTS = 50

#: 笔记根目录下的隐藏目录：快照（可撤销）、导入清单、回收站
SNAPSHOT_DIR = ".snapshots"
IMPORT_DIR = ".import"
TRASH_DIR = ".trash"
#: 不该出现在笔记树里的目录
SKIP_DIRS = frozenset({SNAPSHOT_DIR, IMPORT_DIR, ".obsidian", ".trash", ".git", "__pycache__"})



#: 大纲：**纯文本的缩进项目符号**，一行一个节点，没有 YAML 头。
#: 为什么单独一种后缀而不是 `.md` 里打个标记：这样树里一眼分得清、
#: 打开就知道该用哪套视图，而且解析完全不依赖"头有没有写坏"。
OUTLINE_SUFFIX = ".outline"

NOTE_SUFFIXES = (MD_SUFFIX, CANVAS_SUFFIX, OUTLINE_SUFFIX)

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
        """检索：表达式（`#标签` / `title:` / `-词` / `AND` `OR` / `orderby` / `limit`）+ 全文。

        没做倒排索引 —— 单用户、点一下搜一次，直接扫就够快；先上倒排会把
        "索引与文件不同步"这类 bug 引进来，而收益在这个规模上是零。
        """
        parsed = parse_query(query or "")
        if not parsed.text.strip():
            return []
        limit = parsed.limit or limit
        needle = _plain_needle(parsed.text)
        rows: list[tuple[dict[str, Any], Entry]] = []
        for entry in self.entries():
            row = search_row(
                entry.rel,
                entry.title,
                entry.body,
                entry.tags,
                kind=kind_of(entry.rel),
                modified=entry.mtime,
                size=entry.size,
                links=len(entry.refs),
                unresolved=len(entry.unresolved),
                generated=entry.generated_header,
                archived=bool(entry.meta.get("archived")),
            )
            if parsed.match(row):
                rows.append((row, entry))

        if parsed.order:
            rows.sort(key=lambda pair: parsed.sort_key(pair[0]), reverse=parsed.desc)
        elif needle:
            # 纯文本查询：标题命中排前面（表达式查询有自己的排序，不插一脚）
            rows.sort(key=lambda pair: (0 if needle in pair[1].title.lower() else 1, pair[1].rel))
        else:
            rows.sort(key=lambda pair: pair[1].rel)

        hits: list[dict[str, Any]] = []
        for row, entry in rows[:limit]:
            line, snippet, where = 0, entry.title, "标题"
            if needle and needle not in entry.title.lower():
                for index, text in enumerate(entry.body.splitlines()):
                    if needle in text.lower():
                        line, snippet, where = index, text.strip()[:160], "正文"
                        break
            if where == "标题" and not needle:
                snippet = _first_meaningful_line(entry.body) or entry.title
                where = "整篇命中"
            hits.append(
                {
                    "path": entry.rel,
                    "title": entry.title,
                    "line": line,
                    "snippet": snippet,
                    "where": where,
                }
            )
        return hits


_INDEXES: dict[str, Index] = {}
_LOCK = threading.RLock()


def link_graph(lib: Library) -> dict[str, Any]:
    """文档图谱：一篇笔记一个节点、一条双链一条边。

    直接读索引里现成的 `refs` —— 那里面已经存着每处引用**解析到谁**了，
    不重新解析正文。一千多篇的量级下，这一点决定了它是"点开就有"还是"等十秒"。
    只连**同库内、真实存在**的目标：断链与被引到别的库的不画进来（后者会突然
    多出一片孤立点，反而看不清结构）。
    """
    idx = index(lib)
    entries = idx.entries()
    known = {entry.rel for entry in entries}
    nodes = [
        {
            "id": entry.rel,
            "title": entry.title,
            "tags": list(entry.tags),
            "out": len([ref for ref in entry.refs if ref.kind == "note"]),
        }
        for entry in entries
    ]
    edges: list[dict[str, str]] = []
    for entry in entries:
        for ref in entry.refs:
            target = ref.resolved
            if ref.kind == "note" and target and target in known and target != entry.rel:
                edges.append({"source": entry.rel, "target": target})
    return {"lib": lib.name, "nodes": nodes, "edges": edges}


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


# ------------------------------------------------------------------ 快照 / 历史
#
# 模型：**历史 = 所有版本，游标 = 当前那一版**。
#
# 早先写的是"每次改动前存一份、撤销时取最新那份"，看着等价，实测不成立：
# 撤销自己也会存一份，于是"最新那份"永远是刚撤掉的那版 —— 再点就在最后两个状态
# 之间来回跳，连撤 11 次都退不回原样。改成"记下每一版 + 一个游标"之后，
# 撤销就是"把游标往回挪一格"，再挪一格还能继续往回。


def _natural_key(text: str) -> list[Any]:
    """自然序排序键：把数字段当数字比（`10` 排在 `2` 后面，而不是前面）。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def _plain_needle(text: str) -> str:
    """纯文本查询返回那段词，否则空串。

    "标题命中排前面"只对纯文本查询有意义 —— 表达式查询有自己的 `orderby`，别去插一脚。
    """
    lowered = (text or "").strip().lower()
    if not lowered or lowered.startswith("-"):
        return ""
    if any(mark in lowered for mark in ("#", ":", "(", ")", " and ", " or ", " not ")):
        return ""
    return lowered


def _first_meaningful_line(body: str) -> str:
    for line in (body or "").splitlines():
        text = line.strip().lstrip("#> -*`").strip()
        if text:
            return text[:160]
    return ""


def _snapshot_dir(lib: Library, rel: str) -> Path:
    return notes_root() / SNAPSHOT_DIR / lib.name / Path(rel)


def _seq_of(path: Path) -> int:
    """版本号（文件名开头那串数字）。老格式或别的文件返回 -1，直接忽略。"""
    head = path.name.split("-", 1)[0]
    return int(head) if head.isdigit() else -1


def _cursor_file(folder: Path) -> Path:
    return folder / "current.json"


def _read_cursor(folder: Path) -> int:
    try:
        payload = json.loads(_cursor_file(folder).read_text(encoding="utf-8"))
        return int(payload.get("seq") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _write_cursor(folder: Path, seq: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    _cursor_file(folder).write_text(json.dumps({"seq": seq}), encoding="utf-8")


def _write_version(folder: Path, seq: int, data: bytes, why: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"{max(seq, 0):05d}-{why or 'save'}-{stamp}.snap"
    (folder / name).write_bytes(data)
    _rotate(folder)


def _rotate(folder: Path) -> None:
    """只留最近 MAX_SNAPSHOTS 版。按**版本号**排序，不靠文件名里的时间戳。"""
    items = [path for path in folder.glob("*.snap") if _seq_of(path) >= 0]
    for old in sorted(items, key=_seq_of)[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)


def _save(lib: Library, rel: str, path: Path, text: str, *, why: str) -> None:
    """写一份新内容，并把每一版都记进历史。

    第一次改动时把**改动前**那一版也补记进去，否则撤不回原始内容。
    """
    folder = _snapshot_dir(lib, rel)
    folder.mkdir(parents=True, exist_ok=True)
    cursor = _read_cursor(folder)
    if not [item for item in folder.glob("*.snap") if _seq_of(item) >= 0] and path.is_file():
        _write_version(folder, cursor, path.read_bytes(), "原始")
        cursor += 1
    _write_atomic(path, text.encode("utf-8"))
    _write_version(folder, cursor + 1, text.encode("utf-8"), why)
    _write_cursor(folder, cursor + 1)


def save_raw(lib: Library, rel: str, text: str, *, why: str = "编辑") -> None:
    """写一份**非 Markdown** 的正文（画布用）。

    与 `write_body` 共用快照与轮转，只是**不走 YAML 头那套拼装** —— 画布是 JSON，
    拼个头会把文件写坏。所以这里是"原样写 + 照例留版本"。
    """
    path = safe_path(lib, rel)
    if not path.is_file():
        raise NoteNotFound(f"没有这篇：{rel}")
    _save(lib, rel, path, text, why=why)
    _invalidate(lib)


def snapshots(lib: Library, rel: str) -> list[dict[str, Any]]:
    """改动历史（新→旧），并标出"哪一版是当前这一版"。"""
    folder = _snapshot_dir(lib, rel)
    if not folder.is_dir():
        return []
    cursor = _read_cursor(folder)
    out: list[dict[str, Any]] = []
    for path in sorted(
        [item for item in folder.glob("*.snap") if _seq_of(item) >= 0],
        key=_seq_of,
        reverse=True,
    ):
        stat = path.stat()
        parts = path.stem.split("-", 2)
        out.append(
            {
                "name": path.name,
                "seq": _seq_of(path),
                "at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size": stat.st_size,
                "why": parts[1] if len(parts) > 2 else "",
                "current": _seq_of(path) == cursor,
                "named": False,
            }
        )
    # 命名快照（手动"存一版"）排在后面：它们不参与游标、也不被轮转清理
    for path in sorted(folder.glob("named-*.snap"), reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "seq": -1,
                "at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size": stat.st_size,
                "why": path.stem.split("-", 2)[1] if path.name.count("-") >= 2 else "命名",
                "current": False,
                "named": True,
            }
        )
    return out


def history_state(lib: Library, rel: str) -> dict[str, Any]:
    """自动版本的状态。**命名快照不算在内** —— 否则"存一版留档"会让"可撤销"永远为真。"""
    items = [item for item in snapshots(lib, rel) if not item["named"]]
    cursor = next((item["seq"] for item in items if item["current"]), -1)
    return {
        "versions": len(items),
        "named": len([item for item in snapshots(lib, rel) if item["named"]]),
        "cursor": cursor,
        "can_undo": any(item["seq"] < cursor for item in items),
    }


def undo(lib: Library, rel: str) -> dict[str, Any]:
    """把游标往回挪一格（撤销上一次改动）。"""
    path = safe_path(lib, rel)
    folder = _snapshot_dir(lib, rel)
    cursor = _read_cursor(folder)
    older = [
        item for item in sorted(folder.glob("*.snap"), key=_seq_of) if 0 <= _seq_of(item) < cursor
    ]
    if not older:
        raise NoteError("已经是最早的那一版了")
    target = older[-1]
    data = target.read_bytes()
    _write_atomic(path, data)
    _write_cursor(folder, _seq_of(target))
    _invalidate(lib)
    return {"restored": target.name, "seq": _seq_of(target), "remaining": len(older) - 1}


def restore(lib: Library, rel: str, name: str) -> dict[str, Any]:
    """恢复**指定的那一版**（文件恢复：从历史里挑一条）。

    撤销是"往回退一步"，这个是"跳到某一版" —— 两者都有用：前者治手滑，
    后者是"三天前那版里有一段我要抄回来"。
    """
    folder = _snapshot_dir(lib, rel)
    target = folder / Path(name).name          # 只取文件名，挡住 `../` 这类路径
    if not target.is_file():
        raise NoteNotFound(f"没有这一版：{name}")
    path = safe_path(lib, rel)
    data = target.read_bytes()
    _write_atomic(path, data)
    seq = _seq_of(target)
    if seq >= 0:
        _write_cursor(folder, seq)
    _invalidate(lib)
    return {"restored": target.name, "bytes": len(data), "seq": seq}


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
    suffix = path.suffix.lower()
    if suffix not in (MD_SUFFIX, OUTLINE_SUFFIX):
        # 画布不走这条路（它写的是 JSON，走 `save_raw` + `canvas.apply`）；
        # **大纲走这条**：它也是纯文本，行级操作照用，只是没有头。
        raise NoteError(f"这个操作只支持 Markdown 笔记与大纲（当前是 {path.suffix}）")
    text = read_text(path)
    if suffix == OUTLINE_SUFFIX:
        # 大纲没有 YAML 头，也就没有"头没闭合"这回事；
        # 解析结果照 `split_text` 给（`note.body` 就是全文）—— 调用方只用到 `.body`
        # 与 `.had_header`，两个都对得上。
        return path, text, split_text(text)
    note = split_text(text)
    if note.broken_header:
        raise NoteError("这篇笔记的元数据头没有闭合，先修好再编辑（免得越改越乱）")
    return path, text, note


def write_body(lib: Library, rel: str, body: str, *, why: str) -> dict[str, Any]:
    path, text, note = ensure_note(lib, rel)
    if kind_of(rel) == "outline":
        # 大纲**不带 YAML 头**，写正文就是写正文 —— 走 `render()` 会给它补一个头，
        # 文件的形状就被悄悄改了（实测：写一次正文，行数从 3 变成 9，头也成了节点）。
        new_text = body
    elif note.had_header:
        new_text = splice_body(text, body)
    else:
        from .notes import render as _render  # noqa: PLC0415

        new_text = _render(minimal_meta(body, path.stem), body)
    _save(lib, rel, path, new_text, why=why)
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
    new_text = render(meta, note.body)
    _save(lib, rel, path, new_text, why="meta")
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


def _level_of_line(lines: list[str], index: int) -> int:
    """某一行的缩进层级（按缩进列算，与 `notes.outline()` 同一套口径）。

    只用来对齐拖拉后的层级，所以按"每 4 列一级、制表符算 4 列"就够 ——
    真正的层级判定归解析器，这里不重复发明。
    """
    if not 0 <= index < len(lines):
        return 0
    raw = lines[index]
    indent = raw[: len(raw) - len(raw.lstrip(" \t"))]
    return len(indent.replace("\t", "    ")) // 4


def _align_level(body: str, index: int, level: int) -> str:
    """把第 `index` 行那棵子树**整棵**对齐到 `level` 层。

    关键在"整棵"：算的是**第一行要挪几级**，然后子树里每一行都挪**同一个差值**。
    踩过 —— 写成"把每一行都设成同一个层级"会把子树**摊平**：
    `甲 / 甲一(子) / 甲二(子)` 拖进一层之后变成三个并列的兄弟，
    相对结构没了（那是比"位置不对"更重的错，而且看起来还挺整齐）。
    """
    from .notes import block_extent, shift_line  # noqa: PLC0415

    lines, _ = split_lines(body)
    if not 0 <= index < len(lines):
        return body
    start, end = block_extent(body, index)
    unit = detect_indent_unit(body)
    delta = max(0, level) - _level_of_line(lines, index)
    if not delta:
        return body
    for cursor in range(start, end):
        # 逐行调 `shift_line`：它只改缩进、不改行数，所以原行号一路有效
        body = shift_line(body, cursor, delta, unit)
    return body


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
    count: int = 0,
    target: int = -1,
) -> dict[str, Any]:
    """行级操作 —— 幕布那半边的核心（回车同级、Tab 缩进、Alt+↑ 挪块）。"""
    from .notes import (  # noqa: PLC0415
        delete_line,
        insert_line,
        move_block,
        replace_line,
        replace_range,
        shift_line,
    )

    path, text, note = ensure_note(lib, rel)
    body = note.body
    if op == "insert":
        body = insert_line(body, index, raw or _sibling_line(body, index))
    elif op == "replace":
        body = replace_line(body, index, raw)
    elif op == "replace_range":
        # 块级替换：`count` 行换成 `raw`（可以多行）。默认视图按块改走这一条。
        body = replace_range(body, index, count, raw)
    elif op == "delete":
        body = delete_line(body, index)
    elif op in ("indent", "outdent"):
        body = shift_line(body, index, 1 if op == "indent" else -1, detect_indent_unit(body))
    elif op == "move":
        body = _move(body, index, delta or 1)
    elif op == "move_sibling":
        # 拖拽搬块：把第 `index` 行的**整棵子树**搬到第 `target` 行的那个兄弟的
        # 上边（`delta < 0`）或下边（`delta > 0`）。
        # **边界由这里算，不由前端算** —— 前端只知道"我拖到了哪一行"，
        # 而"那一行的子树到哪儿结束"要 `block_extent`；让调用方算，算错一次
        # 就把内容挪进别人的子树里（`_move` 顶上那段注释写的就是这个顾虑）。
        from .notes import block_extent, move_block  # noqa: PLC0415

        lines, _ = split_lines(body)
        if not 0 <= target < len(lines):
            raise NoteError(f"要挪到的那一行不存在：{target}（共 {len(lines)} 行）")
        if target == index:
            # 拖回原地：什么都不做，也**不保存**（省一个没意义的快照）
            return read_note(lib, rel)
        target_start, target_end = block_extent(body, target)
        # **先对齐层级、再搬**：幕布拖到某行前后是"插到那一层的相邻位置"，
        # 不是"抱着原来的缩进过去"（从 L0 拖进一堆 L1 里就该变成 L1）。
        # 顺序很重要：`_align_level` 只改缩进、**不改行数**，所以 `index` 与上面算出的
        # `target_start/target_end` 依旧有效；反过来先搬再对齐就会对到别人身上
        #（实测踩过：拖一次下去，旁边的兄弟跟着缩进了两级）。
        body = _align_level(body, index, _level_of_line(split_lines(body)[0], target))
        body = move_block(body, index, target_end if (delta or 1) > 0 else target_start)
    elif op == "move_into":
        # 落到**行上** = 成为它的子级（幕布拖拽的另一种落法）。
        # 这一条与上一条是两件事：上一条"插在那一层的相邻位"，这一条"进入它里面"。
        from .notes import block_extent, move_block  # noqa: PLC0415

        lines, _ = split_lines(body)
        if not 0 <= target < len(lines):
            raise NoteError(f"要放进去的那一行不存在：{target}（共 {len(lines)} 行）")
        if target == index:
            return read_note(lib, rel)
        _, target_end = block_extent(body, target)
        # 同样先对齐再搬（理由见 `move_sibling`）
        body = _align_level(body, index, _level_of_line(split_lines(body)[0], target) + 1)
        body = move_block(body, index, target_end)
    else:
        raise NoteError(f"不认识的编辑操作：{op}")

    # **大纲不能走 `render()`**：那份函数会给没有头的文件补一个 YAML 头，
    # 而 `.outline` 的全部好处就在于"没有头、纯文本、随便编辑" ——
    # 每次编辑都给它加个头，文件形状就被悄悄改掉了（改完还是能读，所以更难发现）。
    if kind_of(rel) == "outline" or note.had_header:
        new_text = body if kind_of(rel) == "outline" else splice_body(text, body)
    else:
        new_text = render(minimal_meta(body, path.stem), body)
    _save(lib, rel, path, new_text, why=op)
    _invalidate(lib)
    return read_note(lib, rel)


def create_note(lib: Library, folder: str, title: str, *, kind: str = "note") -> dict[str, Any]:
    """新建一篇笔记（「未解析链接」一键补齐也走这里）。

    **标题可以为空** —— 界面上的新建是"先把它建出来，再在树里起名"（Trilium 的
    Ctrl+O 就是这样：`keyboard_actions.ts:126-133`，建完直接在树上改标题）。
    要求先想好名字，等于给最高频的动作加一道仪式；而空标题只要给个占位名、
    重名往后编号就行。
    """
    suffix = OUTLINE_SUFFIX if kind == "outline" else MD_SUFFIX
    clean = _BAD_NAME.sub(" ", title).strip()
    folder_rel = (folder or "").replace("\\", "/").strip("/")
    if not clean:
        clean, series = "未命名", 2
        while True:
            probe = f"{folder_rel}/{clean}{suffix}" if folder_rel else f"{clean}{suffix}"
            if not safe_path(lib, probe).exists():
                break
            clean, series = f"未命名 {series}", series + 1
    rel = f"{folder_rel}/{clean}{suffix}" if folder_rel else f"{clean}{suffix}"
    path = safe_path(lib, rel)
    if path.exists():
        raise NoteError(f"已经有一篇叫这个的了：{rel}")
    # 新建的笔记不标 `qf_generated`：头是应用写的，但内容是作者写的 ——
    # 那个标记的语义是"这个头是导入器补的"，别把它稀释掉
    if kind == "outline":
        # 大纲**不写 YAML 头**（这正是它好编辑的原因）：先给一个空节点，
        # 打开就能直接打字 —— 与幕布新建即一条空主题一样。
        _save(lib, rel, path, "- \n", why="create")
    else:
        meta: dict[str, Any] = {"title": clean, "tags": []}
        _save(lib, rel, path, render(meta, ""), why="create")
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
            _save(lib, other, safe_path(lib, other), new_text, why="rename")
            written.append((other, text))
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        # **标题就是文件名**：front-matter 里的 `title` 跟着改。
        # 不改的话，树里显示新名字、打开却还是旧标题 —— 实测就是这么对不上的。
        try:
            moved = split_text(read_text(new_path))
        except NoteError:
            moved = None
        if moved is not None and str(moved.meta.get("title") or "") != new_path.stem:
            meta = dict(moved.meta)
            meta["title"] = new_path.stem
            _save(lib, new_rel, new_path, render(meta, moved.body), why="rename")
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


# ------------------------------------------------------------------ 回收站 / 移动


def _trash_dir(lib: Library) -> Path:
    return notes_root() / TRASH_DIR / lib.name


def delete_note(lib: Library, rel: str) -> dict[str, Any]:
    """删一篇 —— **移到回收站，不是真删**。

    Trilium 是"软删除 + 孤儿回收"两阶段（`bbranch.ts:141-206`）：先标 `deleteId`，
    等所有父都删了才回收。那套是为多父（克隆）服务的，文件系统上只会多出一堆
    "删了但还在"的中间态。最小可恢复方案就是挪到 `.trash/`：文件还在、能看能捞，
    树里不再出现。
    """
    path = safe_path(lib, rel)
    if not path.is_file():
        raise NoteNotFound(f"没有这篇笔记：{rel}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash = _trash_dir(lib)
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{stamp}__{path.name}"
    shutil.move(str(path), str(target))
    # 快照目录一起挪走：不然回收站里那篇还能"撤销"，而树里已经找不到它了
    folder = _snapshot_dir(lib, rel)
    if folder.is_dir():
        shutil.move(str(folder), str(trash / f"{stamp}__{path.name}.snapshots"))
    _prune_empty_dirs(lib, path.parent)
    _invalidate(lib)
    return {"deleted": rel, "trash": target.name}


def trash_list(lib: Library) -> list[dict[str, Any]]:
    folder = _trash_dir(lib)
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(folder.iterdir(), reverse=True):
        if not item.is_file():
            continue
        stat = item.stat()
        out.append(
            {
                "name": item.name,
                "title": item.name.split("__", 1)[-1],
                "at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "bytes": stat.st_size,
            }
        )
    return out


def restore_from_trash(lib: Library, name: str, folder: str = "") -> dict[str, Any]:
    """从回收站捞回来。同名冲突时加序号，**不覆盖**现有笔记。"""
    source = _trash_dir(lib) / Path(name).name
    if not source.is_file():
        raise NoteNotFound(f"回收站里没有这一份：{name}")
    base = Path(name.split("__", 1)[-1])
    dest_dir = safe_path(lib, folder) if folder else lib.root
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / base.name
    suffix = 2
    while target.exists():
        target = dest_dir / f"{base.stem} {suffix}{base.suffix}"
        suffix += 1
    shutil.move(str(source), str(target))
    _invalidate(lib)
    return read_note(lib, target.relative_to(lib.root).as_posix())


def move_note(lib: Library, rel: str, folder: str) -> dict[str, Any]:
    """把一篇挪进另一个目录（树里拖拽就是这个）。

    与改名走**同一套**：两者都改路径，所以都要重写"按路径写的引用"。
    只按文件名写的双链（`[[标题]]`）不受影响 —— 那正是 Obsidian 习惯的好处。
    """
    folder = folder.strip("/")
    target_rel = f"{folder}/{Path(rel).name}" if folder else Path(rel).name
    if target_rel == rel:
        return read_note(lib, rel)
    moved = rename_note(lib, rel, target_rel)
    # 移走最后一篇之后把空目录收掉 —— 与删除同一个讲究，别在树里留空壳
    _prune_empty_dirs(lib, safe_path(lib, rel).parent)
    return moved


def _prune_empty_dirs(lib: Library, folder: Path) -> None:
    """删掉最后一篇之后，把空目录一路收掉 —— 别在树里留一串空壳。"""
    while folder != lib.root and folder.is_dir():
        if any(folder.iterdir()):
            return
        folder.rmdir()
        folder = folder.parent


def snapshot_named(lib: Library, rel: str, name: str) -> dict[str, Any]:
    """手动存一版（命名快照）。

    它**不参与 `undo` 的游标，也不参与轮转** —— 与 Trilium 的
    `revisionIgnoreNamedSnapshots`（`options_init.ts:151`）同一个意思：
    命名的那些是"我特意留下的"，不该被自动清理掉。
    """
    path = safe_path(lib, rel)
    if not path.is_file():
        raise NoteNotFound(f"没有这篇笔记：{rel}")
    folder = _snapshot_dir(lib, rel)
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (name or "").strip())[:40].strip("-") or "snapshot"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"named-{slug}-{stamp}.snap"
    target.write_bytes(path.read_bytes())
    return {"saved": target.name, "slug": slug, "bytes": target.stat().st_size}


def diff_version(lib: Library, rel: str, name: str) -> dict[str, Any]:
    """当前版与某一版的**逐行差异**（给"改动可对比"用）。

    Trilium 是在前端用 `diffWords` 算（`dialogs/revisions.tsx`）；我们在后端用
    `difflib` 出统一格式，前端只要给行着色就行 —— 少了把两边正文都塞进浏览器的开销。
    """
    path = safe_path(lib, rel)
    target = _snapshot_dir(lib, rel) / Path(name).name
    if not target.is_file():
        raise NoteNotFound(f"没有这一版：{name}")
    old = target.read_text(encoding="utf-8", errors="replace").splitlines()
    new = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    lines = list(difflib.unified_diff(old, new, fromfile=name, tofile="当前", lineterm="", n=2))
    return {
        "name": name,
        "lines": lines[:2000],
        "added": sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")),
        "removed": sum(1 for line in lines if line.startswith("-") and not line.startswith("---")),
    }


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


def kind_of(rel: str) -> str:
    """这份文件是什么：`note` / `canvas` / `outline`。

    一处判定，索引与 `read_note` 共用 —— 分两处写就会有一天对不上
    （症状是"树里的图标是一个样、打开又是另一个样"）。
    """
    lowered = rel.lower()
    if lowered.endswith(CANVAS_SUFFIX):
        return "canvas"
    if lowered.endswith(OUTLINE_SUFFIX):
        return "outline"
    return "note"


_INLINE_TAG = re.compile(r"(?<![\w/#])#([\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]{0,39})")


def inline_tags(text: str) -> list[str]:
    """从正文里取 `#标签`（大纲没有 YAML 头，标签只能写在正文里 —— 幕布就是这样）。"""
    out: list[str] = []
    fenced = False
    for line in split_lines(text or "")[0]:
        # 代码围栏里不认标签（`# 注释` 在代码里到处都是，认了会冒出一堆假标签）
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        for hit in _INLINE_TAG.findall(line):
            if hit.isdigit() or hit in out:
                continue
            out.append(hit)
    return out[:30]


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
            "can_undo": history_state(lib, rel)["can_undo"],
            "snapshots": history_state(lib, rel)["versions"],
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "message": "画布（Canvas）的渲染与编辑在下一步做，这里先作为笔记树里的一等条目出现",
        }

    if path.suffix.lower() == OUTLINE_SUFFIX:
        # 大纲：正文就是全部，没有头。节点信息由解析器给出
        #（它已经能算标题、项目符号、相对缩进、围栏 —— 正好是大纲要的那套）
        text = read_text(path)
        entry = found.entry(rel)
        body = text
        return {
            "lib": lib.name,
            "path": rel,
            "kind": "outline",
            "title": path.stem,
            "meta": {},
            "tags": inline_tags(body),
            "generated_header": False,
            "body": body,
            "outline": [asdict(item) for item in outline(body)],
            "lines": len(split_lines(body)[0]),
            "indent_unit": detect_indent_unit(body),
            "outlinks": found.outlinks(rel),
            "backlinks": found.backlinks(rel),
            "unresolved": entry.unresolved if entry else [],
            "snapshots": history_state(lib, rel)["versions"],
            "can_undo": history_state(lib, rel)["can_undo"],
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "bytes": path.stat().st_size,
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
        "snapshots": history_state(lib, rel)["versions"],
        "can_undo": history_state(lib, rel)["can_undo"],
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
                "kind": kind_of(entry.rel),
                "tags": entry.tags,
                "mtime": entry.mtime,
                # Trilium 的三个展示位：`#iconClass` / `#color` / `branch.prefix`（见 docs/笔记模块设计.md）。
                # 落成 front-matter 的 icon / color / prefix —— 文件里怎么写，树里就怎么显示。
                "icon": str(entry.meta.get("icon") or ""),
                "color": str(entry.meta.get("color") or ""),
                "prefix": str(entry.meta.get("prefix") or ""),
                "archived": bool(entry.meta.get("archived")),
            }
        )

    def finalize(node: dict[str, Any], prefix: str) -> dict[str, Any]:
        here = f"{prefix}/{node['name']}" if prefix else node["name"]
        dirs = [finalize(child, here) for _, child in sorted(node["dirs"].items())]
        # 自然序（Trilium 的 `sortNatural`）：`第 2 章` 排在 `第 10 章` 前面。
        # **不做手工排序**（它靠 branch.notePosition）—— 文件系统上那要么写进文件名、
        # 要么另开顺序文件，都是噪音；代价是不能拖拽排序同级条目，接受。
        files = sorted(node["files"], key=lambda item: _natural_key(item["title"] or item["name"]))
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
    "delete_note",
    "diff_version",
    "history_state",
    "index",
    "libraries",
    "library",
    "line_op",
    "move_note",
    "notes_root",
    "read_note",
    "rename_note",
    "reset_index",
    "restore",
    "restore_from_trash",
    "save_raw",
    "safe_path",
    "search",
    "set_meta",
    "snapshot_named",
    "snapshots",
    "stats",
    "tag_list",
    "trash_list",
    "tree",
    "undo",
    "write_body",
]
