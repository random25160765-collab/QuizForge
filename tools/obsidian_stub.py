#!/usr/bin/env python3
"""假插件：在普通目录上模拟 Obsidian 桥接（**开发/测试用，不是产品**）。

真插件在 Obsidian 里跑（`obsidian-bridge/`），它用 `app.vault` / `metadataCache`。
这个桩只做同一份文件信箱协议的最小实现，用来在没装 Obsidian 的机器上验证
QFStudio 这一端（`api/app/vault.py` + 五个笔记工具）。

    python3 tools/obsidian_stub.py --vault /tmp/notes

然后：

    QF_OBSIDIAN_VAULT=/tmp/notes python3 -c "from app import tools; ..."

它与真插件遵守同一条纪律：**原子写**（tmp + rename）、heartbeat 每秒一次、
陈旧文件启动时清掉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BOX = ".quizforge-bridge"
MD = {".md", ".markdown"}
SKIP = {".obsidian", ".trash", ".quizforge-bridge", ".git"}

FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
TAG_RE = re.compile(r"(?<![\w#])#([\w\u4e00-\u9fff/-]+)")


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def split_front(text: str) -> tuple[dict[str, Any], str]:
    match = FM_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [part.strip().strip('"\'') for part in value[1:-1].split(",") if part.strip()]
            meta[key.strip()] = items
        else:
            meta[key.strip()] = value.strip('"\'')
    return meta, text[match.end():]


def outline_of(body: str) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(body.splitlines(), start=1):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            rows.append({"level": len(match.group(1)), "text": match.group(2).strip(), "line": number})
    return rows


class Stub:
    def __init__(self, vault: Path) -> None:
        self.vault = vault.resolve()
        self.box = self.vault / BOX
        self.inbox = self.box / "inbox"
        self.outbox = self.box / "outbox"
        self.tmp = self.box / "tmp"
        for folder in (self.inbox, self.outbox, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        self._clean()
        self._beat_stop = False
        threading.Thread(target=self._beat, daemon=True).start()

    # ---------------------------------------------------------------- 信箱

    def _clean(self) -> None:
        now = time.time()
        for folder in (self.inbox, self.outbox, self.tmp):
            for path in folder.glob("*"):
                try:
                    if now - path.stat().st_mtime > 60:
                        path.unlink()
                except OSError:
                    pass

    def _atomic(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = self.tmp / (path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)

    def _beat(self) -> None:
        meta = {
            "protocol": 1,
            "pluginVersion": "stub-0.1.0",
            "vaultName": self.vault.name,
            "pid": os.getpid(),
            "startedAt": int(time.time() * 1000),
            "capabilities": ["read", "search", "write", "open"],
        }
        (self.box / "bridge.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        while not self._beat_stop:
            (self.box / "heartbeat").write_text(str(time.time()), encoding="utf-8")
            time.sleep(1.0)

    # ---------------------------------------------------------------- 操作

    def _safe(self, rel: str) -> Path:
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError("路径越界（必须在 vault 内）")
        path = (self.vault / rel).resolve()
        if path != self.vault and self.vault not in path.parents:
            raise ValueError("路径越界（必须在 vault 内）")
        return path

    def _notes(self) -> list[Path]:
        out = []
        for path in self.vault.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP for part in path.relative_to(self.vault).parts):
                continue
            if path.suffix.lower() in MD:
                out.append(path)
        return sorted(out)

    def tree(self, params: dict[str, Any]) -> dict[str, Any]:
        root: dict[str, Any] = {"name": self.vault.name, "path": "", "dirs": {}, "files": []}
        for path in self._notes():
            rel = path.relative_to(self.vault).as_posix()
            node = root
            for part in Path(rel).parts[:-1]:
                node = node["dirs"].setdefault(part, {"name": part, "path": "", "dirs": {}, "files": []})
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, _ = split_front(text)
            node["files"].append({
                "path": rel,
                "title": str(meta.get("title") or path.stem),
                "tags": list(meta.get("tags") or []),
                "kind": "note",
            })
        return root

    def read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._safe(str(params.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"没有这篇笔记：{params.get('path')}")
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = split_front(text)
        start = max(0, int(params.get("startChar") or 0))
        # 默认给全文（与真插件同一条纪律：切片交给工具层）
        want = int(params.get("maxChars") or 0)
        limit = max(200, min(100_000, want)) if want > 0 else max(1, len(body))
        chunk = body[start:start + limit]
        tail = start + len(chunk) if start + len(chunk) < len(body) else None
        needles = {match.group(1).strip() for match in LINK_RE.finditer(body)}
        backlinks = []
        for other in self._notes():
            if other == path:
                continue
            other_text = other.read_text(encoding="utf-8", errors="replace")
            if path.stem in needles or f"[[{path.stem}]]" in other_text or f"[[{path.stem}|" in other_text:
                backlinks.append({"path": other.relative_to(self.vault).as_posix(), "title": other.stem})
        return {
            "lib": self.vault.name,
            "path": path.relative_to(self.vault).as_posix(),
            "kind": "note",
            "title": str(meta.get("title") or path.stem),
            "meta": meta,
            "tags": list(meta.get("tags") or []) + TAG_RE.findall(body),
            "body": chunk,
            "chars": len(body),
            "startChar": start,
            "truncated": tail is not None,
            "nextChar": tail or 0,
            "hash": sha16(body),
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
            "outline": outline_of(body),
            "backlinks": backlinks,
            "outlinks": [{"target": name, "resolved": ""} for name in sorted(needles)],
        }

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        limit = max(1, min(50, int(params.get("limit") or 12)))
        tags = [word[1:].lower() for word in query.split() if word.startswith("#")]
        titles = [word.split(":", 1)[1].lower() for word in query.split() if word.startswith("title:")]
        plain = [
            word.lower()
            for word in query.split()
            if not word.startswith(("#", "title:", "-")) and word.upper() not in ("AND", "OR", "NOT")
        ]
        items = []
        for path in self._notes():
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = split_front(text)
            hay = (path.stem + "\n" + body).lower()
            if plain and not all(word in hay for word in plain):
                continue
            if titles and not all(word in str(meta.get("title") or path.stem).lower() for word in titles):
                continue
            if tags:
                own = [str(tag).lower() for tag in (meta.get("tags") or [])] + [tag.lower() for tag in TAG_RE.findall(body)]
                if not all(tag in own for tag in tags):
                    continue
            items.append({
                "lib": self.vault.name,
                "path": path.relative_to(self.vault).as_posix(),
                "title": str(meta.get("title") or path.stem),
                "snippet": body.strip().splitlines()[0][:80] if body.strip() else "",
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
            })
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items)}

    def write(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._safe(str(params.get("path") or ""))
        body = str(params.get("body") if params.get("body") is not None else "")
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        meta, old_body = split_front(text)
        base = str(params.get("baseHash") or "")
        if base and sha16(old_body) != base:
            raise RuntimeError("文件在读取后被改过，请重读再改")
        head = text[:len(text) - len(old_body)] if text else ""
        new_text = (head + body) if head else body
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        return {"path": path.relative_to(self.vault).as_posix(), "hash": sha16(body), "chars": len(body)}

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = str(params.get("folder") or "").strip("/")
        title = str(params.get("title") or "").strip() or "未命名"
        rel = params.get("path") or ((folder + "/" if folder else "") + title + ".md")
        target = self._safe(str(rel))
        if target.exists():
            raise RuntimeError(f"已经有这一篇：{rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(params.get("body") or ""), encoding="utf-8")
        return {"path": target.relative_to(self.vault).as_posix()}

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = str(request.get("op") or "")
        params = request.get("params") or {}
        try:
            if op == "ping":
                result: Any = {"protocol": 1, "pluginVersion": "stub-0.1.0",
                               "vaultName": self.vault.name,
                               "noteCount": len(self._notes())}
            elif op == "tree":
                result = self.tree(params)
            elif op == "read":
                result = self.read(params)
            elif op == "search":
                result = self.search(params)
            elif op == "write":
                result = self.write(params)
            elif op == "create":
                result = self.create(params)
            elif op == "open":
                result = {"ok": True}
            else:
                return {"protocol": 1, "id": request.get("id"), "ok": False,
                        "error": {"code": "unsupported", "message": f"不认识的 op：{op}"},
                        "ts": int(time.time() * 1000)}
            return {"protocol": 1, "id": request.get("id"), "ok": True, "result": result,
                    "ts": int(time.time() * 1000)}
        except FileNotFoundError as exc:
            return {"protocol": 1, "id": request.get("id"), "ok": False,
                    "error": {"code": "not_found", "message": str(exc)}, "ts": int(time.time() * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"protocol": 1, "id": request.get("id"), "ok": False,
                    "error": {"code": "conflict", "message": str(exc)}, "ts": int(time.time() * 1000)}

    def run(self) -> None:
        print(f"stub 在跑：{self.vault}（信箱 {self.box}）", flush=True)
        while True:
            for path in sorted(self.inbox.glob("req-*.json")):
                try:
                    request = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 - 对端还在写
                    continue
                reply = self.handle(request)
                self._atomic(self.outbox / f"res-{request.get('id')}.json", reply)
                try:
                    path.unlink()
                except OSError:
                    pass
            time.sleep(0.05)


def main() -> int:
    parser = argparse.ArgumentParser(description="Obsidian 桥接的假插件（测试用）")
    parser.add_argument("--vault", required=True, help="vault 目录（会被当成笔记库）")
    args = parser.parse_args()
    vault = Path(args.vault).expanduser()
    vault.mkdir(parents=True, exist_ok=True)
    Stub(vault).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
