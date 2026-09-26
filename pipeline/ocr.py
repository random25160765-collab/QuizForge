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

import os
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
_ENGINE_PROVIDERS: list[str] = []
#: `_patch_upstream_cuda_keys` 只该跑一次
_PATCHED = False


def _preload_cuda() -> None:
    """把 pip 装的那几个 CUDA 库喂给 ONNX Runtime。

    为什么必须显式做：`onnxruntime-gpu` 是**动态链** `libcudart.so.12` / `libcublas.so.12`
    / `libcudnn.so.9`，而 pip 的 `nvidia-*-cu12` 把它们放在 `site-packages/nvidia/*/lib`
    —— 那里不在动态库搜索路径上（`local_embed.cuda_lib_dirs()` 那段注释讲的是同一件事）。
    `make` / `ops` 起的子进程能走 CUDA，是因为父进程替它们设好了 `LD_LIBRARY_PATH`；
    **裸着跑的 python 不能**（`make images` 就是这样），而且症状不好认：
    不是"找不到库"，是会话建得起来、一推理就

        CudaKernel::RequireCudnnHandle → NOT_IMPLEMENTED

    ORT 1.19+ 的 `preload_dlls()` 就是干这件事的。老版本没有它、或本机没有 pip 的
    CUDA 库，都不是错误 —— 那就按 CPU 跑，慢一点，结果一样。
    """
    try:
        import onnxruntime as ort  # noqa: PLC0415

        ort.preload_dlls()
    except Exception:  # noqa: BLE001
        pass


def _cuda_kwargs() -> dict:
    """给 RapidOCR 的 CUDA 开关。**三件事都得对**，少一件就白给（都实测过）：

    * `*_use_cuda=True` —— 开关本身；
    * `*_model_path=""` —— 包的 `UpdateParameters` 把前缀后的键塞进各段配置时**同时读
      `model_path`**，不给就 `KeyError: 'model_path'`（给空串即可，它会补默认路径）；
    * 先 `preload_dlls()`（见 `_preload_cuda`），否则会话建得起来、一推理就死在 cudnn 句柄上。

    没有 CUDA provider 就退回 CPU；`QF_OCR_CUDA=0` 强制 CPU（做 A/B 用）。
    """
    forced = str(os.environ.get("QF_OCR_CUDA") or "").strip().lower()
    if forced in ("0", "false", "no", "off"):
        return {}
    try:
        import onnxruntime as ort  # noqa: PLC0415

        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return {}
    except Exception:  # noqa: BLE001
        return {}
    return {
        "det_use_cuda": True, "det_model_path": "",
        "rec_use_cuda": True, "rec_model_path": "",
        "cls_use_cuda": True, "cls_model_path": "",
    }


def _providers_of(engine) -> list[str]:  # noqa: ANN001
    """从引擎里掏出各子模块**真实**用的 provider。

    属性名不写在石头里（`text_detector` / `text_recognizer` 下面还有一层），所以递归找
    第一个带 `get_providers()` 的东西 —— 这样这份代码不会因为包内改名而静默报错。
    """
    found: list[str] = []
    seen: set[int] = set()

    def walk(obj, depth: int = 0) -> None:  # noqa: ANN001
        if depth > 5 or id(obj) in seen or not hasattr(obj, "__dict__"):
            return
        seen.add(id(obj))
        if hasattr(obj, "get_providers"):
            found.append(str(obj.get_providers()[0]))
            return
        for value in list(vars(obj).values())[:30]:
            if isinstance(value, (str, int, float, bool, bytes)) or value is None:
                continue
            walk(value, depth + 1)

    walk(engine)
    return sorted(set(found))


def _patch_upstream_cuda_keys() -> None:
    """给上游那两个方法补上 `use_cuda` 的前缀处理（**只在内存里换掉方法，不动它的文件**）。

    `rapidocr_onnxruntime.utils.UpdateParameters` 把 kwargs 按前缀分流到 Det / Cls / Rec
    三段，但清洗前缀时**只认** `rec_model_path` / `cls_model_path` / `cls_label_list`：
    `rec_use_cuda=True` 于是被原样塞进配置，成了一个没人读的怪键，真正的 `use_cuda`
    还是 false —— 症状是 **det 上了 GPU、rec 还在 CPU**（实测：providers 报
    `['CPUExecutionProvider', 'CUDAExecutionProvider']`，而不是三个都 CUDA）。
    Det 那段没这个问题（它对所有键都去前缀），所以只看 det 会以为"已经成功了"。
    """
    global _PATCHED
    if _PATCHED:
        return
    from rapidocr_onnxruntime import utils as upstream  # noqa: PLC0415

    original_cls = upstream.UpdateParameters.update_cls_params
    original_rec = upstream.UpdateParameters.update_rec_params

    def update_cls_params(self, config, cls_dict):  # noqa: ANN001
        if cls_dict and "cls_use_cuda" in cls_dict:
            cls_dict = dict(cls_dict)
            cls_dict["use_cuda"] = cls_dict.pop("cls_use_cuda")
        return original_cls(self, config, cls_dict)

    def update_rec_params(self, config, rec_dict):  # noqa: ANN001
        if rec_dict and "rec_use_cuda" in rec_dict:
            rec_dict = dict(rec_dict)
            rec_dict["use_cuda"] = rec_dict.pop("rec_use_cuda")
        return original_rec(self, config, rec_dict)

    upstream.UpdateParameters.update_cls_params = update_cls_params
    upstream.UpdateParameters.update_rec_params = update_rec_params
    _PATCHED = True


def engine():
    """RapidOCR 引擎**只建一次**：它要加载检测与识别两个模型，反复建会白等十几秒。

    **默认尽量走 GPU**：OCR 是本机唯一 CPU 密集的一步（4 路并发能吃掉 14 个核），
    实测同一张图 0.70 秒 → 0.37 秒，而且那 14 个核能放下来。
    """
    global _ENGINE, _ENGINE_PROVIDERS
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # 延迟导入：没装 OCR 的计划外的人不该被它拖累

        kwargs = _cuda_kwargs()
        if kwargs:
            _preload_cuda()
            _patch_upstream_cuda_keys()
        _ENGINE = RapidOCR(**kwargs)
        _ENGINE_PROVIDERS = _providers_of(_ENGINE)
    return _ENGINE


def providers() -> list[str]:
    """现在这套 OCR 跑在哪个 provider 上（会顺手把引擎建起来）。给人一个交代。"""
    if _ENGINE is None:
        engine()
    return list(_ENGINE_PROVIDERS)


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
