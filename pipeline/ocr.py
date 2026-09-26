"""OCR 支路：给"没有文字层"的 PDF 补一份可检索的正文（`normalize.py` 的第二条兜底）。

**什么时候走它**：`judge_text` 把抽取结果判成 `garbled` / `none` 的时候。实测那两本正是
这一类（算法导论 142M、人月神话 31M，前 20 页 0 字）—— 抽出空串不代表文件坏了，是因为
整页都是图。

**怎么走**：

1. `pdftoppm -r 200 -png` 把页渲成图（poppler 本机就有，不引新依赖）；
2. RapidOCR（ONNX）逐页识别，给的是行框 + 文本 + 置信度；
3. 按**纵坐标排序**拼行（识别顺序不等于阅读顺序），丢掉页眉页脚那种"纯页码"行；
4. 每页落一个 `## 第 N 页`，于是切片粒度到"页" —— 这是诚实的：扫描件的**章节标题**
   认不出来，硬造会把切片切歪。真要更好，得加版面分析（那是另一摊）。

选型是用户拍的：**先 RapidOCR（ONNX，装起来快、依赖少）**，够用再考虑换 PaddleOCR。
本机是 ONNX 的 CPU 版（`onnxruntime`，CPUExecutionProvider）—— 一台 RTX 5060 在机器上，
换 `onnxruntime-gpu` 之后这里不用改代码，它自己会挑。

**产物形状与别的支路一致**（`{citekey}.md` + 旁注），旁注多一个 `"ocr": true` 标明来路 ——
"这段话是 OCR 出来的"与"是原生文字层"在下游不是一个可信度，不能混。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

#: 渲页的分辨率。200 dpi 是 OCR 的常用档：再高收益很小、耗时就涨。
RENDER_DPI = 200
#: 一页渲染 + 识别的上限（秒）。
PAGE_TIMEOUT = 120

_ENGINE = None


def engine():
    """RapidOCR 引擎**只建一次**：它要加载检测与识别两个模型，反复建会白等十几秒。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # 延迟导入：没装 OCR 的计划外的人不该被它拖累

        _ENGINE = RapidOCR()
    return _ENGINE


def _has_pdftoppm() -> bool:
    return shutil.which("pdftoppm") is not None


def render_pages(pdf: Path, out_dir: Path, *, first: int = 1, last: int = 0) -> list[Path]:
    """渲页成 PNG，返回按页序排好的文件清单。`last=0` 表示渲到最后一页。"""
    prefix = out_dir / "page"
    command = ["pdftoppm", "-r", str(RENDER_DPI), "-png", "-f", str(first)]
    if last:
        command += ["-l", str(last)]
    command += [str(pdf), str(prefix)]
    done = subprocess.run(command, capture_output=True, timeout=PAGE_TIMEOUT * 4, check=False)
    if done.returncode != 0:
        return []

    def page_number(path: Path) -> int:
        hit = re.search(r"-(\d+)\.png$", path.name)
        return int(hit.group(1)) if hit else 0

    return sorted(out_dir.glob("page-*.png"), key=page_number)


def lines_of(png: Path) -> tuple[list[str], float]:
    """一页 → 若干行（按纵坐标排）。返回 (行, 平均置信度)。"""
    got = engine()(str(png))
    boxes = got[0] if isinstance(got, tuple) else got
    if not boxes:
        return [], 0.0
    rows: list[tuple[float, float, str, float]] = []
    for one in boxes:
        try:
            box, text = one[0], str(one[1])
            score = float(one[2]) if len(one) > 2 else 0.0
        except (IndexError, TypeError, ValueError):
            continue
        ys = [float(point[1]) for point in box]
        xs = [float(point[0]) for point in box]
        rows.append((min(ys), min(xs), text.strip(), score))
    rows.sort(key=lambda one: (one[0], one[1]))
    kept = [one for one in rows if one[2]]
    average = sum(one[3] for one in kept) / len(kept) if kept else 0.0
    return [one[2] for one in kept], average


def is_page_noise(line: str, index: int, total: int) -> bool:
    """页眉页脚那种"纯页码/书名 + 页码"的行：它进正文只会污染切片。"""
    text = line.strip()
    if not text:
        return True
    edge = index < 2 or index >= total - 2  # 只在一页的头两行与末两行里判
    if not edge:
        return False
    if re.fullmatch(r"[-—–\s]*\d{1,4}[-—–\s]*", text):
        return True
    return bool(re.fullmatch(r"[^\d]{0,24}\s\d{1,4}\s*", text))


def convert(pdf: Path, *, max_pages: int = 0, sample_every: int = 1) -> dict:
    """一份扫描件 → `{ok, md, pages, chars, confidence, why}`。

    `max_pages` > 0 时只做前若干页（试跑用）；`sample_every` > 1 时按间隔抽页
    （想知道"整本 OCR 要多久"时用，别真的跑整本）。
    """
    if not _has_pdftoppm():
        return {"ok": False, "why": "没有 pdftoppm（poppler），渲不了页"}
    with tempfile.TemporaryDirectory(prefix="qf-ocr-") as tmp:
        out_dir = Path(tmp)
        last = max_pages * sample_every if max_pages else 0
        pages = render_pages(pdf, out_dir, first=1, last=last)
        if not pages:
            return {"ok": False, "why": "渲页失败（pdftoppm 没吐东西）"}
        if sample_every > 1:
            pages = pages[::sample_every]

        blocks: list[str] = []
        scores: list[float] = []
        total_chars = 0
        for order, png in enumerate(pages, start=1):
            lines, average = lines_of(png)
            scores.append(average)
            kept = [one for index, one in enumerate(lines) if not is_page_noise(one, index, len(lines))]
            body = "\n".join(kept).strip()
            total_chars += len(body)
            # 页码用**渲染出来的页号**（`-f/-l/抽页` 时它未必从 1 开始），坐标才不会骗人
            number = int(re.search(r"-(\d+)\.png$", png.name).group(1))
            blocks.append(f"## 第 {number} 页\n\n{body}" if body else f"## 第 {number} 页")
        if total_chars < 200:
            return {"ok": False, "why": f"OCR 只认出 {total_chars} 字（这页可能不是文字）", "pages": len(pages)}
        title = pdf.stem
        md = f"# {title}\n\n" + "\n\n".join(blocks) + "\n"
        return {
            "ok": True,
            "md": md,
            "pages": len(pages),
            "chars": total_chars,
            "confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        }
