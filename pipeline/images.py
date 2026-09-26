"""把材料里的图接进检索：**图里的字 OCR 出来、图的内容让模型说一句**，都写回正文。

## 为什么必须写回正文

检索只认正文（切片 → 窗口 → 向量）。图里的信息如果不落到正文里，它对检索就是
**不存在**的。实测那批网页材料：148 条图片引用，只有 34% 带 alt，而其中大半是
"图片"两个字 —— 于是问"这张图画了什么"，向量里没有任何东西可匹配。

## 两件事，各有分工

* **OCR**（RapidOCR，本地、免费）：图里的**字**。技术文章里的图多半是表格、曲线、
  终端截图，信息就在字上。`pipeline/ocr.py` 的引擎直接拿来用 —— 它本来对"没有文字层
  的 PDF"做的就是同一件事，`lines_of()` 其实吃任意图片。
* **模型读图**（`llm.vision()`）：图里没字或字少的（示意图、架构图、照片），
  只有它能说出来。

两条都在行首标来路（`图里的字（OCR）` / `图说明（模型读图）`）：**"这是机器从图里
读出来的"与原生正文不是一个可信度**，不能混 —— 这条规矩是 `ocr.py` 立的，这里沿用。

## 写进去长什么样（幂等）

    ![原图](src)
    > 图说明（模型读图）：…
    > 图里的字（OCR）：…

重跑时先把**带这两个前缀的行**整段删掉再写，所以反复跑不会越写越多，也不会因为换了
模型留下两版说明。这也意味着：**这两行会被切进切片、被向量化**，于是图的内容进了检索；
引用落在它们上面时，读者一眼能看出这段话是机器从图里读的。

## 图从哪来

* **相对路径**：按原件所在目录解析（旁注里的 `source` 就是原件；保存网页带下来的
  `*_files/` 就在它旁边）；
* **http(s)**：抓一份存本机（`data/library/.text/.media/<citekey>/`，按 URL 哈希命名）。
  实测微信 CDN 那类链接现在还能取到（200 / image/png）—— 不抓的话前端里就是破图，
  也 OCR 不了。

`.media` 藏在 `.text` 里（隐藏目录）：扫描与列表都不看它，它只是这些图的落脚点。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from sqlalchemy import select

from . import config, ocr
from .llm import LLM, LLMError, parse_json

sys.path.insert(0, str(config.ROOT / "api"))

from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Material  # noqa: E402

#: 正文里的图片引用（`htmlmd` 落下来的形状就是 `![alt](src)`，`ingest.IMG_RE` 同源）
IMG_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)\s*$")

#: 我们写进去的那两行的行首。**认它们就等于认整段**（重跑时先删后写）。
OCR_PREFIX = "> 图里的字（OCR）："
CAPTION_PREFIX = "> 图说明（模型读图）："

#: 单张图多大就不抓。整篇的资源也就几十 MB 级，一张超过这个数基本不是插图。
MAX_IMAGE_BYTES = 12 * 1024 * 1024
#: 一张图 OCR 出来留多少字（图里的字通常几十到几百，给足余量）
MAX_OCR_CHARS = 2000
#: 读图的提示词。要它只描述看得见的 —— 图和正文不同，模型很容易替图"补剧情"。
CAPTION_PROMPT = (
    "这是一篇技术文章里的一张图。用一两句中文说清它画的是什么、信息量在哪"
    "（表格？曲线？终端输出？示意图？）。只说你看见的，不要推测文章没写的东西。"
    '输出 JSON：{"caption": "……"}'
)


def text_dir() -> Path:
    return Path(get_settings().data_dir) / "library" / ".text"


def media_dir(citekey: str) -> Path:
    """抓下来的图放哪。`mkdir` 在这里做 —— 抓图的路径上到处都要它。"""
    path = text_dir() / ".media" / citekey
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_refs(lines: list[str]) -> list[tuple[int, str, str]]:
    """正文里的图片引用：`[(行号(0 基), alt, src)]`。"""
    out: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = IMG_RE.match(line.strip())
        if match:
            out.append((index, match.group("alt").strip(), match.group("src").strip()))
    return out


def source_dir(citekey: str, folder: Path | None = None) -> Path | None:
    """原件所在目录 —— 相对路径的图就是相对它。取旁注里的 `source`。

    网页抓下来的那批，`source` 也可能是 URL：那种情况没有"本机原件目录"可用，
    返回 None（它的图要么是 CDN 绝对地址、要么找不到，两条路都不靠这个）。
    """
    folder = folder or text_dir()
    for sidecar in (folder / f"{citekey}.json", folder / f"{citekey}.meta.json"):
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raw = str(data.get("source") or "").strip()
        if not raw or raw.startswith(("http://", "https://")):
            return None
        return Path(raw.replace("\\", "/")).parent
    return None


def fetch(citekey: str, src: str, *, log=print) -> Path | None:  # noqa: ANN001
    """http(s) 的图抓一份到本机。按 URL 哈希命名 → **抓过就不重复抓**。"""
    suffix = (Path(urlparse(src).path).suffix or ".img").lower()[:6]
    stamp = hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
    target = media_dir(citekey) / f"{stamp}{suffix}"
    if target.is_file() and target.stat().st_size > 0:
        return target
    try:
        request = Request(src, headers={"User-Agent": "Mozilla/5.0 (quizforge)"})
        with urlopen(request, timeout=45) as response:  # noqa: S310
            kind = str(response.headers.get("Content-Type") or "")
            if not kind.startswith("image/"):
                log(f"    ! 不是图片（{kind[:30]}），跳过：{src[:56]}")
                return None
            data = response.read(MAX_IMAGE_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        log(f"    ! 抓不到（{type(exc).__name__}）：{src[:56]}")
        return None
    if len(data) > MAX_IMAGE_BYTES:
        log(f"    ! 图超过 {MAX_IMAGE_BYTES // 1048576} MB，跳过：{src[:56]}")
        return None
    target.write_bytes(data)
    return target


def resolve(citekey: str, src: str, base: Path | None, *, fetch_remote: bool = True, log=print) -> Path | None:  # noqa: ANN001
    """这条引用对应本机的哪个文件（找不到就 None）。"""
    if src.startswith("data:"):
        return None
    if src.startswith(("http://", "https://")):
        return fetch(citekey, src, log=log) if fetch_remote else None
    name = unquote(src.split("?")[0].split("#")[0])
    candidates = [media_dir(citekey) / Path(name).name]
    if base is not None:
        candidates.insert(0, base / name)
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def read_text_from_image(path: Path) -> str:
    """图里的字。读不出来就是空字符串 —— 一张图失败不该毁掉整批。"""
    try:
        lines, _score = ocr.lines_of(path)
    except Exception:  # noqa: BLE001
        return ""
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        text = " ".join(str(line).split())
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        kept.append(text)
    return " ".join(kept)[:MAX_OCR_CHARS]


async def caption_image(llm: LLM, path: Path) -> tuple[str, int, int]:
    """让模型说一句这张图画的是什么。返回 `(说明, 输入 token, 输出 token)`。

    失败就空着、不抛：一张图读不出来不该毁掉整批；用量照样要带回去 —— **读图是花钱
    的那一步**，跑完得能报出这笔账（这个项目别的批处理都这么干，见 `pipeline/fix.py`）。
    """
    try:
        reply = await llm.vision(path, CAPTION_PROMPT)
        data = parse_json(reply.text)
    except (LLMError, ValueError):
        return "", 0, 0
    text = (
        str(data.get("caption") or "").strip()
        if isinstance(data, dict)
        else str(data or "").strip()
    )
    return text, int(reply.tokens_in or 0), int(reply.tokens_out or 0)


def strip_blocks(lines: list[str]) -> list[str]:
    """删掉上一次写进去的那些行（重跑不叠加）。"""
    return [
        line
        for line in lines
        if not line.strip().startswith((OCR_PREFIX, CAPTION_PREFIX))
    ]


def render_block(caption: str, ocr_text: str) -> list[str]:
    """要插在图片引用下面那几行（两条都空就什么都不插）。"""
    out: list[str] = []
    if caption:
        out.append(CAPTION_PREFIX + caption)
    if ocr_text:
        out.append(OCR_PREFIX + ocr_text)
    return out


async def describe_many(
    llm: LLM,
    jobs: list[tuple[str, str, Path | None]],
    *,
    workers: int,
    ocr_workers: int = 4,
    fetch_workers: int = 8,
    fetch_remote: bool = True,
    want_caption: bool,
    want_ocr: bool,
    stats: dict | None = None,
    log=print,
) -> dict[tuple[str, str], tuple[str, str]]:
    """每张图走一条流水线：**抓图 → （OCR ∥ 读图）**，三段各有闸门、互相重叠。

    `jobs` 是 `(citekey, src, 原件目录)`；抓图与"找不到"都在这里判（不是调用方先做），
    因为**抓图是这一段里最慢的一环**，串在流水线外面就等于把它单独排成一队。

    为什么值得这么排：三段吃的是**不同的资源** —— 抓图吃网络、OCR 吃本地 CPU（ONNX）、
    读图吃网络（另受 llm 自己那道 AIMD 闸门管）。串着做等于让两种资源轮流空转：
    原先那版 OCR 是一张一张来的，而读图的几路并发在等它的时间里网络是闲的。
    OCR 与读图对同一张图**同时**开跑（互不依赖），这是流水线真正的收益所在。

    `stats` 是调用方递进来的记账本（可变）：张数、token、找不到的、有产出的都记在里面。
    """
    gate_fetch = asyncio.Semaphore(max(1, fetch_workers))
    gate_ocr = asyncio.Semaphore(max(1, ocr_workers))
    gate_vision = asyncio.Semaphore(max(1, workers))
    bill = stats if stats is not None else {}
    for key in ("images", "tokens_in", "tokens_out", "missing", "done"):
        bill.setdefault(key, 0)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    total = len(jobs)

    async def one(citekey: str, src: str, base: Path | None) -> None:
        async with gate_fetch:
            local = await asyncio.to_thread(resolve, citekey, src, base, fetch_remote=fetch_remote, log=log)
        if local is None:
            bill["missing"] += 1
            out[(citekey, src)] = ("", "")
            return

        async def ocr_stage() -> str:
            async with gate_ocr:
                return await asyncio.to_thread(read_text_from_image, local)

        async def vision_stage() -> tuple[str, int, int]:
            async with gate_vision:
                return await caption_image(llm, local)

        stages: list[tuple[str, asyncio.Task]] = []
        if want_ocr:
            stages.append(("ocr", asyncio.ensure_future(ocr_stage())))
        if want_caption:
            stages.append(("vision", asyncio.ensure_future(vision_stage())))
        results = await asyncio.gather(*(task for _kind, task in stages))

        ocr_text, caption, tokens = "", "", (0, 0)
        for (kind, _task), value in zip(stages, results):
            if kind == "ocr":
                ocr_text = value
            else:
                caption = value[0]
                tokens = (value[1], value[2])
        out[(citekey, src)] = (caption, ocr_text)

        bill["images"] += 1
        bill["tokens_in"] += tokens[0]
        bill["tokens_out"] += tokens[1]
        if ocr_text or caption:
            bill["done"] += 1
        # 每完成一张就报一行：跑几分钟的任务，"看不出在动"和"卡住"是一样的
        log(
            f"    [{bill['images']:3d}/{total}] {citekey[:20]:20s} "
            f"说明 {len(caption):3d} 字 · OCR {len(ocr_text):4d} 字"
        )

    await asyncio.gather(*(one(key, src, base) for key, src, base in jobs))
    return out


def collect(db, wanted: list[str], *, all_depths: bool = False, log=print) -> dict[str, tuple[Path, list[str], list[tuple[int, str, str]]]]:  # noqa: ANN001
    """挑出要处理的材料：`{slug: (正文文件, 行, 图片引用)}`（只留真有图片的）。

    **默认只碰"检索"层**（资料库：网页、书）。出题层那 16 份材料有它们自己的读图
    支路（`worker` 的 vision 任务，结果进候选点），而这里做的是"把图写回正文、重切、
    重算向量" —— 重切会动到出题账本认的切片。那条链不该被"把资料接进检索"这件事
    顺手改掉。要一起做就 `--all-depths`，或者用 `--material` 点名（点名优先）。
    """
    stmt = select(Material).order_by(Material.slug)
    if wanted:
        stmt = stmt.where(Material.slug.in_(wanted))
    elif not all_depths:
        stmt = stmt.where(Material.depth == "检索")
    found: dict[str, tuple[Path, list[str], list[tuple[int, str, str]]]] = {}
    for material in db.scalars(stmt):
        path = Path(str(material.source_path or ""))
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        refs = image_refs(lines)
        if refs:
            found[material.slug] = (path, lines, refs)
    return found


def process(args) -> int:  # noqa: ANN001
    db = get_session_factory()()
    try:
        targets = collect(db, args.material, all_depths=args.all_depths)
        if not targets:
            print("没有带图片引用的材料（用 --material 指定，或确认这批材料里确实有图）")
            return 0
        # 抓图与"找不到"都在流水线里判（见 `describe_many`）—— 抓图是最慢的一环，
        # 串在流水线外面就等于把它单独排一队。只有 `--dry-run` 才就地解析一遍，
        # 而且**不抓远程**：预演不该动网络，也不该往盘上写东西。
        jobs: list[tuple[str, str, Path | None]] = []
        plan: dict[str, list[tuple[int, str]]] = {}
        skipped_done = 0
        for slug, (_path, lines, refs) in sorted(targets.items()):
            base = source_dir(slug)
            print(f"  {slug}：{len(refs)} 条图片引用")
            plan[slug] = []
            for index, _alt, src in refs:
                if not args.force and described_before(lines, index):
                    skipped_done += 1
                    continue
                if args.dry_run and resolve(slug, src, base, fetch_remote=False) is None:
                    print(f"    ! 找不到图（预演不抓远程）：{src[:56]}")
                    continue
                plan[slug].append((index, src))
                jobs.append((slug, src, base))
        if skipped_done:
            print(f"  已处理过、跳过 {skipped_done} 张（要看新的加 --force）")
        if not args.no_ocr:
            # 让人一眼看到 OCR 到底跑在卡上还是 CPU 上（"到底用没用 GPU"这个问题，
            # 靠猜没有意义 —— 上一版就是因为 det 上了 GPU、rec 还在 CPU，看着像成功）
            print(f"  OCR provider：{ocr.providers()}")

        if args.dry_run:
            print(f"\n（预演）{len(jobs)} 张图待处理：抓图/OCR/读图都没做，正文也没改")
            return 0

        stats: dict[str, int] = {}
        planned = len(jobs)

        async def run_all() -> dict[tuple[str, str], tuple[str, str]]:
            async with LLM(config.load()) as llm:
                return await describe_many(
                    llm, jobs,
                    workers=args.workers, ocr_workers=args.ocr_workers,
                    fetch_workers=args.fetch_workers, fetch_remote=not args.no_fetch,
                    want_caption=not args.no_caption, want_ocr=not args.no_ocr,
                    stats=stats,
                )

        described = asyncio.run(run_all()) if jobs else {}
        if jobs:
            print(
                "处理 %d 张：拿到 %d 张（找不到 %d）· 读图用量 %d k in / %d k out"
                % (planned, stats.get("images", 0), stats.get("missing", 0),
                   stats.get("tokens_in", 0) // 1000, stats.get("tokens_out", 0) // 1000)
            )

        changed = 0
        for slug, (path, lines, _refs) in sorted(targets.items()):
            body = strip_blocks(lines)
            inserted = 0
            for _index, src in plan.get(slug, []):
                caption, ocr_text = described.get((slug, src), ("", ""))
                block = render_block(caption, ocr_text)
                if not block:
                    continue
                # 按引用行本身定位：重跑时正文可能已经变了，行号不作数，**按内容找**
                needle = f"]({src})"
                for position, line in enumerate(body):
                    if line.strip().endswith(needle):
                        body[position + 1:position + 1] = block
                        inserted += 1
                        break
            if inserted and body != lines:
                path.write_text("\n".join(body) + "\n", encoding="utf-8")
                changed += 1
                print(f"  {slug}：写进 {inserted} 张图的说明与 OCR 文本（{len(lines)} → {len(body)} 行）")
        print(f"\n改了 {changed} 份正文。接着重切 + 算向量（`make images` 已经把这两步串好了）：")
        print("   python -m pipeline.intake && python -m pipeline.embed")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.images",
        description="把材料里的图接进检索（图的字 OCR + 模型读图说明，写回正文）",
    )
    parser.add_argument("--material", action="append", default=[], help="只做这几份（可给多次）")
    parser.add_argument("--dry-run", action="store_true", help="只列要做什么，不动正文、不抓图")
    parser.add_argument("--no-ocr", action="store_true", help="不做 OCR（只要模型读图）")
    parser.add_argument("--no-caption", action="store_true", help="不读图（只要 OCR，省钱）")
    parser.add_argument("--no-fetch", action="store_true", help="不抓远程图（只认本机已有的）")
    parser.add_argument("--force", action="store_true",
                        help="已经处理过的图也重来（默认跳过：读图是按张收费的）")
    parser.add_argument("--workers", type=int, default=8,
                        help="读图的并发（默认 8；llm 客户端另有一道自适应闸门）")
    parser.add_argument("--ocr-workers", type=int, default=4, help="OCR 的并发（默认 4，本地 CPU/GPU）")
    parser.add_argument("--fetch-workers", type=int, default=8, help="抓图的并发（默认 8）")
    parser.add_argument("--all-depths", action="store_true",
                        help="连出题层一起做（默认只做检索层：那层的切片归出题账本管）")
    args = parser.parse_args(argv)
    return process(args)


if __name__ == "__main__":
    raise SystemExit(main())
