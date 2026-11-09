"""笔记的**云副本**：没有 Obsidian 的设备靠它读写。

与 `vault`（本地信箱）**同形** —— 同样的函数名、同样的返回结构，`tools.py`
里那五个笔记工具不必知道自己在跟谁说话（路由见 `vault.py`）。

## 谁是权威

* 桌面：vault 里的 Markdown 文件是权威。`vault.py` 走信箱，**永远不写这张表**；
* 手机：没有文件可读，`notes` 表就是它看到的全部 —— 内容由桌面那台上
  `obsidian-sync` 插件同步上来的云端副本。

于是有一条边界必须记住：**桥掉线时读到的是上次同步的版本，不是实时文件**。
这不是缺陷，是"没有 Obsidian 也能读写笔记"这个能力的定义。

## 为什么哈希算法要照抄插件

两边靠 `hash` 判断"是不是同一版"（乐观锁与冲突副本都靠它）。算法一分叉，
冲突检测就会**永远**误报，而且表现为"偶尔发生"那种最难查的误报。所以
`sha16` / `split_front` / `outline_of` 与 `obsidian-bridge/main.js` 逐字节一致 ——
**不复用** `notes.split_text`：那是 YAML 解析版，对坏 frontmatter 的处理不同
（插件是正则一刀切，坏头也当正文）。
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_

from .models import Note
from .notelib import Library, NoteError, NoteNotFound

#: 请求级 Session（由 `vault.configure` 转进来）。没有库就明确报错，不静默返回空。
_conf: dict[str, Any] = {"db": None}


def configure(db: Any = None) -> None:
    """工具层/路由层每次调用前喂一次会话（与 `vault.configure` 同一个入口）。"""
    _conf["db"] = db


def _session() -> Any:
    db = _conf.get("db")
    if db is None:
        raise NoteError("没有本地库，云端笔记不可用（这条路径只给手机端）")
    return db


# ------------------------------------------------------------------ 与插件同口径

#: 与 `main.js` 的 `splitFront` 同一条正则：**不做 YAML 解析**，坏头也当正文。
_FRONT = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_H1 = re.compile(r"^#\s+(.+?)\s*$")


def sha16(text: str) -> str:
    """与 `main.js` 的 `sha16` 完全一致：sha256(utf8) 的十六进制前 16 位。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def split_front(text: str) -> tuple[str, str]:
    """切出 frontmatter 与正文。返回 `(head, body)`；没有头时 head 为空串。"""
    raw = str(text or "")
    match = _FRONT.match(raw)
    if not match:
        return "", raw
    return raw[: match.end()], raw[match.end():]


def outline_of(body: str) -> list[dict[str, Any]]:
    """标题大纲：`[{level, text, line}]`，行号从 1 起（与插件的 `outlineOf` 同）。"""
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(str(body or "").split("\n")):
        match = _HEADING.match(line)
        if match:
            rows.append(
                {"level": len(match.group(1)), "text": match.group(2).strip(), "line": index + 1}
            )
    return rows


def _title(rel: str, body: str) -> str:
    """标题：正文第一个 `# `，没有就用文件名（与插件的 `fm.title || basename` 对齐）。"""
    for line in str(body or "").split("\n")[:80]:
        match = _H1.match(line)
        if match:
            return match.group(1).strip()
    return Path(rel).stem


def _iso(epoch: float) -> str:
    """epoch 秒 → `2026-11-09T12:34:56`（UTC，与插件的 `toISOString().slice(0,19)` 同）。"""
    if not epoch:
        return ""
    return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ 路径与库


def _clean_rel(rel: str) -> str:
    """库内相对路径：归一斜杠、挡越界。与插件的 `fileOf` 同一套判据。"""
    text = str(rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise NoteError("路径不能为空")
    if any(part == ".." for part in parts):
        raise NoteError("路径越界（必须在库内）")
    if parts[0] == ".quizforge-bridge":
        raise NoteError("路径越界（信箱不给笔记工具用）")
    cleaned = "/".join(parts)
    if len(cleaned) > 512:
        raise NoteError("路径太长（超过 512 字符）")
    return cleaned


def _vault_path() -> str:
    db = _conf.get("db")
    if db is None:
        return ""
    try:
        from .settings_store import row as settings_row

        stored = settings_row(db)
        data = (stored.data if stored else {}) or {}
        return str((data.get("obsidian") or {}).get("vaultPath") or "").strip()
    except Exception:  # noqa: BLE001 - 读不到设置就当没配（名字退回 notes）
        return ""


def _lib_name() -> str:
    raw = _vault_path().replace("\\", "/").rstrip("/")
    return Path(raw).name if raw else "notes"


def libraries() -> list[Library]:
    """只有一个库（云副本不分库）—— 与桌面端"一个 vault 一个库"对齐。"""
    db = _session()
    name = _lib_name()
    return [Library(name=name, root=Path(name), notes=db.query(Note).count())]


def library(name: str = "") -> Library:
    found = libraries()
    wanted = str(name or "").strip()
    if not wanted:
        return found[0]
    for item in found:
        if item.name == wanted:
            return item
    raise NoteNotFound(f"没有这个笔记库：{wanted}")


# ------------------------------------------------------------------ 读


def read_note(lib: Library, rel: str) -> dict[str, Any]:
    """读一篇：返回与插件 `read` 同形的结构（正文是全文，切片由工具层做）。"""
    db = _session()
    path = _clean_rel(rel)
    row = db.get(Note, path)
    if row is None:
        raise NoteNotFound(f"没有这篇笔记：{path}（它可能还没从 Obsidian 同步上来）")

    body = row.body or ""
    return {
        "lib": lib.name,
        "path": path,
        "kind": "note",
        "title": _title(path, body),
        "meta": {},
        "tags": [],
        "body": body,
        "chars": len(body),
        "startChar": 0,
        "truncated": False,
        "nextChar": 0,
        # 旧行可能没写 hash（云副本出现之前建的行）：现算一份，别让它空着
        "hash": row.hash or sha16(body),
        "mtime": _iso(float(row.mtime or 0)),
        "outline": outline_of(body),
        # 云端不做反链扫描：那是插件的活（它手上有 metadataCache）。
        # 手机端读到"没有反链"是准确的 —— 这张表里确实只有正文。
        "backlinks": [],
        "outlinks": [],
    }


# ------------------------------------------------------------------ 写


def write_body(
    lib: Library, rel: str, body: str, *, why: str = "编辑", base_hash: str = ""
) -> dict[str, Any]:
    """整篇换正文（保留不了 frontmatter —— 表里没存它，见模块头）。

    `base_hash` 是乐观锁：调用方上次读到的 hash 与表里不一致，就说明这中间
    别处（另一台设备经同步）改过，**不覆盖**，让它重读再改。
    """
    db = _session()
    path = _clean_rel(rel)
    row = db.get(Note, path)
    if row is None:
        raise NoteNotFound(f"没有这篇笔记：{path}（要新建请用 create）")

    if base_hash and row.hash and str(base_hash) != str(row.hash):
        raise NoteError("这条笔记在读取后被改过（另一台设备同步了新版本），请重读再改")

    text = str(body or "")
    row.body = text
    row.hash = sha16(text)
    row.size = len(text.encode("utf-8"))
    row.mtime = time.time()
    row.updated_at = datetime.now(UTC)
    db.commit()
    return {"path": path, "hash": row.hash, "chars": len(text)}


def create_at(lib: Library, rel: str, body: str = "") -> dict[str, Any]:
    """按完整相对路径新建（与插件的 `create` 同形；父目录在表里不需要）。"""
    db = _session()
    path = _clean_rel(rel)
    if db.get(Note, path) is not None:
        raise NoteError(f"已经有这一篇：{path}")

    text = str(body or "")
    row = Note(
        path=path,
        body=text,
        hash=sha16(text),
        mtime=time.time(),
        size=len(text.encode("utf-8")),
    )
    db.add(row)
    db.commit()
    return {"path": path, "hash": row.hash, "chars": len(text)}


def create_note(lib: Library, folder: str, title: str, kind: str = "note") -> dict[str, Any]:
    """`folder` + `title` 的旧写法（拼 `title.md`），与插件一致。"""
    top = str(folder or "").strip().strip("/")
    name = str(title or "").strip() or "未命名"
    rel = (top + "/" if top else "") + name + ".md"
    return create_at(lib, rel, "")


# ------------------------------------------------------------------ 找与列


def _like_escape(text: str) -> str:
    """LIKE 里的 `%` / `_` / `\\` 要转义，否则用户搜 `50%` 会变成通配。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """与插件 `search` 同一套解析：`#tag` / `title:` / `and|or|not` 都不是正文词。

    云端副本没有 frontmatter，标签过滤无法成立 —— 把 `#tag` 直接丢掉，
    而不是拿它去匹配正文（那会搜出一堆"正文里恰好写了 #tag"的假命中）。
    """
    words = str(query or "").split()
    tags = [word for word in words if word.startswith("#")]
    titles = [word[6:].lower() for word in words if word.startswith("title:")]
    plain = [
        word.lower()
        for word in words
        if not word.startswith("#")
        and not word.startswith("title:")
        and not word.startswith("-")
        and word.lower() not in ("and", "or", "not")
    ]
    del tags  # 只是显式说明：标签条件不参与
    return plain, titles


def search(
    query: str, *, lib_name: str = "", limit: int = 50, folder: str = ""
) -> list[dict[str, Any]]:
    """按词搜路径与正文，返回与插件 `search` 同形的 `{items,count}` 里的 items。"""
    db = _session()
    plain, titles = _parse_query(query)
    if not plain and not titles:
        return []

    q = db.query(Note)
    if folder:
        prefix = _clean_rel(folder).rstrip("/") + "/"
        q = q.filter(Note.path.like(_like_escape(prefix) + "%", escape="\\"))
    for word in plain:
        pattern = "%" + _like_escape(word) + "%"
        q = q.filter(
            or_(
                Note.path.like(pattern, escape="\\"),
                Note.body.like(pattern, escape="\\"),
            )
        )

    # 标题要现推（表里没有这一列），所以多取一些再在 Python 侧过滤
    need = max(limit * 4, limit)
    rows = q.order_by(Note.mtime.desc()).limit(need).all()

    items: list[dict[str, Any]] = []
    for row in rows:
        body = row.body or ""
        title = _title(row.path, body)
        if titles and not all(word in title.lower() for word in titles):
            continue
        line = next((text.strip() for text in body.split("\n") if text.strip()), "")
        items.append(
            {
                "lib": lib_name or _lib_name(),
                "path": row.path,
                "title": title,
                "snippet": line[:120],
                "mtime": _iso(float(row.mtime or 0)),
            }
        )
        if len(items) >= limit:
            break
    return items


def tree(lib: Library, dir_path: str = "") -> dict[str, Any]:
    """目录树：与插件 `tree()` 同形（`dirs` 是 dict，节点自带 `files`）。

    `dir_path` 给了就只列那一段（返回的根换名字）；空 = 整库。
    """
    db = _session()
    prefix = _clean_rel(dir_path) if str(dir_path or "").strip() else ""
    root_name = prefix.rsplit("/", 1)[-1] if prefix else lib.name
    root: dict[str, Any] = {"name": root_name, "path": "", "dirs": {}, "files": []}

    q = db.query(Note)
    if prefix:
        q = q.filter(Note.path.like(_like_escape(prefix + "/") + "%", escape="\\"))
    for row in q.order_by(Note.path).all():
        rel = row.path
        parts = rel.split("/")
        if prefix:
            parts = parts[len(prefix.split("/")):]
            if not parts:
                continue
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(
                part, {"name": part, "path": "", "dirs": {}, "files": []}
            )
        node["files"].append(
            {
                "path": rel,
                "title": _title(rel, row.body or ""),
                "tags": [],
                "kind": "note",
            }
        )
    return root


__all__ = [
    "configure",
    "libraries",
    "library",
    "tree",
    "read_note",
    "search",
    "write_body",
    "create_note",
    "create_at",
    "sha16",
    "split_front",
    "outline_of",
]
