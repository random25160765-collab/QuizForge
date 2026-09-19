"""和本机桌面打交道的那一点点：在系统文件管理器里打开一个目录。

为什么值得单独一个模块：它是**唯一**一处我们会去启动别的程序。所以规矩写在这里，
而不是散在路由里：

* **路径只由服务端给**。接口那边永远从"已登记的库 / 资料根"里取真实路径，
  绝不让客户端传一个路径进来 —— 否则这就成了一个"按请求打开任意目录"的口子
  （`notes.safe_path` 管的是"别越出库"，这里管的是"根本不存在这条路"）。
* **打不开不算错**。没装 `xdg-open`、SSH 会话、无桌面环境、容器里 —— 都是正常情况。
  返回 `opened: False` 让界面上给一句人话，而不是抛 500 让人以为库坏了。
* **不经 shell**。命令与参数分开放进列表，`..`、空格、`;` 都只是普通字符。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def command_for(platform: str, path: str) -> list[str]:
    """各平台打开一个目录的命令。抽成纯函数就是为了能测 —— 这里没有别的逻辑。"""
    if platform == "darwin":
        return ["open", path]
    if platform.startswith("win"):
        # `explorer` 打开目录是它的正常用法（不是"打开文件夹里的文件"那种）
        return ["explorer", path]
    return ["xdg-open", path]


def reveal(path: Path) -> dict:
    """在系统文件管理器里打开这个目录。返回 `{"opened": bool, ...}`。"""
    target = str(Path(path))
    if not Path(path).exists():
        return {"opened": False, "path": target, "why": "这个目录不在本机（可能被移走了）"}
    cmd = command_for(sys.platform if os.name != "nt" else "win32", target)
    try:
        # Popen 而不是 run：文件管理器是**长驻**程序，run 会一直等它关掉
        subprocess.Popen(  # noqa: S603 —— 命令是我们自己拼的固定几种，参数不经 shell
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"opened": False, "path": target, "why": str(exc)}
    return {"opened": True, "path": target}


__all__ = ["command_for", "reveal"]
