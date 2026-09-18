"""让模型给笔记补标签与双链 —— 但**不替人做决定**。

四个库一千多篇里，多数既没有标签也没有链接（实测口径见 `docs/笔记功能对照.md`）。
一条条手加不现实，全自动加又没人敢信 —— 所以做成"模型出建议、人逐条收"：
建议不落盘，只有被接受的那几条才写进文件，而且写之前照例留快照（可撤销，与「文件恢复」同源）。

两条讲究：

* **链接只能指向真实存在的标题**。模型的常见失败是"编一个看起来该有的标题" ——
  所以清单是喂给它的白名单，回来还要**照库里的索引逐条核对**，对不上的直接丢掉并计数
  （丢掉多少要能看到，不能悄悄吞掉）。
* **宁缺勿滥**。空建议是允许的答案：一篇没有同类可归的笔记，硬凑标签只会让人一条条删，
  比不给还费事。提示词里明说，这里也不"兜底补一个"。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from . import config

# 让 `app.*` 可导入（与 dbstore / graph_build 同一做法：pipeline 是脚本目录，不在包里）
sys.path.insert(0, str(config.ROOT / "api"))

from app import notelib  # noqa: E402

#: 喂给模型的正文长度。前 1500 字足够判断"这篇在讲什么"，再长是浪费 tokens
BODY_LIMIT = 1500

#: 标题清单的上限。一千多篇全塞进去会挤掉正文，也让模型更容易挑错；
#: 先用正文里的词做一轮粗筛（见 `_title_pool`），再取最像的那些。
TITLE_POOL = 160

#: 一次最多处理几篇（配 `limit` 用，防止一口气把预算花光）
BATCH_MAX = 40


def _prompt(name: str) -> str:
    from . import config  # noqa: PLC0415

    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _render(template: str, mapping: dict[str, str]) -> str:
    """把 `{{KEY}}` 换掉。

    自己写这一小段而不 import `pipeline.worker`：那个模块为了跑任务拖了一堆东西，
    而这里只是"渲染一个提示词"。少一个依赖就少一处将来会断的地方。
    """
    text = template
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _tokens(text: str) -> set[str]:
    """粗筛用的词：中日韩按字、其余按词。分词器不引（够用就行）。"""
    out: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_\-]{2,}", text or ""):
        if re.match(r"^[\u4e00-\u9fff]+$", chunk):
            out.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
        else:
            out.add(chunk.lower())
    return out


def _title_pool(entry: Any, all_entries: list[Any]) -> list[str]:
    """给这一篇挑一批"候选标题"。

    为什么不全给：一千多篇标题塞进提示词，正文就没地方了；而且候选项越多，
    模型越容易挑一个"看起来像"的。先按**标题里的词是否出现在这篇正文里**粗筛，
    再补上标题与路径共享词的那些 —— 绝大多数真正的链接都在这个池子里。
    """
    body = (entry.body or "").lower()
    picked: list[str] = []
    for other in all_entries:
        title = other.title or Path(other.rel).stem
        if other.rel == entry.rel or not title:
            continue
        pieces = [piece for piece in re.split(r"[\s\-_/]+", title) if len(piece) >= 2]
        if title.lower() in body or any(piece.lower() in body for piece in pieces):
            picked.append(title)
    if len(picked) < 24:
        # 自己的目录同学科也算"可能相关"：正文里没提到，但往往同一主题
        folder = str(Path(entry.rel).parent)
        picked += [
            other.title or Path(other.rel).stem
            for other in all_entries
            if other.rel != entry.rel and str(Path(other.rel).parent) == folder
        ]
    seen: list[str] = []
    for title in picked:
        if title and title not in seen:
            seen.append(title)
    return seen[:TITLE_POOL]


def pick_candidates(lib: notelib.Library, *, limit: int = BATCH_MAX) -> list[dict[str, Any]]:
    """谁最需要整理：**一个标签都没有**的排前面，其次是一条链接都没有的。

    顺序有讲究 —— 先把"完全没有元数据"的清掉，检索和反链才刚开始有用；
    已经有一半的笔记，补不补影响小得多。
    """
    entries = [entry for entry in notelib.index(lib).entries() if entry.rel.endswith(".md")]
    ranked = sorted(
        entries,
        key=lambda entry: (
            0 if not entry.tags else (1 if not entry.refs else 2),   # 没标签 → 没链接 → 都有一点
            entry.rel,
        ),
    )
    out: list[dict[str, Any]] = []
    for entry in ranked[: max(1, min(limit, BATCH_MAX))]:
        out.append(
            {
                "path": entry.rel,
                "title": entry.title,
                "tags": list(entry.tags),
                "links": len(entry.refs),
                "words": len((entry.body or "").replace("\n", "")),
            }
        )
    return out


def _coerce(raw: Any) -> dict[str, list[Any]]:
    """把模型回的东西拉成我们要的形状。它偶尔会包一层 `{"result": {...}}` 或返回裸列表。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"tags": [], "links": []}
    if isinstance(raw, list):
        raw = {"tags": [], "links": raw}
    if not isinstance(raw, dict):
        return {"tags": [], "links": []}
    for key in ("result", "data", "output"):
        if isinstance(raw.get(key), dict):
            raw = raw[key]
            break
    tags = raw.get("tags") or raw.get("labels") or []
    links = raw.get("links") or raw.get("related") or []
    return {
        "tags": tags if isinstance(tags, list) else [],
        "links": links if isinstance(links, list) else [],
    }


def clean_tags(items: list[Any], *, known: set[str]) -> list[str]:
    """标签清洗：去 `#`、限长、去重、丢掉已经在用的。

    不认识的标签**留着** —— 库里的标签本来就该长出来（与链接不同：链接有"存在与否"
    这个硬事实，标签没有）。
    """
    out: list[str] = []
    for item in items:
        text = str(item or "").strip().lstrip("#").strip()
        if not text or len(text) > 24 or text in known or text in out:
            continue
        out.append(text)
    return out[:4]


def clean_links(items: list[Any], *, titles: dict[str, str]) -> tuple[list[dict[str, str]], int]:
    """双链清洗：**目标必须在库里真实存在**（按标题索引查）。

    返回（留下的, 丢掉的条数）。丢掉的条数要报出去：模型编标题是常事，
    悄悄吞掉会让人以为"它就是没建议"，而真相是它乱说了。
    """
    kept: list[dict[str, str]] = []
    dropped = 0
    for item in items:
        target = ""
        why = ""
        if isinstance(item, dict):
            target = str(item.get("target") or item.get("title") or "").strip()
            why = str(item.get("why") or "").strip()
        else:
            target = str(item or "").strip()
        target = target.strip("[]«»《》").strip()
        if not target:
            dropped += 1
            continue
        # 用**标题**当双链目标（正文里写 `[[标题]]`，按标题解析），`titles` 只用来核对存在性。
        # 早先这里错把 rel（路径）写成了 target，正文里就会出现 `[[全微分.md]]` ——
        # 能解析但难看，而且改文件位置就断。用例逮住了这个。
        if target not in titles:
            dropped += 1
            continue
        if all(exist["target"] != target for exist in kept):
            kept.append({"target": target, "why": why[:80]})
    return kept[:4], dropped


async def suggest(
    lib: notelib.Library,
    paths: list[str],
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """对指定的几篇跑一遍建议。**只读文件、只调模型，不落盘。**

    失败不抛给调用方：一篇出错不该让整批白跑（与流水线的做法一致），
    出错的摆进 `failed` 里让人看见。
    """
    from . import config  # noqa: PLC0415
    from .llm import LLM, parse_json  # noqa: PLC0415

    found = notelib.index(lib)
    entries = {entry.rel: entry for entry in found.entries()}
    all_entries = list(entries.values())
    titles = {entry.title: entry.rel for entry in all_entries if entry.title}
    known_tags = {str(tag).strip().lower() for entry in all_entries for tag in entry.tags}
    template = _prompt("note_suggest")

    out: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    dropped_total = 0
    cfg = config.load()
    async with LLM(cfg) as llm:
        for rel in paths:
            entry = entries.get(rel)
            if entry is None:
                failed.append({"path": rel, "error": "库里没有这一篇"})
                continue
            prompt = _render(
                template,
                {
                    "PATH": entry.rel,
                    "TAGS": "、".join(entry.tags) or "（还没有）",
                    "BODY": (entry.body or "")[:BODY_LIMIT] or "（这篇是空的）",
                    "TITLES": "\n".join("- " + item for item in _title_pool(entry, all_entries)) or "（库里还没有别的笔记）",
                },
            )
            try:
                reply = await llm.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=700,
                )
                raw = parse_json(reply.text)
            except Exception as exc:  # noqa: BLE001 - 单篇失败不该毁掉整批
                failed.append({"path": rel, "error": str(exc)[:160]})
                continue
            shaped = _coerce(raw)
            links, dropped = clean_links(shaped["links"], titles=titles)
            dropped_total += dropped
            out.append(
                {
                    "path": rel,
                    "title": entry.title,
                    "tags": clean_tags(shaped["tags"], known=known_tags),
                    "links": links,
                    "dropped": dropped,
                }
            )
            if on_progress is not None:
                on_progress(rel)
    return {
        "items": out,
        "failed": failed,
        "dropped": dropped_total,
        "model": cfg.model,
    }


# ------------------------------------------------------------------ 落盘

#: 双链追加到正文的这一节下面。写正文而不是写进 YAML 头：链接是"读的时候想点"的东西，
#: 放在读得到的地方才有用；属性面板里那一条只有整理的时候才看得到。
SECTION = "## 相关"


def apply(lib: notelib.Library, items: list[dict[str, Any]]) -> dict[str, Any]:
    """把**被接受**的建议写进文件。两条路都先留快照（`notelib` 的写入路径自带）。

    **幂等**：已经在用的标签不重复加；正文里已经写着 `[[标题]]` 的不再追加一行。
    这条不是洁癖 —— 人复核时会来回点，重复追加会把正文越堆越乱，而且没人愿意回头清。

    标签进 YAML 头、双链进正文尾部一节，两处都留了痕迹让人看得出来"这是后面补的"
    （键名就叫 `相关`，不是伪装成原文）。
    """
    found = notelib.index(lib)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    snapshots: list[str] = []

    for item in items or []:
        rel = str((item or {}).get("path") or "").strip()
        entry = found.entry(rel)
        if entry is None:
            skipped.append({"path": rel, "reason": "库里没有这一篇"})
            continue

        added_tags = [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip() and str(tag).strip() not in entry.tags]
        body = entry.body or ""
        fresh = [
            link
            for link in (item.get("links") or [])
            if str(link.get("target") or "").strip() and f"[[{str(link['target']).strip()}]]" not in body
        ]

        if not added_tags and not fresh:
            skipped.append({"path": rel, "reason": "没有新东西可写（都已存在）"})
            continue

        if added_tags:
            notelib.set_meta(lib, rel, {"tags": list(entry.tags) + added_tags})

        if fresh:
            lines = []
            for link in fresh:
                target = str(link.get("target")).strip()
                why = str(link.get("why") or "").strip()
                lines.append(f"- [[{target}]]" + (f" —— {why}" if why else ""))
            block = SECTION + "\n\n" + "\n".join(lines) + "\n"
            text = (body.rstrip() + "\n\n" + block) if body.strip() else block
            notelib.write_body(lib, rel, text, why="suggest")
            snapshots.append(rel)

        applied.append({"path": rel, "tags": added_tags, "links": [link["target"] for link in fresh]})

    return {"applied": applied, "skipped": skipped, "touched": [row["path"] for row in applied], "snapshots": snapshots}


