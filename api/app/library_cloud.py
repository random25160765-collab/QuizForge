"""资料切片整包上传/下载（材料 + 切片**正文** + 图）。

## 为什么是"整包"而不是行级同步

现有 sync 是**行级**的：每张表按本地主键 `id` 生成 uid 再逐行推。资料这三张表
（`materials` / `material_slices` / `material_figures`）是出题流水线写的，
主键是自增 id、切片还带 `material_id` 外键 —— 直接纳入行级同步，两台机器上
"同一个材料不同 id"会打架（`conversation_folders` 那次就撞过唯一约束）。

而资料的变更不频繁（拖进来、重新拆分时才变），**整包替换**最省事：
一个元数据包 + 若干正文分块，拉回来按业务键（slug / slice_id / figure_id）upsert。

## 正文为什么分块

切片正文合计 **31.7 MB**（3989 片全覆盖）。Turso 的单次 pipeline 请求实测
超过几 MB 就被掐断（`Connection reset by peer`），所以正文按 768 KB 一块写进
`qf_library_text`，元数据包里记块数与总字节以便核对。

## 边界（用户 2026-11-08 定的）

* **切片就是正文**：`material_slices.text` 是从原文件按行区间截出来的那一段，
  换一台机器靠它读材料（原文件本身不上云）；
* 资料**不加密**（与对话那套端到端加密不同）。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import func, select, text

from . import sync
from .models import Material, MaterialFigure, MaterialSlice

PACK_ID = 1

#: 正文一块多少字节。768 KB 在实测里很稳（6.9 MB 那次会断）。
TEXT_BLOCK_BYTES = 768 * 1024

#: 云端那两张表。放在 `qf_sync_*` 旁边，但不进那套行级机制（互不干扰）。
REMOTE_TABLE = """
CREATE TABLE IF NOT EXISTS qf_library (
  id      INTEGER PRIMARY KEY,
  at      REAL NOT NULL,
  origin  TEXT NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""

REMOTE_TEXT_TABLE = """
CREATE TABLE IF NOT EXISTS qf_library_text (
  idx     INTEGER PRIMARY KEY,
  bytes   INTEGER NOT NULL DEFAULT 0,
  payload TEXT
)
"""


def _client() -> Any:
    cfg = sync.turso_config()
    if not cfg.get("url") or not cfg.get("token"):
        raise RuntimeError(
            "还没配云同步：先在设置里填 Turso 地址与令牌（或写 `config/settings.local.json` 的 turso 块）"
        )
    return sync.TursoClient(cfg["url"], cfg["token"])


def _origin() -> str:
    from .db import get_engine  # noqa: PLC0415 - 避免循环导入

    return sync._origin(get_engine())  # noqa: SLF001


# ------------------------------------------------------------ 自动同步的两把标尺
#
# 资料包是**整包替换**的：谁最后推，云上就是谁那份。自动同步不能每轮都推
# 31MB，也不能每轮都把云上的整包拉下来。所以本地记两个值（存在
# `qf_sync_state`，与行级同步共用；它是本机状态，不参与同步）：
#
#   library_pack_at  上次对齐过的云端 `qf_library.at`
#   library_pack_fp  上次对齐时本地资料包的指纹
#
# 两边一比就知道"谁变了"：远端 at 变了 = 云上动过；本地指纹变了 = 本机动过。
# 只有一边动 → 拉/推；两边都动 → 先把云上的拉下来（共享行以远端为准），
# 再把并集推回去 —— 本机独有、云端没有的材料不会丢。
_STATE_AT = "library_pack_at"
_STATE_FP = "library_pack_fp"


def _state_get(db: Any, key: str) -> str:
    row = db.execute(text("SELECT v FROM qf_sync_state WHERE k = :k"), {"k": key}).first()
    return str(row[0]) if row is not None and row[0] is not None else ""


def _state_set(db: Any, key: str, value: str) -> None:
    db.execute(
        text(
            "INSERT INTO qf_sync_state(k, v) VALUES(:k, :v)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v"
        ),
        {"k": key, "v": value},
    )


def _text_stats(db: Any) -> dict[str, int]:
    """正文规模（条数 + 字符数）。指纹用它，避免为了算哈希把 31MB 正文读一遍。"""
    row = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(func.length(MaterialSlice.text)), 0),
        ).where(MaterialSlice.text != "")
    ).one()
    return {"textSlices": int(row[0]), "textChars": int(row[1])}


#: 指纹只覆盖"同步会带走的内容"：三张表的业务字段 + 正文规模。
#: 云端 payload 里也存了同样这几个字段，所以两边的指纹可以直接比。
_FP_KEYS = ("version", "materials", "slices", "figures", "textSlices", "textChars")


def _fingerprint_of(data: dict) -> str:
    slim = {key: data.get(key) for key in _FP_KEYS}
    blob = json.dumps(slim, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint(db: Any) -> str:
    """本地资料包现在的指纹。"""
    data = pack(db)
    data.update(_text_stats(db))
    return _fingerprint_of(data)


def pack(db: Any, *, with_text: bool = False) -> dict:
    """把本地三张表打成一份可传输的 JSON（用业务键，不用自增 id）。

    `with_text=False` 时**不带正文**（正文走 `text_blocks()` 分块传）。
    """
    materials = db.scalars(select(Material)).all()
    slug_of = {m.id: m.slug for m in materials}

    out_materials = [
        {
            "slug": m.slug,
            "subject": m.subject,
            "title": m.title,
            "lines": m.lines,
            "sha256": m.sha256,
            "depth": m.depth,
        }
        for m in materials
    ]
    out_slices = []
    for s in db.scalars(select(MaterialSlice)).all():
        slug = slug_of.get(s.material_id)
        if not slug:
            continue
        item = {
            "material": slug,
            "slice_id": s.slice_id,
            "path": s.path,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "tokens": s.tokens,
            "figures": s.figures,
            "summary": s.summary,
            "windows": s.windows,
        }
        if with_text:
            item["text"] = s.text
        out_slices.append(item)

    out_figures = []
    for f in db.scalars(select(MaterialFigure)).all():
        slug = slug_of.get(f.material_id)
        if not slug:
            continue
        out_figures.append(
            {
                "material": slug,
                "figure_id": f.figure_id,
                "src": f.src,
                "slice_id": f.slice_id,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "caption": f.caption,
                "kind": f.kind,
                "keys": f.keys,
                "plan": f.plan,
            }
        )
    return {
        "version": 2,
        "materials": out_materials,
        "slices": out_slices,
        "figures": out_figures,
    }


def text_blocks(db: Any, *, limit: int = TEXT_BLOCK_BYTES) -> list[tuple[int, str, int]]:
    """切片正文 → 若干块 `(idx, json, bytes)`，每块不超过 `limit`。"""
    materials = db.scalars(select(Material)).all()
    slug_of = {m.id: m.slug for m in materials}

    blocks: list[tuple[int, str, int]] = []
    buf: list[dict] = []
    size = 0
    for s in db.scalars(select(MaterialSlice).order_by(MaterialSlice.id)).all():
        slug = slug_of.get(s.material_id)
        if not slug or not s.text:
            continue
        item = {"material": slug, "slice_id": s.slice_id, "text": s.text}
        chunk = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if buf and size + chunk > limit:
            blob = json.dumps(buf, ensure_ascii=False)
            blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
            buf, size = [], 0
        buf.append(item)
        size += chunk
    if buf:
        blob = json.dumps(buf, ensure_ascii=False)
        blocks.append((len(blocks), blob, len(blob.encode("utf-8"))))
    return blocks


def push(db: Any, *, remote: Any | None = None) -> dict:
    """把整包（元数据 + 正文分块）写到云端，覆盖上一次。"""
    data = pack(db)
    blocks = text_blocks(db)
    data["textBlocks"] = len(blocks)
    data["textBytes"] = sum(size for _, _, size in blocks)
    data.update(_text_stats(db))

    blob = json.dumps(data, ensure_ascii=False)
    client = remote or _client()
    client.execute(REMOTE_TABLE)
    client.execute(REMOTE_TEXT_TABLE)
    pushed_at = time.time()
    client.execute(
        "INSERT OR REPLACE INTO qf_library (id, at, origin, bytes, payload) VALUES (?, ?, ?, ?, ?)",
        (PACK_ID, pushed_at, _origin(), len(blob.encode("utf-8")), blob),
    )
    client.execute("DELETE FROM qf_library_text")
    for idx, payload, size in blocks:
        client.execute(
            "INSERT INTO qf_library_text (idx, bytes, payload) VALUES (?, ?, ?)",
            (idx, size, payload),
        )
    return {
        "at": pushed_at,
        "materials": len(data["materials"]),
        "slices": len(data["slices"]),
        "figures": len(data["figures"]),
        "bytes": len(blob.encode("utf-8")),
        "textBlocks": len(blocks),
        "textBytes": data["textBytes"],
        "textSlices": data["textSlices"],
        "textChars": data["textChars"],
    }


def pull(db: Any, *, remote: Any | None = None) -> dict:
    """从云端拉整包，按业务键 upsert 到本地三张表（含切片正文）。"""
    client = remote or _client()
    try:
        rows = client.query("SELECT payload, bytes FROM qf_library WHERE id = ?", (PACK_ID,))
    except Exception:  # noqa: BLE001 - 表还没建过
        return {"materials": 0, "slices": 0, "figures": 0, "bytes": 0, "empty": True}
    if not rows:
        return {"materials": 0, "slices": 0, "figures": 0, "bytes": 0, "empty": True}

    data = json.loads(rows[0]["payload"] or "{}")
    by_slug = {m.slug: m for m in db.scalars(select(Material)).all()}

    added = updated = 0
    for item in data.get("materials", []):
        slug = str(item.get("slug") or "")
        if not slug:
            continue
        hit = by_slug.get(slug)
        if hit is None:
            hit = Material(
                slug=slug,
                subject=str(item.get("subject") or ""),
                title=str(item.get("title") or ""),
                # 原文件不在这台机器上：路径留空，正文走切片（`material_slices.text`）
                source_path="",
                sha256=str(item.get("sha256") or ""),
                lines=int(item.get("lines") or 0),
                depth=str(item.get("depth") or "检索"),
            )
            db.add(hit)
            by_slug[slug] = hit
            added += 1
        else:
            hit.subject = str(item.get("subject") or hit.subject)
            hit.title = str(item.get("title") or hit.title)
            hit.lines = int(item.get("lines") or hit.lines)
            hit.sha256 = str(item.get("sha256") or hit.sha256)
            hit.depth = str(item.get("depth") or hit.depth)
            updated += 1
    db.flush()

    # 切片与图：按 (material_id, slice_id/figure_id) 覆盖
    slices = {(s.material_id, s.slice_id): s for s in db.scalars(select(MaterialSlice)).all()}
    for item in data.get("slices", []):
        material = by_slug.get(str(item.get("material") or ""))
        if material is None:
            continue
        key = (material.id, str(item.get("slice_id") or ""))
        hit = slices.get(key)
        if hit is None:
            hit = MaterialSlice(material_id=material.id, slice_id=key[1])
            db.add(hit)
            slices[key] = hit
        hit.path = item.get("path") or []
        hit.start_line = int(item.get("start_line") or 0)
        hit.end_line = int(item.get("end_line") or 0)
        hit.tokens = str(item.get("tokens") or "")
        hit.figures = item.get("figures") or []
        hit.summary = str(item.get("summary") or "")
        hit.windows = int(item.get("windows") or 0)

    figures = {(f.material_id, f.figure_id): f for f in db.scalars(select(MaterialFigure)).all()}
    for item in data.get("figures", []):
        material = by_slug.get(str(item.get("material") or ""))
        if material is None:
            continue
        key = (material.id, str(item.get("figure_id") or ""))
        hit = figures.get(key)
        if hit is None:
            hit = MaterialFigure(material_id=material.id, figure_id=key[1])
            db.add(hit)
            figures[key] = hit
        hit.src = str(item.get("src") or "")
        hit.slice_id = str(item.get("slice_id") or "")
        hit.start_line = int(item.get("start_line") or 0)
        hit.end_line = int(item.get("end_line") or 0)
        hit.caption = str(item.get("caption") or "")
        hit.kind = str(item.get("kind") or "")
        hit.keys = item.get("keys") or []
        # `plan` 是 Text 列（一句话说明），传 dict 会报 "type 'dict' is not supported"
        hit.plan = str(item.get("plan") or "")

    db.flush()

    # 正文：从分块里按 (slug, slice_id) 回填
    text_slices = filled = 0
    try:
        blocks = client.query("SELECT payload FROM qf_library_text ORDER BY idx")
    except Exception:  # noqa: BLE001 - 老包里没有这张表
        blocks = []
    for row in blocks:
        try:
            items = json.loads(row.get("payload") or "[]")
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            material = by_slug.get(str(item.get("material") or ""))
            if material is None:
                continue
            hit = slices.get((material.id, str(item.get("slice_id") or "")))
            if hit is None:
                continue
            hit.text = str(item.get("text") or "")
            text_slices += 1
            filled += len(hit.text)

    db.commit()
    return {
        "materials": added + updated,
        "added": added,
        "updated": updated,
        "slices": len(data.get("slices", [])),
        "figures": len(data.get("figures", [])),
        "bytes": int(rows[0].get("bytes") or 0),
        "textBlocks": len(blocks),
        "textSlices": text_slices,
        "textBytes": filled,
    }


def sync_pack(db: Any, *, remote: Any | None = None) -> dict:
    """自动同步一轮资料包：比较两边指纹，决定推、拉、还是都不动。

    这是「云端-缓存」里资料那一半的入口。规则只有三条：

    * 只有云上变了 → 拉（本机没改过，直接接受云上的）；
    * 只有本机变了 → 推（云上没动过，本机就是最新）；
    * 两边都变了 → 先拉齐（共享行以远端为准），再把并集推回去
      （本机独有的材料不在远端，拉取不会删它，推上去两边就一致了）。

    首次同步（本地还没有标尺）走第三条：先拉后推，把两边的并集建立起来。
    指纹只看业务字段与正文规模，所以每轮先比指纹、再决定要不要传
    31MB 的正文块 —— 这是它敢每分钟跑一次的原因。

    安全阀：**本机一份材料都没有时绝不推空包**。云端可能是另一台机器
    整理好的资料库，推一个空包上去等于把它抹掉。
    """
    client = remote or _client()
    client.execute(REMOTE_TABLE)
    client.execute(REMOTE_TEXT_TABLE)
    bind = db.get_bind()
    sync.ensure_sync_schema(bind if hasattr(bind, "begin") else bind.engine)

    rows = client.query(
        "SELECT at, origin, bytes, payload FROM qf_library WHERE id = ?", (PACK_ID,)
    )
    local_count = int(db.scalar(select(func.count()).select_from(Material)) or 0)
    local_fp = fingerprint(db)
    state_at = float(_state_get(db, _STATE_AT) or 0.0)
    state_fp = _state_get(db, _STATE_FP)

    if not rows:
        if not local_count:
            return {"action": "idle", "reason": "两边都还没有材料"}
        out = push(db, remote=client)
        _state_set(db, _STATE_AT, str(out["at"]))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "push", **out, "fingerprint": local_fp}

    remote_row = rows[0]
    remote_at = float(remote_row.get("at") or 0.0)
    try:
        remote_data = json.loads(remote_row.get("payload") or "{}")
    except ValueError:
        remote_data = {}
    remote_fp = _fingerprint_of(remote_data)
    remote_changed = remote_at > state_at + 1e-6
    local_changed = local_fp != state_fp

    if not remote_changed and not local_changed:
        return {"action": "idle", "at": remote_at, "fingerprint": local_fp}

    if remote_changed and remote_fp == local_fp:
        # 云上那份与本机当前内容一致（对端把同一份包推了回来）：只对齐标尺。
        _state_set(db, _STATE_AT, str(remote_at))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "same", "at": remote_at, "fingerprint": local_fp}

    if remote_changed and not local_changed:
        out = pull(db, remote=client)
        new_fp = fingerprint(db)
        _state_set(db, _STATE_AT, str(remote_at))
        _state_set(db, _STATE_FP, new_fp)
        db.commit()
        return {"action": "pull", **out, "at": remote_at, "fingerprint": new_fp}

    if local_changed and not remote_changed:
        if not local_count:
            # 本机清空了资料：不推空包，只把标尺记成"已对齐"，避免每轮重算。
            _state_set(db, _STATE_FP, local_fp)
            db.commit()
            return {"action": "idle", "reason": "本机没有材料，不覆盖云端"}
        out = push(db, remote=client)
        _state_set(db, _STATE_AT, str(out["at"]))
        _state_set(db, _STATE_FP, local_fp)
        db.commit()
        return {"action": "push", **out, "fingerprint": local_fp}

    # 两边都变过（含首次同步）：先拉齐，再把并集推回去。
    pulled: dict = {"materials": 0}
    if remote_data.get("materials"):
        pulled = pull(db, remote=client)
    if local_count:
        after_pull_fp = fingerprint(db)
        out = push(db, remote=client)
        _state_set(db, _STATE_AT, str(out["at"]))
        _state_set(db, _STATE_FP, after_pull_fp)
        db.commit()
        return {"action": "pull+push", "pulled": pulled, **out, "fingerprint": after_pull_fp}

    new_fp = fingerprint(db)
    _state_set(db, _STATE_AT, str(remote_at))
    _state_set(db, _STATE_FP, new_fp)
    db.commit()
    if pulled.get("materials"):
        return {"action": "pull", "pulled": pulled, "at": remote_at, "fingerprint": new_fp}
    return {
        "action": "idle",
        "reason": "本机没有材料，不覆盖云端",
        "at": remote_at,
        "fingerprint": new_fp,
    }
