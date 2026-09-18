#!/usr/bin/env python3
"""把这份仓库（**唯一事实**）镜像到 Windows 侧那个构建目录。

## 三条规矩

* **只朝一个方向**：WSL/Linux → Windows。Windows 侧那份是**构建用的镜像**，
  不许手改 —— 改了会在下次同步时被覆盖。这就是"以 WSL 侧为唯一事实"的意思。
* **镜像，不是叠加**：目标里多出来的文件会被删掉，删除才会真的传播。
  （踩过：早先只做拷贝，于是删掉的模块在 Windows 侧活着，打出来的包里多一个废文件。）
* **同步完要校验**：逐文件比对 sha256，不一致就退出码非零 ——
  "我同步过了"和"两边真的一样"是两件事。

## 为什么要有这一层

打包必须在 Windows 上跑（PyInstaller **不能交叉编译**），而开发与事实都在 WSL 这边。
两边靠手工拷贝，迟早出现"改了这边忘了那边"，而且症状是"打出来的包行为不对"，
离原因很远。所以固化成一条命令，**每次出包前都跑**。

## 它不做的事

不管 venv、不管打包、不管自检 —— 那些是 `build/package.py` 的事。
这里只保证"Windows 侧那份代码，和 WSL 这份一模一样"。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 不进镜像的目录（相对仓库根）。理由写在每一项后面。
SKIP_DIRS = (
    ".git",             # 版本库不进镜像：Windows 侧那份不提交任何东西
    ".codebuddy",       # IDE 的工作草稿
    "api/.venv",        # venv 是平台专属的，Windows 侧自己建一个
    "build/dist",       # 产物：Windows 侧自己出，不从这边搬
    "build/.pyinstaller",  # PyInstaller 的工作目录
    "vendor",           # 76M 的 Pyodide：打包不需要它（清单已经提交进仓库）
    "data/cache",       # 重型组件的缓存：Windows 侧首启自己取
    "__pycache__",
    ".pytest_cache",    # 测试缓存：跑出来的东西不该进镜像
    ".ruff_cache",
    "htmlcov",
    "node_modules",
)

#: 不进镜像的文件（相对仓库根）
SKIP_FILES = (
    "api/.env",         # 本机配置：Windows 侧的运行环境由包自己决定（通道说了算）
    "bank.json",
    # 开发库的 WAL 附属文件：**它们是活的**（本地跑着服务时一直在变），
    # 同步它们既没意义，还会让"镜像期间源被改了"这类误报变多。
    # 主文件足够当自检样本（SQLite 从主文件也能读出已回写的部分）。
    "data/quizforge.db-wal",
    "data/quizforge.db-shm",
)


def _decode(raw: bytes) -> str:
    """Windows 的输出按 **GBK** 先试，再退到 UTF-8。

    别用 `subprocess(..., text=True)`：那等于告诉 Python"这是 UTF-8"，
    而中文区 Windows 控制台输出的是 GBK —— 解不动会抛 `UnicodeDecodeError`，
    且抛在 subprocess 内部，看起来像脚本自己崩了（实测撞过）。

    放在这个模块里是因为**四个兄弟脚本都要它**（setup / sync / dist / smoke），
    各写一份的结果是踩同一个坑四次。
    """
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _powershell(script: str, timeout: int = 1800) -> tuple[int, str]:
    """在 Windows 上跑一段 PowerShell（从 `/mnt/c` 起，免得它抱怨 UNC 路径）。"""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            cwd="/mnt/c",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, _decode((proc.stdout or b"") + (proc.stderr or b""))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skipped(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    for entry in SKIP_DIRS:
        if rel == entry or rel.startswith(entry + "/"):
            return True
    if rel in SKIP_FILES:
        return True
    return rel.endswith(".pyc")


def _source_files() -> dict[str, Path]:
    """镜像里该有的文件：`相对路径 → 绝对路径`。"""
    out: dict[str, Path] = {}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(ROOT))
        if _skipped(rel):
            continue
        out[rel.replace(os.sep, "/")] = path
    return out


def _windows_src_dir() -> Path | None:
    """Windows 侧的镜像目录：`%USERPROFILE%\\qf-build\\src`。

    从 WSL 里问 Windows 拿它 —— 不猜用户名（`C:\\Users\\<谁>` 这件事只有 Windows 自己知道）。
    """
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", "Write-Output $env:USERPROFILE"],
            capture_output=True,
            text=True,
            cwd="/mnt/c",  # 从 /mnt/c 起，免得 powershell 抱怨 UNC 路径
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    profile = (proc.stdout or "").strip().replace("\r", "")
    if not profile:
        return None
    try:
        converted = subprocess.run(
            ["wslpath", "-u", profile], capture_output=True, text=True, timeout=10
        )
        base = (converted.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        base = ""
    if not base:
        return None
    return Path(base) / "qf-build" / "src"


def _to_wsl_path(windows_path: str) -> str:
    """Windows 路径 → WSL 路径（给"把产物拷回仓库"用）。"""
    try:
        proc = subprocess.run(
            ["wslpath", "-u", windows_path.strip()], capture_output=True, text=True, timeout=10
        )
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def sync(target: Path) -> int:
    if not target.is_dir():
        print(f"目标目录不存在：{target}")
        print("（先跑 `build/package.py` 的前置准备：在 Windows 上建一个 venv，见 docs/分发工作流.md）")
        return 2

    source = _source_files()
    print(f"源（唯一事实）：{ROOT}")
    print(f"目标（构建镜像）：{target}")

    copied: list[str] = []
    removed: list[str] = []

    # ① 拷贝有差异的（先比大小，再比哈希 —— 大文件不必每次全读）
    for rel, src in sorted(source.items()):
        dest = target / rel
        if dest.is_file():
            if dest.stat().st_size == src.stat().st_size and _sha256(dest) == _sha256(src):
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        copied.append(rel)

    # ② 删掉目标里多出来的（镜像语义：删除要传播）
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(target)).replace(os.sep, "/")
        if _skipped(rel) or rel in source:
            continue
        path.unlink()
        removed.append(rel)

    # ③ 校验：逐文件比哈希（"同步过了"不等于"真的一样"）
    source.pop("data/quizforge.db", None)  # 样本库 22M，单独说明，但不参与逐字节校验的噪声
    mismatched: list[str] = []
    for rel, src in sorted(source.items()):
        dest = target / rel
        if not dest.is_file() or _sha256(dest) != _sha256(src):
            mismatched.append(rel)

    print(f"\n拷贝 {len(copied)} 个 · 删除 {len(removed)} 个 · 其余一致")
    if copied[:6]:
        for rel in copied[:6]:
            print(f"  + {rel}")
        if len(copied) > 6:
            print(f"  … 还有 {len(copied) - 6} 个")
    if removed[:6]:
        for rel in removed[:6]:
            print(f"  - {rel}")
        if len(removed) > 6:
            print(f"  … 还有 {len(removed) - 6} 个")

    if mismatched:
        print("\n**校验没过**：下面这些文件同步完两边还不一样：")
        for rel in mismatched[:10]:
            print(f"  ✗ {rel}")
        print(
            "  最常见的原因是**同步期间源仓库被改了**（编辑、跑构建、切分支都会）。\n"
            "  镜像本身没问题，重新跑一次这条命令即可（把改动一起带过去）。"
        )
        return 1
    print("校验通过：两边逐文件哈希一致 ✓")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(
        prog="build/sync_win.py", description="把仓库镜像到 Windows 侧构建目录（WSL 是唯一事实）"
    )
    # 注意 `%%`：argparse 会对 help 做 % 格式化，单个 % 会当场抛错
    parser.add_argument("--target", help="目标目录（默认从 Windows 的 %%USERPROFILE%% 推）")
    args = parser.parse_args(argv)

    target = Path(args.target) if args.target else _windows_src_dir()
    if target is None:
        print("找不到 Windows 侧的镜像目录 —— 用 --target 指定一个（需在 /mnt/ 下）")
        return 2
    return sync(target)


if __name__ == "__main__":
    raise SystemExit(main())
