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

文件里是**跨机器有意义的那部分设置**，形状与前端 `QF.store.fileSettings()` 一致：
`theme` / `fontScale` / `maxWidth` / `ai` / `search` / `obsidian` / `turso` …
只在本机的状态（`chatRead` 读到哪儿、`graph` 面板与布局参数、`basket` 选题篮）
**不落这里**；滤镜记录（`lens` / 旧名 `skills`）是**数据**，住 `lens_records` 表。
名单见 `app/settings_store.py` 的 `LOCAL_ONLY`。

**2026-11-10 起不再有 `syncPassphrase`**：端到端加密的钥匙由 turso 的
地址 + 令牌推导（见 `app/sync.py` 的 `_derived_passphrase`），不落任何文件。
老设备设置文件里残留的那一串会被忽略，导出设置时也会被丢掉
（`FILE_ONLY` 里保留这个键名只为兜住旧文件）。

**任何来源都过 `complete()`**（2026-11-09）：手改的文件、从剪贴板粘进来的半份 JSON、
另一台机器拷来的旧文件，都可能缺键或带着废键。骨架写在 `DEFAULTS`，
与 `config/settings.example.json`、前端 `store.js` 的 `DEFAULT_SETTINGS` 同一份形状。
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

#: 设置的**骨架**。文件缺失 / 被手改残 / 导入半份时拿它补齐。
#:
#: `ai` 里刻意没有 `enabled`（2026-11-09 用户：「设置页那个启用 AI 删掉 ——
#: 这就是个 AI 应用」）：能不能用只看有没有填密钥。
DEFAULTS: dict[str, Any] = {
    "theme": "light",
    "fontScale": 1,
    "maxWidth": 860,
    "shuffle": False,
    "pinnedTopics": [],
    "ai": {
        "baseUrl": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "apiKey": "",
        "timeoutMs": 90000,
        "temperature": 0.2,
        "jsonMode": True,
        "fallbackSelfRate": True,
        "strictRubric": True,
    },
    "search": {},
    "obsidian": {"vaultPath": ""},
    "turso": {"url": "", "token": "", "enabled": False},
}

#: **只住在这份文件里的键**：读得到、写得到，但不进数据库、不随设置同步上云。
#:
#: `syncPassphrase` 是对话 / 记忆 / 计划书的端到端加密口令。它一旦被写进
#: `app_settings`，就会随设置整表同步到 Turso —— 钥匙和锁一起上云，加密就白做了。
#: 名单由 `settings_store.normalize()` 在落库 / 上传前剔掉。
FILE_ONLY: tuple[str, ...] = ("syncPassphrase",)

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


def deep_merge(base: dict, patch: Any) -> dict:
    """递归合并：`patch` 里给了的键覆盖，没给的保持 `base`。

    **必须是深的**（2026-11-09 修）：设置页改密钥只发 `{ai: {apiKey}}`，
    浅合并会把整个 `ai` 块换成只剩 `apiKey` —— model / baseUrl 一起没了。
    """
    out = dict(base or {})
    if not isinstance(patch, dict):
        return out
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def complete(data: Any) -> dict:
    """任何一种来源的设置都过这一道：补全骨架、剔掉废键。"""
    out = deep_merge(DEFAULTS, data if isinstance(data, dict) else {})
    ai = out.get("ai")
    if isinstance(ai, dict):
        ai.pop("enabled", None)  # 旧的「启用 AI」开关：已删
    return out


def exists() -> bool:
    return path().is_file()


def load() -> dict:
    """整份设置（**文件不存在 → 空 dict**，调用方据此退回数据库那份）。

    文件在但缺键/带废键时照样返回补全后的整份：读的地方不必自己防缺字段。
    """
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
    data = complete(data)
    _cache.update({"path": str(target), "mtime": mtime, "data": data})
    return dict(data)


def save(data: dict[str, Any]) -> Path:
    """整份写回（原子：先写 `.tmp` 再 replace）。返回写到的文件。

    `FILE_ONLY` 那些键（同步口令）会被写回来：调用方手里那份通常来自数据库，
    本来就不含它们 —— 不补一下，改个主题就把钥匙弄丢了。
    """
    target = path()
    payload = dict(data)
    current = load()
    for key in FILE_ONLY:
        if not payload.get(key) and current.get(key):
            payload[key] = current[key]

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    # 里面有 AI 密钥 / Turso 令牌 / 同步口令：只有本人可读写
    try:
        target.chmod(0o600)
    except OSError:
        pass
    # 自己的写要让下一次 load 重新读
    _cache.update({"path": "", "mtime": 0.0, "data": None})
    return target


def example() -> dict[str, Any]:
    """示例那份（`settings.example.json`）；读不到/坏了 → 直接给骨架。

    打包版里示例文件可能没跟进去（移动端尤其），不能让它把新用户挡在门外。
    """
    try:
        data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return complete({})
    return complete(data)


def merge(patch: dict[str, Any]) -> dict[str, Any]:
    """把一份 patch 合进现有设置（深合并）。

    文件还没建时以骨架为底：新用户第一次只改个主题，别让落盘的只有那个键。
    """
    return complete(deep_merge(load() or deep_merge({}, DEFAULTS), patch))


__all__ = [
    "DEFAULTS",
    "DEFAULT_PATH",
    "EXAMPLE_PATH",
    "FILE_ONLY",
    "complete",
    "deep_merge",
    "example",
    "exists",
    "load",
    "merge",
    "path",
    "save",
]
