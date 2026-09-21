#!/usr/bin/env python3
"""生成 `build/embed-manifest.json` —— 本地小模型的**下载清单**。

## 为什么清单要进版本库

与 Pyodide 清单同一个理由（`heavy_deps.MANIFEST_FILE` 那段注释）：
它让"下载到的东西对不对"这件事**离线可判断** —— 校验不该依赖"再去网上取一份
期望值"，那等于没校验。

## 清单里钉住什么

* **wheels**：`onnxruntime` / `numpy` / `protobuf` / `packaging` / `flatbuffers` /
  `tokenizers`。地址由 pip 自己解析（`--report` 给出**真实**下载地址），所以
  "从哪个镜像取"这件事跟着 pip 的配置走，不在这里写死。
* **模型**：`Xenova/multilingual-e5-small` 的 int8 ONNX + 分词器，从 hf-mirror 取
  （huggingface.co 在目标机器上不一定通，而镜像通）。
* **平台与 Python 次版本**：wheel 是编译产物，文件名就把这两个钉死了。
  所以要为**真正会跑的那几个组合**各生成一份：Linux 开发机（3.14）与
  Windows 分发包（`build/setup_win.py` 装的是 3.12）。

## 跑法

    python3 build/embed_manifest.py              # 生成清单（会联网）
    python3 build/embed_manifest.py --vendor     # 顺手拷一份进 vendor/embed/（离线开发用）
    python3 build/embed_manifest.py --only-model # 只更新模型那几个文件

`--vendor` 那一份会有一百多 MB，进仓库是为了"离线也能起"（与 `vendor/pyodide`
同一条取舍）—— 要不要提交由你定，不提交也能跑（那就走下载）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_FILE = ROOT / "build" / "embed-manifest.json"
VENDOR_DIR = ROOT / "vendor" / "embed"

#: 六个 wheel。前五个是 onnxruntime 真正要的（numpy 是硬依赖，另外三个很小）；
#: `tokenizers` 单独取 —— **带 `--no-deps`**：它的依赖里有一串给
#: `from_pretrained` 用的东西（huggingface_hub / hf_xet / tqdm…），
#: 而我们只用 `Tokenizer.from_file`，实测解包后能独立 import。
WHEELS = ("onnxruntime", "numpy", "protobuf", "packaging", "flatbuffers", "tokenizers")

#: 真正会跑的那两个组合。wheel 文件名把平台与 Python 次版本都编进去了，
#: 所以清单必须按组合分开 —— 拿 Linux 的 wheel 去 Windows 上是装不起来的。
TARGETS = (
    {
        "platform": "linux-x86_64",
        "python": "3.14",
        "pip_args": [],  # 本机就是它，pip 自己解析
    },
    {
        "platform": "windows-amd64",
        "python": "3.12",
        "pip_args": ["--platform", "win_amd64", "--python-version", "3.12"],
    },
)

#: 模型。本地名字 → 仓库里的路径。
#:
#: **为什么是 bge-m3 而不是更小的**（2026-09-21 实测，同一份考卷）：
#:   * 跨语言判别力：短文本考卷 top1 **8/8**（vs e5-small 6/8）；
#:     区分度 **0.075**（vs 0.018，四倍）；分数带 0.53~0.79 能分开（vs 0.71~0.82 全挤一起）。
#:   * 真实语料上这是决定性的：e5-small 即使配了窗口化，中文问句**仍然返回「前言」**
#:     （0.854 对 0.773），bge-m3 同一窗口就选对了（0.642 对 0.615）。
#:   * 代价是 542MB（vs 112.8MB）—— 用户已确认可以接受（首启下载，不进包）。
#:
#: **它的 8192 token 卖点在 CPU 上不实用**：注意力是 O(seq²)，
#: `max_length=8192` 配 padding 一次要申请 22GB（实测直接 OOM）。
#: 实际能用的是 1024 —— 这也是"必须窗口化"的另一半理由（见 pipeline/embed.py）。
MODEL_REPO = "Xenova/bge-m3"
MODEL_BASE = f"https://hf-mirror.com/{MODEL_REPO}/resolve/main/"
MODEL_ITEMS = {
    "model.onnx": "onnx/model_quantized.onnx",  # int8 动态量化，542MB
    "tokenizer.json": "tokenizer.json",
}
MODEL_DIM = 1024  # bge-m3


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_wheel(filename: str) -> tuple[str, str]:
    """`onnxruntime-1.30.0-cp314-…whl` → `("onnxruntime", "1.30.0")`。

    按 `-` 切开取前两段。我们用的这几个包都没有 build 标记
    （形如 `pkg-1.0-1-py3-none-any.whl`）—— 真有了也只是查不到地址，
    会走下面那条"先别提交"的告警，不会静默写错。
    """
    parts = filename[: -len(".whl")].split("-")
    return (parts[0], parts[1]) if len(parts) >= 5 else ("", "")


def _wheel_url(name: str, version: str, filename: str) -> str:
    """回查这个文件名的真实下载地址。

    **为什么不在 pip 上直接拿**：`pip download` 没有 `--report`（那是
    `pip install` 的），而 `-v` 只打印文件名、不打印地址。按**文件名**去 PyPI
    的 JSON 里匹配最稳 —— 同一个版本有几十个平台各一份，文件名是唯一的。
    """
    if not name or not version:
        return ""
    try:
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 —— 查不到就是查不到，不中断整轮
        return ""
    for item in data.get("urls") or []:
        if item.get("filename") == filename:
            return str(item.get("url") or "")
    return ""


def _wheels_for(target: dict) -> dict:
    """一个平台组合的 wheels：名字 → {sha256, url, bytes, platform, python, kind}。"""
    with tempfile.TemporaryDirectory(prefix="qf-embed-") as work:
        workdir = Path(work)
        command = [
            sys.executable, "-m", "pip", "download", *WHEELS,
            "-d", str(workdir), "--only-binary", ":all:", "--no-deps",
            *target["pip_args"],
        ]
        print("  $", " ".join(command[:6]) + " …")
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            print(done.stderr[-1500:], file=sys.stderr)
            raise SystemExit(f"取 {target['platform']} / {target['python']} 的 wheel 失败")

        out: dict[str, dict] = {}
        for path in sorted(workdir.glob("*.whl")):
            name, version = _split_wheel(path.name)
            url = _wheel_url(name, version, path.name)
            out[path.name] = {
                "sha256": sha256_of(path),
                "url": url,
                "bytes": path.stat().st_size,
                "platform": target["platform"],
                "python": target["python"],
                "kind": "wheel",
            }
            flag = "" if url else "   ⚠ 没有下载地址 —— 先别提交"
            print(f"    {path.name}  {path.stat().st_size / 1048576:.1f} MB{flag}")
        return out


def _model_files(*, vendor: bool, reuse: Path | None = None) -> dict:
    """模型与分词器：名字 → {sha256, url, bytes, platform: any, python: any}。

    `reuse` 指向一个已经有这些文件的目录就直接拿它算哈希，**不再下载** ——
    542MB 的下载很容易被连接重置（实测撞到过一次），重跑时不该重来一遍。

    下载到临时目录、算完哈希就删 —— 想留一份到 `vendor/` 传 `vendor=True`，
    想直接试跑用 `python -m pipeline.embed --fetch`（它会走
    `local_embed.ensure()`，落到真正的缓存里）。
    """
    out: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="qf-embed-model-") as work:
        workdir = Path(work)
        for name, remote in MODEL_ITEMS.items():
            url = MODEL_BASE + remote
            destination = workdir / name
            local = (reuse / name) if reuse else None
            if local is not None and local.is_file():
                print(f"    复用本机的 {local}")
                shutil.copy2(local, destination)
            else:
                print(f"    下载 {remote} …")
                # 断了就再来一次：542MB 一次性走完的概率没那么高
                for attempt in range(1, 4):
                    try:
                        with urllib.request.urlopen(url, timeout=600) as response:  # noqa: S310
                            with destination.open("wb") as handle:
                                shutil.copyfileobj(response, handle)
                        break
                    except Exception as exc:  # noqa: BLE001 —— 网络层的错五花八门
                        if attempt == 3:
                            raise SystemExit(
                                f"取模型文件失败（{remote}，试了 3 次）：{exc}\n"
                                f"它很大，断了很常见。可以手工下到某个目录，"
                                f"再用 --model-dir 指过去。"
                            ) from exc
                        print(f"      第 {attempt} 次断了（{exc}），重试 …")
            out[name] = {
                "sha256": sha256_of(destination),
                "url": url,
                "bytes": destination.stat().st_size,
                "platform": "any",
                "python": "any",
                "kind": "model",
            }
            print(f"      {name}  {destination.stat().st_size / 1048576:.1f} MB")
        if vendor:
            (VENDOR_DIR / "files").mkdir(parents=True, exist_ok=True)
            for name in out:
                shutil.copy2(workdir / name, VENDOR_DIR / "files" / name)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成本地小模型的下载清单")
    parser.add_argument("--vendor", action="store_true",
                        help="顺手把 wheel 与模型拷进 vendor/embed/（离线开发用，一百多 MB）")
    parser.add_argument("--only-model", action="store_true", help="只更新模型那几个文件")
    parser.add_argument("--model-dir", type=Path, default=None,
                        help="模型文件已经在本机某个目录里（省掉那 542MB 的下载）")
    args = parser.parse_args(argv)

    previous: dict = {}
    if MANIFEST_FILE.is_file():
        try:
            previous = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except ValueError:
            previous = {}

    files: dict[str, dict] = {}
    if args.only_model:
        files.update((previous.get("files") or {}))
        files = {k: v for k, v in files.items() if (v or {}).get("kind") != "model"}
    else:
        for target in TARGETS:
            print(f"取 {target['platform']} / Python {target['python']} 的 wheels：")
            files.update(_wheels_for(target))

    print("取模型与分词器：")
    files.update(_model_files(vendor=args.vendor, reuse=args.model_dir))

    total = sum(int((m or {}).get("bytes") or 0) for m in files.values())
    payload = {
        "version": "2026-09-21",
        "model": f"{MODEL_REPO}（int8 ONNX）",
        "dim": MODEL_DIM,
        "onnx": "model.onnx",
        "tokenizer": "tokenizer.json",
        "bytes": total,
        "note": "本地向量小模型清单：首启下载后逐个校验（build/embed_manifest.py 生成）",
        "files": files,
    }
    MANIFEST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
    print(f"\n清单已写出：{MANIFEST_FILE}")
    print(f"  {len(files)} 个文件 · {total / 1048576:.1f} MB")
    for name, meta in sorted(files.items()):
        print(f"    {name}  [{(meta or {}).get('platform')}/{((meta or {}).get('python'))}]")

    if args.vendor:
        print(f"  vendor 副本：{VENDOR_DIR}（要不要提交由你定）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
