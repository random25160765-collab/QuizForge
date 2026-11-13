"""扫描件的 OCR 支路：没有文字层的 PDF → 可检索的正文。

**什么时候走**：`materials.text_lines` 抽出正文之后，发现“每页字数”低得不像话
（扫描件的封面往往有两三行真文字，光看“抽到了没有”会漏掉整本书），或者干脆是空串。

**怎么走**：`pdftoppm` 渲页（poppler 本机就有，不引新依赖）→ RapidOCR 逐页识别 →
按**纵坐标排序**拼行（识别顺序不等于阅读顺序）→ 每页一个 `## 第 N 页`。
只到“页”这一级是**故意**的：扫描件认不出章节标题，硬造会把切片切歪。

**为什么 api 里另写一份**：`pipeline/ocr.py` 是出题机那条链的，打包时不会跟着走。
这里只留入库要用的那一套（渲页 + 识别 + 分页），CUDA 的开关与上游补丁照抄它 ——
本机实测同一页 CPU 2.4 秒、GPU 0.77 秒（RTX 5060），527 页是 20 分钟和 7 分钟的区别。
`QF_OCR_CUDA=0` 可以强制 CPU（做 A/B 用）。

**分块渲页**：一次渲 20 页、认完就删，不把整本书的 PNG 摊在 /tmp 里
（200dpi 一页约 200KB，527 页就是一百多兆，加上并发写盘并不划算）。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

#: 渲页分辨率。200 dpi 是 OCR 的常用档：再高收益很小、耗时就涨。
RENDER_DPI = 200
#: 一批渲多少页。只影响临时文件占多少盘，不影响识别质量。
BATCH_PAGES = 20
#: 单页识别 / 单批渲页的上限（秒）。给足：慢总比悄悄截断好。
PAGE_TIMEOUT = 120

_ENGINE = None
#: 引擎里三个子模型**实际**用的 provider（建引擎时填；`describe()` 给人看）。
_PROVIDERS: list[str] = []
#: `_patch_upstream_cuda_keys` 只该跑一次
_PATCHED = False
#: 这次进程里 CUDA 已经翻过车 —— 之后一律 CPU（别每页都试一次 GPU 再失败）。
_FORCE_CPU = False


def _preload_cuda() -> None:
    """把 pip 装的那几个 CUDA 库喂给 ONNX Runtime。

    为什么必须显式做：`onnxruntime-gpu` 是**动态链** `libcudart.so.12` 之类，而
    pip 的 `nvidia-*-cu12` 把它们放在 `site-packages/nvidia/*/lib` —— 那里不在
    动态库搜索路径上。不 preload 的症状不是"找不到库"，是会话建得起来、一推理就死。
    ORT 1.19+ 的 `preload_dlls()` 就是干这件事的；老版本没有它也不算错误（退回 CPU）。
    """
    try:
        import onnxruntime as ort  # noqa: PLC0415

        ort.preload_dlls()
    except Exception as exc:  # noqa: BLE001 - 没有 CUDA 就按 CPU 跑，结果一样只是慢
        # **不能一声不吭**：这一步没成功的话会话照样建得起来，要到第一次推理才炸
        # （`RequireCudnnHandle` → `dlopen failed for libcudnn.so`），
        # 而那时报的是"ONNXRuntime inferece failed."，看不出是库没挂上。
        logging.getLogger("quizforge").warning("CUDA 库预加载失败（只能按 CPU 跑）：%s", exc)


def _cuda_kwargs() -> dict:
    """给 RapidOCR 的 CUDA 开关。**三件事都得对**，少一件就白给（都实测过）：

    * `*_use_cuda=True` —— 开关本身；
    * `*_model_path=""` —— 包的 `UpdateParameters` 把前缀后的键塞进各段配置时
      同时读 `model_path`，不给就 `KeyError: 'model_path'`（给空串即可）；
    * 先 `preload_dlls()`，否则会话建得起来、一推理就死在 cudnn 句柄上。

    没有 CUDA provider 就退回 CPU；`QF_OCR_CUDA=0` 强制 CPU（做 A/B 用）。
    """
    if _FORCE_CPU:
        return {}
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


def _patch_upstream_cuda_keys() -> None:
    """给上游那两个方法补上 `use_cuda` 的前缀处理（**只在内存里换掉方法，不动它的文件**）。

    `rapidocr_onnxruntime.utils.UpdateParameters` 把 kwargs 按前缀分流到 Det / Cls / Rec
    三段，但清洗前缀时**只认** `rec_model_path` / `cls_model_path` / `cls_label_list`：
    `rec_use_cuda=True` 于是被原样塞进配置，成了一个没人读的怪键，真正的 `use_cuda`
    还是 false —— 症状是 **det 上了 GPU、rec 还在 CPU**（实测过：providers 只报
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


def _providers_of(engine) -> list[str]:  # noqa: ANN001
    """从引擎里掏出各子模块**真实**用的 provider（属性名不写在石头里，递归找）。"""
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


def available() -> tuple[bool, str]:
    """这套 OCR 能不能用。不能用时**说清缺什么** —— 报错要能直接给用户看。"""
    if shutil.which("pdftoppm") is None:
        return False, "没有 pdftoppm（poppler 渲不了页）"
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return False, "没装 rapidocr_onnxruntime（pip install rapidocr_onnxruntime）"
    return True, ""


def _engine():  # noqa: ANN202
    """RapidOCR 引擎**只建一次**：它要加载检测与识别两个模型，反复建会白等十几秒。

    **默认尽量走 GPU**：OCR 是本机唯一 CPU 密集的一步，一本书 527 页在 CPU 上
    要二十分钟、GPU 七分钟（实测 2.4 秒/页 vs 0.77 秒/页）。跑在哪由
    `_cuda_kwargs()` 决定，`QF_OCR_CUDA=0` 可强制回落。
    """
    global _ENGINE, _PROVIDERS
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415 - 没装 OCR 的人不该被拖累

        kwargs = _cuda_kwargs()
        if kwargs:
            _preload_cuda()
            _patch_upstream_cuda_keys()
        _ENGINE = RapidOCR(**kwargs)
        _PROVIDERS = _providers_of(_ENGINE)
        logging.getLogger("quizforge").info(
            "OCR 引擎就绪：%s（%s）",
            " / ".join(_PROVIDERS) or "未知 provider",
            describe()["note"],
        )
    return _ENGINE


def pages_of(pdf: Path) -> int:
    """用 `pdfinfo` 问页数（问不到返回 0 = 不判，按流式渲页处理）。"""
    found = shutil.which("pdfinfo")
    if found is None:
        return 0
    try:
        done = subprocess.run([found, str(pdf)], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0
    hit = re.search(r"^Pages:\s+(\d+)", done.stdout.decode("utf-8", "replace"), re.MULTILINE)
    return int(hit.group(1)) if hit else 0


def _page_number(path: Path) -> int:
    hit = re.search(r"-(\d+)\.png$", path.name)
    return int(hit.group(1)) if hit else 0


def _render(pdf: Path, out_dir: Path, first: int, last: int) -> list[Path]:
    """渲一段页成 PNG（先清掉上一批），返回按页序排好的清单。"""
    for old in out_dir.glob("page-*.png"):
        old.unlink(missing_ok=True)
    count = max(1, last - first + 1)
    try:
        done = subprocess.run(
            [
                "pdftoppm", "-r", str(RENDER_DPI), "-png",
                "-f", str(first), "-l", str(last),
                str(pdf), str(out_dir / "page"),
            ],
            capture_output=True,
            timeout=max(PAGE_TIMEOUT, 8 * count),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return sorted(out_dir.glob("page-*.png"), key=_page_number)


def _noise(line: str, index: int, total: int) -> bool:
    """页眉页脚那种“纯页码 / 书名 + 页码”的行：进正文只会污染切片。"""
    text = line.strip()
    if not text:
        return True
    if not (index < 2 or index >= total - 2):   # 只在一页的头两行与末两行里判
        return False
    if re.fullmatch(r"[-—–\s]*\d{1,4}[-—–\s]*", text):
        return True
    return bool(re.fullmatch(r"[^\d]{0,24}\s\d{1,4}\s*", text))


def _reset_engine() -> None:
    """把引擎丢掉，下次重建（换 provider 用）。"""
    global _ENGINE, _PROVIDERS
    _ENGINE = None
    _PROVIDERS = []


def _retry_on_cpu(png: Path, exc: Exception) -> list[str]:
    """CUDA 这一轮没跑起来（显存被抢、驱动抽风）就退回 CPU 重试这一页。

    为什么不让它把整条任务弄红：OCR 只是入库里的一步，而"抢不到显存"是**环境**
    问题 —— CPU 慢三倍但结果一样，本地单用户，二十分钟也是能接受的代价。
    只翻一次车：`_FORCE_CPU` 之后整个进程都不再试 GPU（否则每页都要先失败一次）。
    """
    global _FORCE_CPU
    if "CUDAExecutionProvider" not in _PROVIDERS:
        raise exc
    logging.getLogger("quizforge").warning(
        "OCR 的 CUDA 推理失败，改用 CPU 继续（%s）", exc.__cause__ or exc
    )
    _FORCE_CPU = True
    _reset_engine()
    return _lines_of(png)


def _lines_of(png: Path) -> list[str]:
    """一页 → 若干行（按纵坐标、再按横坐标排）。"""
    got = _engine()(str(png))
    boxes = got[0] if isinstance(got, tuple) else got
    rows: list[tuple[float, float, str]] = []
    for one in boxes or []:
        try:
            box, text = one[0], str(one[1])
            ys = [float(point[1]) for point in box]
            xs = [float(point[0]) for point in box]
        except (IndexError, TypeError, ValueError):
            continue
        if text.strip():
            rows.append((min(ys), min(xs), text.strip()))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [text for index, (_, _, text) in enumerate(rows) if not _noise(text, index, len(rows))]


def convert(
    pdf: Path,
    *,
    first: int = 1,
    last: int = 0,
    on_page: Callable[[int, int], None] | None = None,
) -> dict:
    """一份扫描件 → `{ok, md, pages, chars, why}`。

    `on_page(已认页数, 总页数)` 每页回调一次：几百页要跑十几分钟，页面上
    只有一个“解析”和卡死没有区别。总页数问不到时给 0（前端只显示已认页数）。
    """
    ready, why = available()
    if not ready:
        return {"ok": False, "why": why}

    total = last or pages_of(pdf) or 0
    blocks: list[str] = []
    chars = 0
    done_pages = 0
    with tempfile.TemporaryDirectory(prefix="qf-ocr-") as tmp:
        out_dir = Path(tmp)
        start = first
        while True:
            stop = min(total, start + BATCH_PAGES - 1) if total else start + BATCH_PAGES - 1
            rendered = _render(pdf, out_dir, start, stop)
            if not rendered:
                break
            for png in rendered:
                try:
                    lines = _lines_of(png)
                except Exception as exc:  # noqa: BLE001 - CUDA 环境问题，退回 CPU 再试
                    lines = _retry_on_cpu(png, exc)
                body = "\n".join(lines).strip()
                chars += len(body)
                number = _page_number(png) or (start + done_pages)
                # 页号用**渲染出来的**那个：抽页 / 分段渲染时它未必从 1 开始，
                # 坐标（引用回去的页码）才不会骗人。
                blocks.append(f"## 第 {number} 页\n\n{body}" if body else f"## 第 {number} 页")
                done_pages += 1
                if on_page is not None:
                    on_page(done_pages, total)
            if total and stop >= total:
                break
            start = stop + 1

    if not done_pages:
        return {"ok": False, "why": "渲页失败（pdftoppm 没吐东西）", "pages": 0}
    if chars < 200:
        return {
            "ok": False,
            "why": f"只认出 {chars} 字（可能不是文字页）",
            "pages": done_pages,
            "chars": chars,
        }
    md = f"# {pdf.stem}\n\n" + "\n\n".join(blocks) + "\n"
    return {"ok": True, "md": md, "pages": done_pages, "chars": chars}


def describe() -> dict:
    """给人看的现状（排障用）：能不能用、缺什么、按什么参数跑、跑在哪。

    **不建引擎**：这里只报已知的事实，引擎是否上了 GPU 要看 `_PROVIDERS`
    （建引擎会加载两个模型，排障时不该顺手等十几秒）。
    """
    ready, why = available()
    if not ready:
        note = why
    elif _PROVIDERS:
        note = "已加载：" + " / ".join(_PROVIDERS)
    else:
        note = "按需加载（有 CUDA provider 就走 GPU）"
    return {
        "ready": ready,
        "why": why,
        "note": note,
        "providers": list(_PROVIDERS),
        "dpi": RENDER_DPI,
        "batch": BATCH_PAGES,
    }
