"""这台机器的设置文件：**一份可拷贝的 JSON**（类比 VS Code 的 `settings.json`）。

为什么要有它（2026-11-07）：

* 设置原先只活在数据库那一行 + 浏览器 localStorage 里。于是"我用 CLI/脚本改了设置"
  在界面上看不见 —— 前端一同步就把空值推回去，覆盖掉。用户的原话：
  「cli 界面填入或者通过其他方式加载参数之后，图形界面里要有东西」。
* 用户还要一份**能直接复制给新用户**的格式。

所以改成：**文件是权威**，数据库只是缓存/镜像。

* 位置：`QF_SETTINGS_FILE` 环境变量，默认 `config/settings.local.json`
* 读：`load()`（按 mtime 缓存 1 秒，改完立刻生效）
* 写：`save(data)`（原子写）
* 示例：仓库里带一份 `config/settings.example.json`（无密钥），新用户复制成
  `settings.local.json` 即可

文件里就是整份设置，形状与前端 `QF.store.settings()` 一致：
`theme` / `fontScale` / `maxWidth` / `ai` / `search` / `obsidian` …
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: 仓库根（`api/app` 往上两级）
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "settings.local.json"
EXAMPLE_PATH = REPO_ROOT / "config" / "settings.example.json"

#: 按 mtime 缓存：设置页每改一次就写一次文件，读得太频繁
_cache: dict[str, Any] = {"path": "", "mtime": 0.0, "data": None}


def _default_path() -> Path:
    """设置文件默认在哪。

    **打包后必须跟数据目录走**（`~/quizforge/config/settings.local.json`）。
    冻结时 `__file__` 落在 PyInstaller 的解包临时目录（`%TEMP%\\_MEIxxxx`），
    照 `DEFAULT_PATH` 走的话，设置会写进那个每次启动都不同的临时目录 ——
    用户改完重启就没了（实测：安装版的 `/api/settings` 报的正是
    `C:\\...\\AppData\\Local\\Temp\\config\\settings.local.json`）。
    """
    if getattr(sys, "frozen", False):
        return Path.home() / "quizforge" / "config" / "settings.local.json"
    return DEFAULT_PATH


def path() -> Path:
    raw = os.environ.get("QF_SETTINGS_FILE", "").strip()
    return Path(raw).expanduser() if raw else _default_path()


def exists() -> bool:
    return path().is_file()


def load() -> dict[str, Any]:
    """整份设置。文件不存在/坏了 → 空 dict（调用方退回数据库那份）。"""
    target = path()
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {}
    if _cache["path"] == str(target) and _cache["mtime"] == mtime and isinstance(_cache["data"], dict):
        return dict(_cache["data"])
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _cache.update({"path": str(target), "mtime": mtime, "data": data})
    return dict(data)


def save(data: dict[str, Any]) -> Path:
    """整份写回（原子：先写 `.tmp` 再 replace）。返回写到的文件。"""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    # 自己的写要让下一次 load 重新读
    _cache.update({"path": "", "mtime": 0.0, "data": None})
    return target


def merge(patch: dict[str, Any]) -> dict[str, Any]:
    """浅合并（顶层键整块替换，与前端 `deepMerge` 的语义一致）。"""
    data = load()
    for key, value in patch.items():
        data[key] = value
    return data


__all__ = ["DEFAULT_PATH", "EXAMPLE_PATH", "exists", "load", "merge", "path", "save"]
