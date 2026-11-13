"""导入流水线：一个文件 → 材料 + 切片（**带正文**）。

用户的一句话是这个模块的全部需求（2026-11-08）：

> 用户把文件拖进去，就直接流水线切片向量处理掉了 —— 这就是"正文入库"啊！

所以上传接口落盘之后调这里，一次做完：

   解析成行文本 → 按标题/长度切分 → 写 `materials` + `material_slices`（含 text）
   → （模型就绪时）算向量

切分规则与出题机的 `pipeline/ingest.py` 同源（按 `##`/`###` 标题、太碎的并、太长的拆），
这里复刻一份是因为 `api/` 打包时不该依赖出题机那套重依赖。

边界：原文件**留在本机**（不进包、不上云）；上云的是切片与它的正文（`make library-push`）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from . import materials as mat_mod
from .models import Material, MaterialSlice

#: 标题行：`#`~`####` + 文字（与 `pipeline/ingest.py` 的 HEADING_RE 一致）
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")

MIN_LINES = 12      # 比这还短的片并进下一片
MAX_LINES = 260     # 比这还长的片按行拆开
SPLIT_AT = 180      # 拆的时候每片多少行

#: 一次送多少条切片给向量模型。**上限是内存**：批内按最长的那条 padding 到
#: 1024 位置，ONNX 那边的激活张量约 `批大小 × 1024 × 1024(hidden) × 4B`。
#: 整份材料一次送进去就是 19740 片 × 那一步 —— 2026-11-13 实测直接报
#: `Failed to allocate memory ... 41397780480`（41GB）。
#: 16 是实测的那个折中（本机：0.30 秒/片、峰值 RSS 约 2GB；32 只快 13%，
#: 激活却翻倍），与出题机那侧 `pipeline.embed` 的 BATCH 一致。
EMBED_BATCH = 16


def split_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """按标题切；返回 `[(起行, 止行, 标题), ...]`（1-based，闭区间）。"""
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            marks.append((index, match.group(2)))
    if not marks:
        marks = [(0, "(全文)")]
    if marks[0][0] != 0:
        marks.insert(0, (0, "(前言)"))
    marks.append((len(lines), ""))

    chunks: list[tuple[int, int, str]] = []
    for (start, title), (next_start, _) in zip(marks, marks[1:]):
        end = max(start + 1, next_start)
        chunks.append((start, min(end, len(lines)), title))

    merged: list[tuple[int, int, str]] = []
    for chunk in chunks:
        if merged and (chunk[1] - chunk[0]) < MIN_LINES:
            prev = merged[-1]
            merged[-1] = (prev[0], chunk[1], prev[2])
        else:
            merged.append(chunk)

    out: list[tuple[int, int, str]] = []
    for start, end, title in merged:
        while end - start > MAX_LINES:
            out.append((start, start + SPLIT_AT, f"{title}（上）"))
            start += SPLIT_AT
        out.append((start, end, title))
    return [(s + 1, e, t) for s, e, t in out if e > s]


def _slugify(text: str, limit: int = 60) -> str:
    out = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", str(text or "").lower()).strip("-")
    return out[:limit] or "doc"


def _write_text(slug: str, body: str) -> str:
    """把归一后的正文写进资料库的正文缓存，返回它的绝对路径。

    放在 `<数据目录>/library/.text/` —— 与浏览 PDF 时懒抽的那份同一个目录
    （`material_files` 的 `TEXT_DIR`）：都是"这份材料的纯文本"，分两处放
    只会让人猜哪份是准的。名字用材料 slug（带内容哈希），与那边的 citekey
    不会撞。
    """
    from .config import get_settings  # noqa: PLC0415 - 免得模块导入期碰配置

    folder = Path(get_settings().data_dir) / "library" / ".text"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}.txt"
    path.write_text(body, encoding="utf-8")
    return str(path)


def ingest_file(
    db: Any,
    path: Path | str,
    *,
    subject: str = "",
    depth: str = "检索",
    embed: bool = True,
    on_stage: Any = None,
    on_progress: Any = None,
) -> dict:
    """一个文件 → 材料 + 切片（带正文）+（可选）向量。幂等：同一份文件重跑会覆盖切片。

    `on_stage(stage)` 在阶段边界回调（`parse` / `slice` / `embed`）——
    流水线页面靠它显示"这份走到哪一步了"，也是暂停/取消的检查点。

    `on_progress(label, done, total)` 在两处长活里回调：`"OCR"`（逐页）与
    `"向量"`（逐批）。几百页 / 上万片要跑几十分钟，页面上只有一个阶段名的话，
    看上去和卡死没有区别。`label` 由这里给，`jobs` 只负责把它写进 `message`。

    没有单独的"正文"阶段：切片那一步就已经把正文截好写进 `MaterialSlice.text`
    （见下面那句"切片就是正文"）。以前在这里打过一次空标记，页面上就多出一个
    什么也不做的步骤。
    """

    def stage(name: str) -> None:
        # **先落盘，再回调。** 回调（`jobs._mark`）是**另开一个 session** 去改
        # `library_jobs` 的，而进到这里时本 session 往往刚 `db.flush()` 过
        # （`Material` 那一行），正握着库的写锁 —— 不先提交，那条连接会干等
        # `busy_timeout`（15 秒）然后报 `database is locked`。
        # 2026-11-13 用户第一次把 87MB 的 PDF 真的送进队列就撞上了：
        # 解析几分钟（无锁），解析完 `flush` 写 Material，随即 `stage("slice")`
        # 被自己锁死，前面几分钟白跑。
        # 语义上也更对：**阶段边界本来就是一个落盘边界** —— 已经走到下一步了，
        # 上一步的产出不该还悬在事务里。
        db.commit()
        if on_stage is not None:
            on_stage(name)

    def report(label: str):  # noqa: ANN202
        """把回调包成带标签的：OCR 与向量都要报数，页面上得知道是哪一步。"""
        if on_progress is None:
            return None
        return lambda done, total: on_progress(label, done, total)

    target = Path(path)
    stage("parse")
    # 按类型抽：PDF/docx 走 pdftotext/pandoc，纯文本才直接读 —— 直接按 UTF-8
    # 读 PDF 会得到 `%PDF` 与压缩流（见 `materials.text_lines` 的注释）。
    # 扫描件在 `text_lines` 里改走 OCR，那时会有逐页进度。
    lines, extracted = mat_mod.text_lines(str(target), on_page=report("OCR"))
    body = "\n".join(lines)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    slug = _slugify(target.stem) + "-" + digest[:8]
    # 抽出来的正文要留一份可回读的：切片存了正文，但 `read_lines` 与字面检索
    # 走的是 `materials.source_file()`，而它不能去读原始 PDF。
    text_path = _write_text(slug, body) if extracted else ""

    material = db.scalar(select(Material).where(Material.slug == slug))
    if material is None:
        # 同名的旧材料（内容变了 → 哈希变了 → slug 变了）先放过，保留历史
        material = Material(slug=slug)
        db.add(material)
    material.subject = subject or material.subject or "资料"
    material.title = material.title or target.stem
    material.source_path = str(target)
    material.text_path = text_path
    material.sha256 = digest
    material.lines = len(lines)
    material.depth = depth
    db.flush()

    # 切片整批换掉（重跑幂等）
    stage("slice")
    db.execute(delete(MaterialSlice).where(MaterialSlice.material_id == material.id))
    pieces = split_lines(lines)
    slices: list[MaterialSlice] = []
    for index, (start, end, title) in enumerate(pieces, start=1):
        slices.append(
            MaterialSlice(
                material_id=material.id,
                slice_id=f"sl-{index:03d}",
                path=[title] if title else [],
                start_line=start,
                end_line=end,
                summary=title,
                # **切片就是正文**：这里直接截出来存好，换台机器靠它读材料
                text="\n".join(lines[start - 1:end]),
                windows=1,
            )
        )
    db.add_all(slices)
    db.commit()

    embedded = 0
    if embed:
        stage("embed")
        try:
            embedded = _embed_slices(db, slices, on_progress=report("向量"))
        except Exception as exc:  # noqa: BLE001 - 向量是可选的，失败不该让入库失败
            # **取消不是失败**：进度回调发现任务被取消时抛的那个异常要放上去，
            # 否则用户点了取消还得等几十分钟算完（`_Canceled.cancelled` 是那个标记）。
            if getattr(exc, "cancelled", False):
                raise
            # 但不能沉默：整份材料"完成"了却一条向量也没有，检索里永远查不到它，
            # 页面上还写着成功（2026-11-13 那份 87MB 的 PDF 就是这样炸的）。
            logging.getLogger("quizforge").exception("向量化失败：%s", target)
            db.rollback()

    return {
        "slug": slug,
        "title": material.title,
        "lines": len(lines),
        "slices": len(slices),
        "chars": sum(len(s.text) for s in slices),
        "embedded": embedded,
    }


def _embed_slices(db: Any, slices: list[MaterialSlice], *, on_progress: Any = None) -> int:
    """给切片算向量（本地小模型没就绪就直接跳过，留给 `make embed`）。

    **一批一落盘**，不是整份算完再提交。两个理由：

    * 内存：整份材料一次送模型会撑爆，见 `EMBED_BATCH`；
    * 中断：这是几十分钟的长活，中途关机 / 取消时已经算完的那部分不该白扔 ——
      在此之前是一个大事务，进程一死全丢、重试从头再来（2026-11-13）。

    某一批炸了只停这一份，返回**已经算好的**条数：切片本身早就提交过了，
    向量是可选的，不该连累入库。失败原因必须写日志 —— 吞掉的话页面上
    只会看到"完成 · 0 条向量"，看不出是模型没就绪还是算炸了。
    """
    from . import local_embed  # noqa: PLC0415 - 重依赖，用到才导入

    if not local_embed.status().get("ready"):
        return 0

    from . import semantic  # noqa: PLC0415
    from .models import SliceEmbedding  # noqa: PLC0415

    model = str(local_embed.manifest().get("model") or "")
    total = len(slices)
    count = 0
    for offset in range(0, total, EMBED_BATCH):
        chunk = slices[offset : offset + EMBED_BATCH]
        try:
            vectors = local_embed.embed([s.text[:6000] for s in chunk], kind="passage")
        except Exception:  # noqa: BLE001 - 一批失败不该把整份材料带走
            db.rollback()
            logging.getLogger("quizforge").exception(
                "向量化中断：算了 %d/%d 片（已算的留在库里，重试会整份重来）", count, total
            )
            return count
        for row, vector in zip(chunk, vectors):
            db.add(
                SliceEmbedding(
                    slice_id=row.id,
                    ordinal=1,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    model=model,
                    dim=len(vector),
                    # `vec` 是 LargeBinary：float32 紧凑打包（与 `semantic.pack` 同源），
                    # 直接塞 list 会报 "memoryview: a bytes-like object is required"
                    vec=semantic.pack(vector),
                    text_hash=hashlib.sha1(row.text.encode("utf-8")).hexdigest()[:32],
                )
            )
        db.commit()
        count += len(chunk)
        if on_progress is not None:
            on_progress(count, total)
    return count
