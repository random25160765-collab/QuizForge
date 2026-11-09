"""笔记的唯一入口：**只有 Obsidian 桥接这一条路**（2026-11-07）。

用户定的三条：

1. 没有 Obsidian，笔记功能就无效 —— 所以这里**不做本地文件降级**。
2. Obsidian 在 Windows 侧、QFStudio 在 WSL 里；**不走网络**（WSL 的网络太乱）。
3. 用户在设置里填 vault 路径，Obsidian 装一个插件，两边就能通。

传输层是**同一个文件系统上的信箱**（协议见 `docs/Obsidian桥接协议.md`）：

    <vault>/.quizforge-bridge/
      bridge.json     插件写：协议版本、能力、vault 名
      heartbeat       插件每秒 touch，用来判在线
      inbox/          QFStudio 写 req-<id>.json
      outbox/         插件写 res-<id>.json
      tmp/            原子写的中间文件

这一层与 `notelib` **同形**（libraries / library / tree / read_note / search /
write_body / create_note + NoteError/NoteNotFound），所以 `tools.py` 里那五个
LLM 笔记工具只要把导入换成这里，业务代码一行不用改。

## 降级（2026-11-09）：没有 Obsidian 的设备

上面那条"没有 Obsidian 笔记功能就无效"被**手机端**打破了：手机上没有 Obsidian，
也不可能有本地文件路径。于是加了第二条路 ——

* 桥在（`bridge.json` + 心跳新鲜）→ 走信箱，桌面行为一行不改；
* 桥不在（没配路径 / Obsidian 没开 / 心跳过期）→ 转 `notecloud`，读写 `notes` 表
  （它是桌面那台机器上 `obsidian-sync` 插件同步下来的**云副本**）。

边界必须记住：**降级时读到的是上次同步的版本，不是实时文件**。所以桌面端
永远优先信箱；只有信箱不可用时才退到表。`ping` / `open_note` **不降级** ——
"检测连接"要如实回答本地 Obsidian 在不在，打不开文件也要如实报错。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import notecloud
from . import notelib

#: 与 notelib 同名的异常：工具的 `except notelib.NoteNotFound`（换成 vault 之后）
#: 不用改写法。
NoteError = notelib.NoteError
NoteNotFound = notelib.NoteNotFound


# ---------------------------------------------------------------- 配置与路径

#: 上一次读设置的时间（设置改完几秒内生效，不必每 50ms 查库）
_CONF_TTL = 3.0
_conf: dict[str, Any] = {"at": 0.0, "vault": "", "db": None}

#: 最近一次 read 拿到的正文 hash：write 时当 baseHash 用（乐观锁）。
#: 工具层的流程是「读 → 改 → 整篇写回」，这中间没有别的东西会拿这个 hash，
#: 所以按 (vault, path) 记一份就够。
_HASH: dict[tuple[str, str], str] = {}


def configure(db: Any = None) -> None:
    """工具层每次调用前喂一次 db（读设置用；降级路径也要用它查表）。"""
    _conf["db"] = db
    _conf["at"] = 0.0
    notecloud.configure(db)


def _normalize(raw: str) -> str:
    """把用户填的路径归一成 WSL 视角的绝对路径。

    用户可能填 `F:\\Notes`（Windows 资源管理器里复制的），也可能填 `/mnt/f/Notes`。
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if match:
        drive, rest = match.group(1).lower(), match.group(2)
        return "/mnt/" + drive + "/" + rest.replace("\\", "/")
    if text.startswith("~"):
        text = os.path.expanduser(text)
    text = text.replace("\\", "/").rstrip("/")
    return text


#: 设置页那颗「检测连接」用的临时覆盖：它刚在输入框里打完路径、还没同步到后端。
_override: dict[str, str] = {"path": ""}


def _vault_path() -> str:
    """vault 路径：临时覆盖 > 环境变量（测试/CLI）> 设置里的 `data.obsidian.vaultPath`。"""
    if _override["path"]:
        return _override["path"]
    env = _normalize(os.environ.get("QF_OBSIDIAN_VAULT", ""))
    if env:
        return env

    now = time.monotonic()
    if now - float(_conf["at"] or 0) < _CONF_TTL:
        return str(_conf["vault"] or "")

    vault = ""
    db = _conf.get("db")
    if db is not None:
        try:
            from .settings_store import row as settings_row

            stored = settings_row(db)
            data = (stored.data if stored else {}) or {}
            conf = data.get("obsidian") or {}
            vault = _normalize(str(conf.get("vaultPath") or ""))
        except Exception:  # noqa: BLE001 - 读不到设置就等于没配
            vault = ""
    _conf["vault"] = vault
    _conf["at"] = now
    return vault


# ---------------------------------------------------------------- 信箱

BRIDGE_DIR = ".quizforge-bridge"
#: heartbeat 在多少秒内算在线。
#: 插件在窗口前台是每秒一次；但 Obsidian 最小化后 Chromium 会把定时器节流到
#: 一分钟一次（实测心跳停在 30 秒前，而进程还活着）。所以窗口放宽到 90 秒 ——
#: 代价是"Obsidian 刚关掉"时我们会多等一会儿才判离线，那比误判可用好。
ONLINE_WINDOW = 90.0
#: 等待响应的默认上限；请求里的 `expect` 会覆盖它。
TIMEOUTS = {"1s": 1.0, "5s": 5.0, "10s": 10.0}


def _box() -> Path:
    vault = _vault_path()
    if not vault:
        raise NoteError(
            "还没配 Obsidian：打开「设置 → Obsidian」，填 vault 路径"
            "（例如 F:\\Notes 或 /mnt/f/Notes），并在 Obsidian 里启用 QFStudio 桥接插件。"
        )
    root = Path(vault)
    if not root.is_dir():
        raise NoteError(f"找不到 vault：{vault}（路径改了？在设置里重新填一次）")
    return root / BRIDGE_DIR


def _bridge() -> dict[str, Any]:
    """读 bridge.json 并确认在线。不在线就抛，**不排队**。"""
    box = _box()
    meta_path = box / "bridge.json"
    beat = box / "heartbeat"
    if not meta_path.is_file():
        raise NoteError(
            "Obsidian 没在跑，或桥接插件没启用（找不到 "
            f"{BRIDGE_DIR}/bridge.json）。"
        )
    if beat.is_file():
        age = time.time() - beat.stat().st_mtime
        if age > ONLINE_WINDOW:
            raise NoteError(f"Obsidian 没有心跳（{age:.0f} 秒无响应）：插件可能在忙，或 Obsidian 已关闭。")
    else:
        raise NoteError("桥接插件没有心跳文件：插件可能刚启动，稍等一秒再试。")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise NoteError(f"bridge.json 读不了（插件正在写？）：{exc}") from exc

    if int(meta.get("protocol") or 0) < 1:
        raise NoteError("桥接插件版本太旧：请更新 QFStudio 桥接插件。")
    meta["_box"] = str(box)
    return meta


def _online() -> bool:
    """桥这会儿可用吗。**判断只此一处** —— 每个入口各写一遍迟早会不一致。

    注意它把 `_bridge()` 的所有失败都当成"不在线"（没配路径、没开 Obsidian、
    心跳过期）。这正是降级的定义：本地拿不到，就去读云副本。
    """
    try:
        _bridge()
        return True
    except NoteError:
        return False


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """先写 tmp 再 rename —— 对端要么看不到，要么看到完整文件。"""
    box = path.parent.parent
    tmp_dir = box / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _call(op: str, params: dict[str, Any], *, expect: str = "5s") -> dict[str, Any]:
    """发一个请求并等响应。超时删掉自己的请求文件，不让插件处理一个没人等的活。"""
    meta = _bridge()
    box = Path(meta["_box"])
    inbox = box / "inbox"
    outbox = box / "outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())
    request = {
        "protocol": 1,
        "id": request_id,
        "ts": int(time.time() * 1000),
        "op": op,
        "params": params,
        "expect": expect,
    }
    req_path = inbox / f"req-{request_id}.json"
    res_path = outbox / f"res-{request_id}.json"
    _atomic_write(req_path, request)

    deadline = time.monotonic() + TIMEOUTS.get(expect, 5.0)
    while time.monotonic() < deadline:
        if res_path.is_file():
            try:
                reply = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 插件可能正在写（它也该用 tmp+rename）
                time.sleep(0.05)
                continue
            try:
                res_path.unlink()
            except OSError:
                pass
            if not reply.get("ok"):
                err = reply.get("error") or {}
                message = str(err.get("message") or "Obsidian 那边报了一个错")
                code = str(err.get("code") or "")
                if code == "not_found":
                    raise NoteNotFound(message)
                raise NoteError(message)
            result = reply.get("result")
            return result if isinstance(result, dict) else {"value": result}
        time.sleep(0.05)

    try:
        req_path.unlink()
    except OSError:
        pass
    raise NoteError(
        f"Obsidian 没在 {TIMEOUTS.get(expect, 5.0):.0f} 秒内回应（它可能正忙，或插件没在监听信箱）。"
    )


# ---------------------------------------------------------------- 与 notelib 同形的 API


def _library(vault: str, name: str = "") -> notelib.Library:
    return notelib.Library(name=name or Path(vault).name, root=Path(vault))


def libraries() -> list[notelib.Library]:
    if not _online():
        return notecloud.libraries()
    # ping 里带着笔记数：`list_notes` 不带 lib 时要把"这个库有几篇"报给模型，
    # 显示 0 会让它以为库是空的。
    info = _call("ping", {}, expect="1s")
    lib = _library(_vault_path(), str(info.get("vaultName") or ""))
    lib.notes = int(info.get("noteCount") or 0)
    return [lib]


def library(name: str = "") -> notelib.Library:
    found = libraries()
    wanted = str(name or "").strip()
    if not wanted:
        return found[0]
    for item in found:
        if item.name == wanted:
            return item
    raise NoteNotFound(f"没有这个笔记库：{wanted}")


def tree(lib: notelib.Library, dir_path: str = "") -> dict[str, Any]:
    if not _online():
        return notecloud.tree(lib, dir_path)
    params: dict[str, Any] = {}
    if dir_path:
        params["dir"] = dir_path
    return _call("tree", params)


def read_note(lib: notelib.Library, rel: str) -> dict[str, Any]:
    if not _online():
        result = notecloud.read_note(lib, rel)
        # 降级路径也要记 hash：下一步 write_body 的乐观锁靠它
        if result.get("hash"):
            _HASH[(lib.name, rel)] = str(result["hash"])
        return result

    result = _call("read", {"path": rel})
    # 记住 hash：工具层的下一步是 write_body(merged)
    if result.get("hash"):
        _HASH[(lib.name, rel)] = str(result["hash"])
    result.setdefault("lib", lib.name)
    result.setdefault("path", rel)
    result.setdefault("kind", "note")
    result.setdefault("title", Path(rel).stem)
    result.setdefault("body", "")
    result.setdefault("outline", [])
    result.setdefault("backlinks", [])
    result.setdefault("outlinks", [])
    return result


def search(query: str, *, lib_name: str = "", limit: int = 50, folder: str = "") -> list[dict[str, Any]]:
    if not _online():
        return notecloud.search(query, lib_name=lib_name, limit=limit, folder=folder)
    params: dict[str, Any] = {"query": query, "limit": limit}
    if lib_name:
        params["lib"] = lib_name
    if folder:
        params["folder"] = folder
    result = _call("search", params, expect="10s")
    items = result.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def write_body(lib: notelib.Library, rel: str, body: str, *, why: str = "编辑") -> dict[str, Any]:
    known = _HASH.get((lib.name, rel))
    if not _online():
        result = notecloud.write_body(lib, rel, body, why=why, base_hash=known or "")
        if result.get("hash"):
            _HASH[(lib.name, rel)] = str(result["hash"])
        return result

    params: dict[str, Any] = {"path": rel, "body": body, "why": why}
    if known:
        params["baseHash"] = known
    result = _call("write", params, expect="10s")
    if result.get("hash"):
        _HASH[(lib.name, rel)] = str(result["hash"])
    return result


def create_note(lib: notelib.Library, folder: str, title: str, kind: str = "note") -> dict[str, Any]:
    if not _online():
        return notecloud.create_note(lib, folder, title, kind)
    return _call("create", {"folder": folder, "title": title, "kind": kind}, expect="10s")


def create_at(lib: notelib.Library, rel: str, body: str = "") -> dict[str, Any]:
    """按**完整相对路径**新建（父目录由插件建）。

    与 `create_note(lib, folder, title)` 的分工：那个拼 `title.md`，适合"给我建一篇
    叫 X 的"；这个给的是完整路径，`edit_note` 写一个还不存在的文件时用它
    （coding agent 的惯例：写新文件 = 创建）。
    """
    if not _online():
        return notecloud.create_at(lib, rel, body)
    return _call("create", {"path": rel, "body": body}, expect="10s")


def open_note(path: str, line: int = 0) -> dict[str, Any]:
    """在 Obsidian 里打开。**不降级**：云端副本打不开 —— 那是本机文件才有的动作。"""
    if not _online():
        raise NoteError(
            "这台设备上没有 Obsidian，打不开笔记窗口（笔记本身可以读写，见 read_note / edit_note）。"
        )
    return _call("open", {"path": path, "line": line}, expect="1s")


def ping(vault_path: str = "") -> dict[str, Any]:
    """探一次连通性。`vault_path` 给的是**还没保存**的路径（设置页刚打完字就点检测）。"""
    _override["path"] = _normalize(vault_path)
    try:
        return _call("ping", {}, expect="1s")
    finally:
        _override["path"] = ""


__all__ = [
    "NoteError",
    "NoteNotFound",
    "configure",
    "libraries",
    "library",
    "tree",
    "read_note",
    "search",
    "write_body",
    "create_note",
    "open_note",
    "ping",
]
