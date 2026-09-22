"""worker：认领任务 → 调 LLM → 写结果文件 → 记账（pipeline.md §6）。

一个 worker 一个 SQLite 连接；N 个 worker 协程并发跑，靠任务表的租约互不打架。

用法：
    api/.venv/bin/python -m pipeline.worker --kind extract --concurrency 4
    api/.venv/bin/python -m pipeline.worker --kind vision  --max 2
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config, dbstore, store
from .llm import LLM, LLMError, parse_json

IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>', re.IGNORECASE)


# ------------------------------------------------------------------ 小工具

def load_prompt(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def prompt_version(name: str) -> str:
    """提示词版本号：写进任务记录，将来「为什么这批结果不一样」有据可查。

    **契约也算进去**：模板里用了 `{{CONTRACT}}` 的角色，它实际收到的提示词 = 模板 + 契约，
    而契约就是仓库里那几个文件、改起来很顺手。只 hash 模板的话，改了契约版本号纹丝不动 ——
    恰好漏掉最常变的那部分，而版本号本来要回答的正是「为什么这批结果不一样」。
    """
    template = load_prompt(name)
    digest = hashlib.sha256(template.encode("utf-8"))
    if "{{CONTRACT}}" in template:
        digest.update(load_contract().encode("utf-8"))
    return digest.hexdigest()[:8]


def render(template: str, mapping: dict) -> str:
    text = template
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def render_slice_text(source: Path, start: int, end: int) -> str:
    """按行取切片正文，**行首带上真实行号**；把 <img> 换成图占位符（图由视觉员单独处理）。

    行号必须带：抽取员要按 `source` 回报出处，看不到真实行号它就会自己猜 ——
    实测它猜出来的行号全部落进目录页（L8–L20），于是出题员拿到的是目录、
    校验员一句「材料中找不到依据」把整批题打回。角色之间的接口，
    不能指望对方凭上下文补齐。
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for index in range(max(0, start - 1), min(len(lines), end)):
        line = lines[index]
        match = IMG_RE.search(line)
        body = f"[[图: {match.group(1)} | 说明: 见视觉员]]" if match else line
        out.append(f"{index + 1}: {body}")
    return "\n".join(out)


def slice_hash(source: Path, start: int, end: int, prompt_ver: str) -> str:
    """幂等键 = 切片正文 + 提示词版本。内容变了才重跑。"""
    text = render_slice_text(source, start, end)
    return hashlib.sha256(f"{text}\n--{prompt_ver}".encode("utf-8")).hexdigest()[:16]


def normalize_points(data) -> list[dict]:
    if isinstance(data, dict):
        for key in ("points", "items", "知识点"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data] if data else []
    if isinstance(data, list):
        return data
    raise LLMError(f"抽取结果结构不对：{type(data).__name__}")


# ------------------------------------------------------------------ 任务体

async def run_extract(llm: LLM, payload: dict):
    source = Path(payload["source_path"])
    start, end = payload["lines"]
    meta = {k: payload.get(k) for k in ("slice_id", "path", "summary", "tokens", "figures")}
    prompt = render(
        load_prompt("extract"),
        {
            "SLICE_META": json.dumps(meta, ensure_ascii=False, indent=2),
            "SLICE_TEXT": render_slice_text(source, start, end),
        },
    )
    reply = await llm.chat([{"role": "user", "content": prompt}])
    points = normalize_points(parse_json(reply.text))
    # **直接入库**（`point_candidates`）：抽取结果是知识空间的原料，
    # 落成文件会立刻多出第二个形状 —— 下游要么读文件、要么读库，迟早分叉
    # （实测分叉过一次：抽取员给单数 `source`，归并读复数 `sources`）。
    saved = dbstore.save_extract(str(payload["map"]), str(payload["slice_id"]), {"points": points})
    return f"db:{payload['slice_id']}/{saved}候选", reply


async def run_vision(llm: LLM, payload: dict):
    image = Path(payload["image_path"])
    if not image.is_file():
        raise LLMError(f"图片不存在：{image}")
    prompt = render(
        load_prompt("vision"),
        {"FIGURE_META": json.dumps(payload.get("figure", {}), ensure_ascii=False, indent=2)},
    )
    reply = await llm.vision(image, prompt)
    data = parse_json(reply.text)
    # 图的事实**直接入库**：图是这条流水线的一等公民，它读出来的空间关系
    # （谁连谁、箭头朝哪）常常是正文没写全的 —— 与文字候选走同一条归并路径。
    saved = dbstore.save_vision(
        str(payload["map"]),
        str(payload["figure_id"]),
        {"loc": payload.get("loc") or {}, "result": data},
    )
    return f"db:{payload['figure_id']}/{saved}事实", reply


# -------------------------------------------------- 出题单元（A4 出题员 → A5 校验员）

#: 出题员的契约文件，路径**仓库根相对**。
#:
#: 四层各一份 —— 派工时写得很清楚：「**四层共用这一条流水线**」（见
#: `dispatch.dispatch_author`），一个包该出哪几层由知识点自己的 `layers` 决定，
#: 所以四层的判定标准都得在手里。只给两层的话，另外两层的题就是在**没有判定表**
#: 的情况下出的（库里那 199 道应用层题正是如此）。
#:
#: 层契约住在 `pipeline/prompts/layers/`（它们是**出题机的提示词**，跟着 pipeline 走）；
#: 格式契约留在 `.codebuddy/skills/` 下 —— 那是**题目**的格式规范，人和 agent 改题时
#: 同样要守，不是出题机私有。契约因此分散在两处，这里点名到文件，不假定同一个基目录。
CONTRACT_FILES = [
    "pipeline/prompts/layers/l1-memorize.md",
    "pipeline/prompts/layers/l2-understand.md",
    "pipeline/prompts/layers/l3-apply.md",
    "pipeline/prompts/layers/l4-transfer.md",
    ".codebuddy/skills/quizforge-author/references/format.md",
]


def load_contract() -> str:
    """契约**从仓库文件里读**，不在提示词里重抄——契约永远以文件为准。

    读不到就报错，**不静默跳过**：少一份契约，出题照样能跑，只是出的题悄悄变差，
    而任务记录里看不出任何异常 —— 这种失败最难回头发现（原先这里是
    `if path.is_file()`，改一次文件名就少一份契约，谁也不会知道）。
    """
    parts: list[str] = []
    for rel in CONTRACT_FILES:
        path = config.ROOT / rel
        if not path.is_file():
            raise LLMError(f"契约文件读不到：{rel}（找的是 {path}）")
        parts.append(f"<!-- 契约：{rel} -->\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def normalize_ranges(value) -> list[list[int]]:
    """把各种形状的行区间统一成 [[起, 止], ...]。

    模型与中间文件给过三种形状：单区间 `[起, 止]`、区间列表 `[[起, 止], ...]`、
    以及被多包一层的 `[[[起, 止]]]`。在这里一次收干净，后面的代码只面对一种形状 ——
    否则一个模型随手多套一层括号，整批任务就会在同一行代码上崩掉。
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip("-").isdigit())
        for v in value
    ):
        value = [value]
    out: list[list[int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            continue
        if end >= start > 0:
            out.append([start, end])
    return out


def read_ranges(source_path: str, ranges: list) -> str:
    """按行区间取原文，去重并按行号排序；行首带原行号，方便模型写 source。"""
    lines = Path(source_path).read_text(encoding="utf-8").splitlines()
    seen: set[int] = set()
    out: list[str] = []
    for start, end in sorted((r[0], r[1]) for r in normalize_ranges(ranges)):
        for index in range(max(0, start - 1), min(len(lines), end)):
            if index in seen:
                continue
            seen.add(index)
            out.append(f"{index + 1}: {lines[index]}")
    return "\n".join(out)


def locate_quote(source_path: Path, quote: str) -> list[list[int]]:
    """把**逐字引用**定位成行号区间（确定性、可核对）。

    为什么不让模型自己写行号：实测它写的整体是错的 —— 题目问 Baby RISC-V 的数量，
    依据却写成 L121（那是 noc_async_read 的段落）。而校验员只能看到声明的区间，
    于是正确的问题也被判「材料中找不到依据」，整批作废。

    分工原则：**引用是模型擅长的**（逐字抄原文），**行号是程序擅长的**（能核对）。
    抄不出来的引用允许让它整包作废（宁可重跑，也不要一条查不到依据的题）。
    """
    text = (quote or "").strip()
    if not text:
        return []
    lines = source_path.read_text(encoding="utf-8").splitlines()
    probes: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*\d+\s*[:：]\s*", "", line).strip()  # 容忍 "82: cb_reserve_back …"
        if cleaned:
            probes.append(cleaned)
    if not probes or len(probes[0]) < 8:
        return []
    for index, line in enumerate(lines):
        if probes[0] not in line:
            continue
        # 首行找到锚点后**逐行往下核对**：抄了几行就延到几行。
        # 于是「quote 抄得越完整」= 校验员看到的窗口越完整 ——
        # 之前只定位一行，题目依据稍长一点就被判「材料里找不到依据」。
        end = index
        for offset, probe in enumerate(probes[1:], start=1):
            nxt = index + offset
            if nxt < len(lines) and probe in lines[nxt]:
                end = nxt
            else:
                break
        return [[index + 1, end + 1]]
    return []


def _front_matter_block(meta: dict) -> str:
    rows = []
    for key, value in meta.items():
        rows.append(f"{key}: {json.dumps(value, ensure_ascii=False)}" if isinstance(value, str) else f"{key}: {value}")
    return "\n".join(rows)


async def run_author(llm: LLM, payload: dict):
    pack = {
        "pack_id": payload["pack_id"],
        "材料": payload["material"],
        "本批目标层": payload.get("layer_target"),
        "起始编号建议": payload.get("start_index"),
        "知识点": payload.get("points"),
    }
    prompt = render(
        load_prompt("author"),
        {
            "CONTRACT": load_contract(),
            "PACK": json.dumps(pack, ensure_ascii=False, indent=1)
            + "\n\n材料原文（行首数字是原文行号）：\n"
            + read_ranges(payload["source_path"], payload["ranges"]),
            "EXISTING": "\n".join(f"- {s}" for s in payload.get("existing") or []) or "（无）",
        },
    )
    reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=8000)
    data = parse_json(reply.text)
    questions = data.get("questions") or []

    items: list[dict] = []
    skipped: list[str] = []
    for question in questions:
        meta = dict(question.get("front_matter") or {})
        body = str(question.get("markdown") or "").strip()
        if not body:
            continue
        # 依据由**程序**从逐字引用里定位（模型不写行号，见 locate_quote）
        ranges = locate_quote(Path(payload["source_path"]), str(question.get("quote") or ""))
        if not ranges:
            skipped.append(str(meta.get("id") or "?"))
            continue
        # 编号由**库里**分配（dbstore.next_ids），模型写的 id 一律丢弃 ——
        # 文件时代靠"扫目录取最大值"，多包并行必然撞号。
        items.append(
            {
                "front": meta,
                # 作者声明这道题考的是包内哪个点 —— 校验通过时据此写"题↔点的边"，
                # 那是"哪些点已经出过题"的唯一精确依据（防重复出题）。
                "point": question.get("point"),
                # 轮次：重出的题要记着自己被重出过几次，否则"最多重出 N 轮"无从收敛
                "round": int(payload.get("round") or 0),
                "markdown": body,
                "sources": ranges,
                # 校验窗口 = 本题依据 ∪ 任务包里该点的全部出处。
                # 窗口太窄会被判「材料里找不到依据」，实测这类失败占一半以上。
                "window": sorted(
                    {tuple(r) for r in ranges} | {tuple(r) for r in (payload.get("ranges") or [])}
                ),
            }
        )

    if skipped:
        print(
            f"[WARN] {len(skipped)} 道题的依据无法在原文中定位，已丢弃：{'、'.join(skipped)}",
            file=sys.stderr,
        )
    if not items:
        raise LLMError("整包题目都因依据无法定位被丢弃 —— quote 不是逐字原文？")

    subject = str(payload.get("subject") or "tt-arch")
    written, dropped = dbstore.insert_drafts(
        subject, items, [str(p.get("key") or "") for p in (payload.get("points") or [])]
    )
    if not written:
        raise LLMError(f"整包题目都没能入库：{dropped}")
    if dropped:
        print(f"[WARN] {len(dropped)} 道题格式不合契约，未入库：{'、'.join(dropped)}", file=sys.stderr)
    return (
        {
            "label": f"db:{payload['pack_id']}/{len(written)}道",
            "questions": written,
            "dropped": dropped,
        },
        reply,
    )


async def run_verify(llm: LLM, payload: dict):
    record = dbstore.load_question(str(payload.get("question_id") or ""))
    if record is None:
        raise LLMError(f"库里没有这道题：{payload.get('question_id')}")
    raw = record["raw_markdown"]
    # 硬约束用代码兜：**校验员看不到「## 解析」**。靠提示词自觉是不够的。
    stem = re.split(r"^## 解析\s*$", raw, flags=re.MULTILINE)[0]
    prompt = render(
        load_prompt("verify"),
        {
            "SOURCE_TEXT": read_ranges(payload["source_path"], payload["ranges"]),
            "QUESTION": stem,
        },
    )
    reply = await llm.chat([{"role": "user", "content": prompt}], max_tokens=2000)
    data = parse_json(reply.text)
    verdict = str(data.get("verdict") or "")
    report = {
        "question": payload.get("question_id"),
        "verdict": verdict,
        "problems": data.get("problems") or [],
        "suggestions": data.get("suggestions") or [],
        "model": reply.model,
        "prompt_version": payload.get("prompt_version"),
        "tokens": {"in": reply.tokens_in, "out": reply.tokens_out},
    }
    # 判定**直接落库**：通过 → 已校验；不通过 → 留在草稿，理由记在 verify_report
    dbstore.save_verdict(str(payload.get("question_id")), verdict, report)
    return (
        {
            "label": f"{payload.get('question_id')}:{verdict}",
            "verdict": verdict,
            "problems": report["problems"],
        },
        reply,
    )


# ------------------------------------------------------------------ 运行日志

LOG_PATH = config.ROOT / "pipeline" / "run.log"

# 事件驱动唤醒：任何 worker 完成一个任务（可能顺带入了队）都会 set 它，
# 等活的 worker 立刻醒来重试认领。替代固定 1.5s 轮询 —— 轮询会让整条链在
# 每一步都白等半个周期，几十步累积起来就是"看着像卡住"。
WAKE = asyncio.Event()


def log(message: str, *, stderr: bool = False) -> None:
    """带时间戳同时写到 stdout 与 pipeline/run.log。

    理由很实在：一个出题任务要跑两三分钟，如果只在结束时打一行，
    期间屏幕上什么都没有 —— 看起来就像卡住了。所以开工也要喊一声。
    log 同时负责叫醒等待中的 worker（见 WAKE）。
    """
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, file=sys.stderr if stderr else sys.stdout, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    WAKE.set()


def queue_depth(conn) -> str:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "空"


# ------------------------------------------------------------------ 主循环

async def worker_loop(name: str, kind: str | None, cfg, llm: LLM, counter: dict, cap: int) -> None:
    conn = store.connect()
    try:
        while True:
            if cap and counter["done"] + counter["failed"] >= cap:
                return
            # 成本闸门：认领**之前**先算账本。花钱的是认领之后那次调用，
            # 所以这里是唯一能真正止血的位置 —— 不认领就不会调用，不会调用就不会扣费。
            # （教训：自动重出成环时队列会自我繁殖，没有这道闸门，几分钟就能烧完余额。）
            spent, _tin, _tout = store.spend(conn, cfg.rate_in_yuan, cfg.rate_out_yuan)
            # 预算算的是**本次运行的增量**，不是历史累计：账本早就超过任何小预算了，
            # 拿累计值比会把流水线永久锁死（实测：¥2 的预算被 ¥75 的历史账直接卡住）。
            # 第一个 worker 记下起点，其余共用（都在启动后几毫秒内，误差可忽略）。
            base = counter.setdefault("base_spent", spent)
            if cfg.budget_yuan and (spent - base) >= cfg.budget_yuan:
                log(
                    f"[{name}] 预算用尽：已花 ¥{spent:.2f} ≥ 上限 ¥{cfg.budget_yuan:.2f}"
                    f"（in {_tin} / out {_tout} tokens），停止认领"
                )
                return
            # 认领本身也可能失败（64 个写者抢 SQLite 时会 database is locked）。
            # 以前 claim 写在 try 之外 —— 一次这样的异常就逃出 worker_loop，
            # 而 gather 又没有 return_exceptions，整轮 run 直接崩；
            # 更糟的是它已认领的任务停在 running，租约过期后被别人重跑 = 重复扣费。
            try:
                task = store.claim(conn, name, cfg.lease_s, kind)
            except Exception as exc:  # noqa: BLE001
                log(f"[{name}] 认领失败（{type(exc).__name__}: {exc}），2 秒后重试", stderr=True)
                await asyncio.sleep(2.0)
                continue
            if task is None:
                # 只看**自己负责的**那一类：否则 --kind author 的 worker 会在只剩 verify 任务时
                # 一直空转（数的是全局队列），看起来像卡住。
                if kind:
                    pending = conn.execute(
                        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','running') AND kind=?",
                        (kind,),
                    ).fetchone()["n"]
                else:
                    pending = conn.execute(
                        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','running')"
                    ).fetchone()["n"]
                if not pending:
                    return
                # 事件驱动，不用固定轮询：任何 worker 完成一个任务（可能顺带入了队）
                # 都会 set 这个事件，等待者立刻醒来重试认领。轮询是「看着像卡住」的元凶之一。
                WAKE.clear()
                try:
                    await asyncio.wait_for(WAKE.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass  # 看门狗：兜住由**另一个进程**派发进来的任务
                continue
            started = time.time()
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','running')"
            ).fetchone()["n"]
            log(f"[{name}] start {task['id']}  剩余 {remaining}  队列 {queue_depth(conn)}")
            try:
                chained = 0
                if task["kind"] == "extract":
                    path, reply = await run_extract(llm, task["payload"])
                elif task["kind"] == "vision":
                    path, reply = await run_vision(llm, task["payload"])
                elif task["kind"] == "author":
                    result, reply = await run_author(llm, task["payload"])
                    path = result["label"]
                    # 单元内串联：题目一入库，立刻为**每道题**入队一个校验任务
                    origin = {
                        k: task["payload"][k]
                        for k in ("map", "material", "subject", "source_path", "pack_id", "points",
                                  "layer_target", "start_index", "existing", "out_dir",
                                  "prompt_version", "ranges",
                                  # ``round`` 必须带上：少了它，重出任务派生出的校验任务
                                  # 会把失败重新当成「第 1 轮」，于是「失败→重出」可以无限循环。
                                  # 实测 6231 个校验任务里只有 18 个带着 round，代价约 ¥41（七成开销）。
                                  "round")
                        if k in task["payload"]
                    }
                    for item in result["questions"]:
                        verify_payload = {
                            "map": task["payload"]["map"],
                            "question_id": item["id"],
                            "source_path": task["payload"]["source_path"],
                            # 校验窗口 = 这道题定位到的依据 ∪ 任务包里该点的全部出处。
                            # 只用定位到的那几行太窄：依据稍长一点就被判「材料里找不到依据」，
                            # 实测这类 fact 失败占全部失败的一半以上 = 一半出题开销被丢掉。
                            "ranges": sorted(
                                {tuple(r) for r in (item.get("sources") or [])}
                                | {tuple(r) for r in (task["payload"].get("ranges") or [])}
                            ),
                            "out_dir": task["payload"].get("out_dir", ""),
                            "prompt_version": prompt_version("verify"),
                            "origin": origin,
                        }
                        # 题目内容哈希 + 校验提示词版本：题不变、提示词不变 → 不重复校验
                        digest = hashlib.sha256(
                            (
                                str(item["id"])
                                + json.dumps(verify_payload["ranges"], ensure_ascii=False)
                                + verify_payload["prompt_version"]
                            ).encode("utf-8")
                        ).hexdigest()[:16]
                        if store.add_task(conn, "verify", digest, verify_payload):
                            chained += 1
                elif task["kind"] == "verify":
                    result, reply = await run_verify(llm, task["payload"])
                    path = result["label"]
                    if result.get("verdict") != "pass":
                        # 单元内打回重出：把缺陷清单塞回一个新轮次的 author 任务（最多 2 轮）。
                        # **只重写没过的那一道**：整包重出会让一次失败放大成 6 道题的产出，
                        # 并发拉满时队列会被重出滚爆（实测一次跑出 22 个 author 任务）。
                        origin = task["payload"].get("origin") or {}
                        round_no = int(origin.get("round") or 1)
                        # **暂不自动重出**：多材料跑起来之后，每次校验失败都会生成新的出题任务，
                        # 而出题又生成新的校验 —— 队列从 1200 涨到 3700，是正反馈（实测）。
                        # 失败的题留着（coverage 报告里看得见），下一轮由"补缺口派工"统一补，
                        # 不再让单条失败沿链放大。要重新打开就把这里改回 round_no < 2。
                        if origin and round_no < 1:
                            retry = dict(origin)
                            retry["round"] = round_no + 1
                            retry["feedback"] = result.get("problems") or []
                            retry["rewrite"] = [
                                {
                                    "id": Path(task["payload"]["question_path"]).stem,
                                    "defects": result.get("problems") or [],
                                }
                            ]
                            digest = hashlib.sha256(
                                (
                                    retry["pack_id"]
                                    + json.dumps(retry["feedback"], ensure_ascii=False)
                                    + str(round_no)
                                ).encode("utf-8")
                            ).hexdigest()[:16]
                            if store.add_task(conn, "author", digest, retry):
                                chained += 1
                            print(f"[{name}]   ↳ 打回重出（第 {round_no + 1} 轮）", flush=True)
                else:
                    raise LLMError(f"未知 kind：{task['kind']}")
                store.finish(conn, task["id"], str(path), reply.tokens_in, reply.tokens_out)
                counter["done"] += 1
                log(
                    f"[{name}] done {task['id']}  {time.time() - started:.1f}s  "
                    f"in={reply.tokens_in} out={reply.tokens_out}"
                    + (f"  ↳ 入队 {chained}" if chained else "")
                    + f"  -> {path.name if isinstance(path, Path) else path}"
                )
            except Exception as exc:  # noqa: BLE001 —— 单个任务失败不该拖垮整批
                status = store.fail(
                    conn, task["id"], f"{type(exc).__name__}: {exc}", cfg.max_attempts
                )
                counter["failed"] += 1
                log(
                    f"[{name}] {status} {task['id']}  {type(exc).__name__}: {str(exc)[:220]}",
                    stderr=True,
                )
    finally:
        conn.close()


async def run(cfg, kind: str | None, concurrency: int, cap: int) -> dict:
    counter = {"done": 0, "failed": 0}
    # worker 数 = 闸门上限：worker 只是廉价协程，真正控制「在飞请求数」的是 AdaptiveLimiter。
    # worker 数若少于闸门上限，闸门永远吃不满 —— 因为根本没人去发那么多请求。
    workers = max(1, concurrency or cfg.max_concurrency)
    names = [f"w{i + 1}" for i in range(workers)]

    # 断点重跑的入口提示：把队列现状打出来，一眼看出「上次停在哪、这次接着跑什么」。
    conn = store.connect()
    pending_before = store.pending(conn, kind)
    queue_before = queue_depth(conn)
    conn.close()

    WAKE.clear()
    async with LLM(cfg) as llm:
        log(
            f"启动 {len(names)} 个 worker（闸门 {llm.limit.limit} → 上限 {llm.limit.maximum}）"
            f" · 待跑 {pending_before} 个 · 队列 {queue_before}"
        )
        try:
            # return_exceptions=True：单个 worker 出意外不能把整轮带下去（否则已认领的任务
            # 会停在 running 直到租约过期，被别人重跑 = 重复扣费）。
            results = await asyncio.gather(
                *(worker_loop(name, kind, cfg, llm, counter, cap) for name in names),
                return_exceptions=True,
            )
            crashed = [r for r in results if isinstance(r, BaseException)]
            if crashed:
                log(f"[WARN] {len(crashed)} 个 worker 异常退出：{crashed[0]!r}", stderr=True)
            log(f"结束：完成 {counter['done']} · 失败 {counter['failed']} · {llm.limit.describe()}")
        finally:
            # **断点收尾**：本进程还挂在 running 的任务放回 todo 并清租约。
            # 于是 Ctrl-C / 断网退出 / 被 kill 之后，下次启动立刻从断点接着跑，
            # 不用等租约过期；也不会留下「认领了却没跑完」的悬空任务。
            # （进程被 SIGKILL 时这里跑不到，由租约兜底 —— 见 config.lease_s。）
            conn = store.connect()
            placeholders = ",".join("?" for _ in names)
            released = conn.execute(
                "UPDATE tasks SET status='todo', lease_until=NULL, worker=NULL "
                f"WHERE status='running' AND worker IN ({placeholders})",
                names,
            ).rowcount
            conn.commit()
            conn.close()
            if released:
                log(f"断点收尾：{released} 个未完成任务已放回队列（下次启动接着跑）")
    return counter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.worker", description="跑任务的 worker")
    parser.add_argument(
        "--kind", default="all", choices=["extract", "vision", "author", "verify", "all"]
    )
    parser.add_argument("--concurrency", type=int, default=0, help="并发数，0=用配置里的值")
    parser.add_argument(
        "--max", dest="cap", type=int, default=0,
        help="本次最多处理多少任务；0 = 用配置里的上限（默认 20，防手滑跑全量）",
    )
    parser.add_argument("--requeue-dead", action="store_true", help="把 dead 任务打回 todo 再跑")
    args = parser.parse_args(argv)

    cfg = config.load()
    print(f"LLM: {cfg.describe()}")
    if args.requeue_dead:
        conn = store.connect()
        print(f"打回 dead：{store.requeue_dead(conn)} 个")
        conn.close()

    kind = None if args.kind == "all" else args.kind
    # 不传 --concurrency 就按闸门上限开 worker（0 = 自动）—— worker 数少于上限时闸门吃不满。
    # 任务数则相反：**默认给上限**。手滑跑全量是最贵的错误（实测一次烧到 ¥50），
    # 要放开必须显式 QF_MAX_RUN=0 或 --max 一个大数。
    cap = args.cap if args.cap else cfg.max_tasks_per_run
    counter = asyncio.run(run(cfg, kind, args.concurrency, cap))
    print(f"完成 {counter['done']} 个，失败 {counter['failed']} 个（本次任务上限 {'不限' if not cap else cap}）")
    return 0 if counter["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
