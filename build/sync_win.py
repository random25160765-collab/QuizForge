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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 不进镜像的目录（相对仓库根）。理由写在每一项后面。
SKIP_DIRS = (
    ".git",             # 版本库不进镜像：Windows 侧那份不提交任何东西
    ".git-rewrite",     # 历史重写留下的整份副本（几千个文件，且不是当前代码）
    ".codebuddy",       # CodeBuddy 的开发 skill：打包版用不到（出题机那几份契约在 pipeline/ 下，会跟着走）
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
    "api/.env",         # 本机配置：Windows 侧的运行环境由包自己决定
    # 本机配置与密钥**一律不进镜像**：`settings.local.json` 里有 Turso token 与
    # 明文 API 密钥，`snapshot.key` 能解开对话密文，`db/*.enc` 是对话全量快照。
    # 构建镜像只该有代码与样例（`settings.example.json`）。
    "config/settings.local.json",
    "config/ai.local.json",
    "config/embed.local.json",
    "config/snapshot.key",
    "db/quizforge.db.gz.enc",
    "bank.json",        # `make bank-export` 的产物（按需生成），不该进构建镜像
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


#: 镜像根下这份清单记着"上次同步时每个文件的 sha256"。有了它，目标端不必再读文件
#: 就能判断"要不要拷"（目标在 `/mnt/c`，每次读都跨 VM，这是慢的根源）。
MANIFEST_NAME = ".qf-sync-manifest.json"


def _load_manifest(target: Path) -> dict[str, str]:
    try:
        data = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_manifest(target: Path, payload: dict[str, str]) -> None:
    try:
        (target / MANIFEST_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


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
    """Windows 侧的镜像目录：`%USERPROFILE%\\Desktop\\QF_dist\\src`。

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
    return Path(base) / "Desktop" / "QF_dist" / "src"


def _to_windows_path(wsl_path: str) -> str:
    """WSL 路径 → Windows 路径（`wslpath -w`）。拿不到就返回空串。

    robocopy 要的是 Windows 视角的源路径（`\\\\wsl.localhost\\Ubuntu\\home\\...`），
    不能把 `/home/...` 直接喂给它。
    """
    try:
        proc = subprocess.run(
            ["wslpath", "-w", wsl_path], capture_output=True, text=True, timeout=10
        )
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


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
    """把仓库镜像到 Windows 的构建目录，用 **robocopy /MIR**。

    ## 为什么不是 Python 逐文件拷

    实测：逐文件 `read_bytes()` + `write_bytes()` 同步一次要 **3 分钟**，而
    `time` 显示 CPU 只用了 3.6s —— 时间全花在跨 VM 的每文件 IO 上（`/mnt/c` 是 9p）。
    改成 robocopy：Windows 原生、多线程、/MIR 只看大小+时间戳（不读内容），
    增量通常 **1-3 秒**。

    ## 排除项有两类

    * **不该进镜像**的：版本库、历史重写副本、vendor（76M）、venv、node_modules、
      测试缓存、构建产物；
    * **密钥与本机配置**：`settings.local.json`（Turso token + 明文密钥）、
      `snapshot.key`（能解开对话密文）、`db/*.enc`（对话全量）。这些一旦进了
      Windows 侧的镜像目录，就等于"构建机上也有一份"。
    """
    if not target.is_dir():
        print(f"目标目录不存在：{target}")
        print("（先跑 `build/package.py` 的前置准备：在 Windows 上建一个 venv，见 docs/分发工作流.md）")
        return 2

    # **逐个目录同步**，不做"整仓一次 /MIR + 排除"。
    #
    # 为什么改：整仓那次 `/MIR` 会莫名其妙跳过某些子树（实测 `api/web` 882 个文件
    # 一个都没拷、`desktop` 也没拷），表现就是"代码改了、包里没变"，而且不报错。
    # 白名单逐目录跑，每个 0.1-1 秒，总共几秒，但**结果可预测**。
    targets = ["api", "theme", "build", "tools", "pipeline", "desktop", "config"]
    for name in targets:
        src = ROOT / name
        if not src.is_dir():
            continue
        win_src = _to_windows_path(str(src))
        win_dst = _to_windows_path(str(target / name))
        if not win_src or not win_dst:
            print(f"拿不到 {name} 的 Windows 路径（wslpath 不可用）")
            return 2
        cmd = [f"robocopy", f'"{win_src}"', f'"{win_dst}"', "/MIR", "/R:1", "/W:1",
               "/NFL", "/NDL", "/NP", "/NJH", "/NJS",
               "/XD", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
               ".venv", "dist", ".pyinstaller", "work",
               "/XF", ".env", "settings.local.json", "ai.local.json", "embed.local.json",
               "snapshot.key", "quizforge.db.gz.enc"]
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", " ".join(cmd)],
                capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{name}: robocopy 起不来（{exc}）")
            return 2
        if proc.returncode >= 8:
            print(f"{name}: robocopy 失败（退出码 {proc.returncode}）")
            err = (proc.stderr or "").strip()
            if err:
                print(err[:800])
            return 1

    print(f"镜像已同步：{', '.join(targets)}")

    # 样本库单独拷（`data/` 整体被排除，但打包自检要读题库）
    sample = ROOT / "data" / "quizforge.db"
    if sample.is_file():
        (target / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample, target / "data" / "quizforge.db")

    # **`api/web` 必须再单独同步一次**：整仓那次 `/MIR` 会把它整个跳过（实测 882 个文件
    # 一个都没拷，镜像里还是几周前的旧前端），而它正是打进包里的界面 —— 跳过它等于
    # "改了前端但包里没变"。单独跑只花 0.5 秒。
    web_src = _to_windows_path(str(ROOT / "api" / "web"))
    web_dst = _to_windows_path(str(target / "api" / "web"))
    if web_src and web_dst:
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f'robocopy "{web_src}" "{web_dst}" /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /NJS',
            ],
            capture_output=True, text=True, timeout=120,
        )
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
