"""资料归类：让模型判"这是一份什么类型的文档"。

为什么需要它：文件名只能给一个**很粗**的默认（`.pdf` → 一个占位的类型），
而实测这个库里大量是**技术文档**（芯片手册、指令集规范、编程指南、白皮书），
默认一律标"论文"是错的 —— 用户一眼就看出来不对。

做法与笔记那套建议（`pipeline/note_suggest.py`）一致：
**只读文件、只调模型、不落盘**；落盘走 `apply()`，而且由人复核之后才调。
落到元数据上时 `origin` 记成 `llm`，人改过的是 `manual` —— 谁定的永远看得出来。

两条纪律：
* **类别只有一处定义**（`app.library.KINDS`）：提示词、校验、界面标签都从它来，
  否则加了新类别却漏改一处，就会出现"模型选了但被判成非法"这种说不清的问题；
* **认识的不动**：`origin` 已经是 `manual` / `llm` 的条目不再送模型（人定的最大）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app import library as lib

#: 一次最多处理几份。一份线索约 300~500 字，24 份对上下文是安全的。
BATCH_MAX = 24

#: 每份线索里带多少字正文。前 1200 字足够判断"这是什么文档"。
HEAD_LIMIT = 1200


def _prompt(name: str) -> str:
    from . import config  # noqa: PLC0415

    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _render(template: str, mapping: dict[str, str]) -> str:
    text = template
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def pick_candidates(
    roots: list[Path],
    meta_dir: Path,
    text_dir: Path,
    *,
    limit: int = BATCH_MAX,
) -> list[dict[str, Any]]:
    """挑该归类的条目：**人定的与模型定过的都不动**；其余按"越不确定越靠前"排。

    排序依据：`kind == 'other'`（连后缀都给不出线索）最靠前，其次是文件名也没线索的。
    """
    out: list[dict[str, Any]] = []
    for entry in lib.entries(roots, meta_dir, text_dir):
        meta = entry.meta
        if str(meta.get("origin") or "") in ("manual", "llm"):
            continue
        out.append(
            {
                "citekey": entry.citekey,
                "rel": entry.item.rel,
                "title": str(meta.get("title") or entry.item.stem),
                "kind": str(meta.get("kind") or "other"),
                "topics": [str(tag) for tag in (meta.get("topics") or [])],
                "year": int(meta.get("year") or 0),
                "authors": [str(name) for name in (meta.get("authors") or [])],
                "head": lib.text_of(text_dir, entry.citekey, limit=HEAD_LIMIT)[:HEAD_LIMIT],
            }
        )
    out.sort(key=lambda one: (0 if one["kind"] == "other" else 1, one["rel"]))
    return out[:limit]


def _render_items(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for one in items:
        head = " ".join(str(one.get("head") or "").split())[:HEAD_LIMIT]
        lines.append(
            "\n".join(
                [
                    f"- citekey: {one['citekey']}",
                    f"  文件: {one['rel']}",
                    f"  目录名: {Path(str(one['rel'])).parent.as_posix()}",
                    f"  当前推断: 类型 {one.get('kind')} · 标题 {str(one.get('title'))[:80]}"
                    + (f" · 作者 {', '.join(one.get('authors') or [])[:80]}" if one.get("authors") else ""),
                    f"  正文开头: {head or '（没有可用的正文）'}",
                ]
            )
        )
    return "\n".join(lines)


def coerce(raw: Any) -> list[dict[str, Any]]:
    """把模型给的 JSON 收成我们要的形状。**纯函数**，所以能单独测。

    宽容但不出错：`items` 缺失、`topics` 是字符串、`kind` 不认识 —— 一律收拾干净或者丢掉，
    绝不让一个格式抖动变成写坏的元数据。
    """
    if isinstance(raw, dict):
        for key in ("items", "results", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        citekey = str(item.get("citekey") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if not citekey or kind not in lib.KINDS:
            continue
        topics = item.get("topics")
        if isinstance(topics, str):
            topics = [part.strip() for part in topics.replace("、", ",").split(",")]
        topics = [str(tag).strip() for tag in (topics or []) if str(tag).strip()][:4]
        out.append(
            {
                "citekey": citekey,
                "kind": kind,
                "topics": topics,
                "why": str(item.get("why") or "")[:120],
            }
        )
    return out


def accept(results: list[dict[str, Any]], known: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把模型给的 citekey 与库里的对一遍。**对不上的丢掉并记下来** —— 宁可少改，不可乱改。"""
    good: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for one in results:
        if one["citekey"] in known:
            good.append(one)
        else:
            dropped.append({"citekey": one["citekey"], "why": "库里没有这个引用键"})
    return good, dropped


async def classify(
    roots: list[Path],
    meta_dir: Path,
    text_dir: Path,
    citekeys: list[str],
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """对指定的几份跑一遍归类。**只读文件、只调模型，不落盘。**

    一份出错不该让整批白跑（与流水线做法一致），出错的摆进 `failed` 里让人看见。
    """
    from . import config  # noqa: PLC0415
    from .llm import LLM, parse_json  # noqa: PLC0415

    wanted = {key for key in citekeys if key}
    pool = pick_candidates(roots, meta_dir, text_dir, limit=max(BATCH_MAX, len(wanted) or BATCH_MAX))
    by_key = {one["citekey"]: one for one in pool}
    chosen = [by_key[key] for key in citekeys if key in by_key]
    note = ""
    if not chosen:
        # 指定的都没得判（人定过 / 引用键不对 / 已经判过）—— 那就按优先级自动挑一批，
        # 免得界面上"点了没反应"，而原因只是"这几份已经定过了"。
        # 但要**说出来**：不然人会以为模型没理他指定的那几份。
        chosen = pick_candidates(roots, meta_dir, text_dir, limit=min(BATCH_MAX, len(citekeys) or BATCH_MAX))
        if chosen:
            note = "你指定的那几份已经定过（人定过或模型定过，都不再送），改按优先级自动挑了 " + str(len(chosen)) + " 份。"
    
    if not chosen:
        return {"items": [], "failed": [], "dropped": [], "note": "没有需要归类的条目"}

    template = _prompt("doc_classify")
    prompt = _render(template, {"ITEMS": _render_items(chosen)})
    cfg = config.load()
    if on_progress:
        on_progress({"phase": "ask", "count": len(chosen)})
    async with LLM(cfg) as llm:
        # 与其余模块同一套调用形状：messages 列表 + `reply.text`。
        # （我第一版直接传了一整段字符串，被上游以 400 挡回来了。）
        reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=2000)
    parsed = parse_json(reply.text)
    results = coerce(parsed)
    known = {one["citekey"] for one in chosen}
    good, dropped = accept(results, known)
    # 模型没提到的那些也算失败：人要知道"这几份它没给"
    seen = {one["citekey"] for one in good}
    failed = [
        {"citekey": one["citekey"], "rel": one["rel"], "why": "模型没给这一份的结论"}
        for one in chosen
        if one["citekey"] not in seen
    ]
    for one in good:
        one["rel"] = by_key[one["citekey"]]["rel"]
        one["was"] = by_key[one["citekey"]]["kind"]
    return {"items": good, "failed": failed, "dropped": dropped, "note": note}


def apply(meta_dir: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    """把**被接受的那几条**写进元数据（`origin: llm`）。

    **不加判别地覆盖**是不行的：这里再查一遍现有 `origin`，
    已经是 `manual` 的直接跳过 —— 人改过的东西不该被一次模型调用冲掉，
    而且那种覆盖不会有任何提示。
    """
    known = lib.load_all_metadata(meta_dir)
    written: list[str] = []
    skipped: list[dict[str, str]] = []
    for one in results:
        citekey = str(one.get("citekey") or "")
        meta = dict(known.get(citekey) or {})
        if not meta:
            skipped.append({"citekey": citekey, "why": "没有这份元数据"})
            continue
        if str(meta.get("origin") or "") == "manual":
            skipped.append({"citekey": citekey, "why": "人定过的，不动"})
            continue
        kind = str(one.get("kind") or "").strip().lower()
        if kind in lib.KINDS:
            meta["kind"] = kind
        if one.get("topics"):
            meta["topics"] = [str(tag) for tag in one["topics"]][:4]
        meta["citekey"] = citekey
        saved = lib.save_metadata(meta_dir, meta, origin="llm")
        written.append(saved["citekey"])
    return {"written": written, "skipped": skipped}


__all__ = ["BATCH_MAX", "accept", "apply", "classify", "coerce", "pick_candidates"]
