"""quizforge 后端应用包。

**这里做一次路径引导**：`pipeline/` 在仓库根（它是脚本目录，不是包），
而服务进程的工作目录是 `api/` —— 不补这一步，任何 `from pipeline import …`
都会以 `ModuleNotFoundError` 收场。

为什么不放在各个调用点各补一次：踩过。笔记路由在自己文件里补过（`_suggester`），
资料路由照着写却漏了，于是"模型归类"一调就 500 —— 而那种 500 在界面上只是
"点了没反应 + 一条报错"，很难联想到是路径问题。补在包入口，谁 import `app` 谁就有。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

#: 仓库根（`api/app/__init__.py` → 上三级）。
REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(REPO_ROOT))


__all__ = ["__version__"]

__version__ = "1.0.0"
