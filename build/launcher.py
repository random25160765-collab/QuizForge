"""双击即用的入口：起本机服务，然后把浏览器打开。

## 为什么不是 Electron

这个应用的前端是**普通网页**，服务端就在本机、同一个进程里 —— 套一层 Chromium
（150M 起）唯一的作用是让窗口看起来像"应用"，代价是安装包大十倍、多一套打包链、
还要跟着上游升 Chromium。所以：**一个 Python 单文件 + 系统自带浏览器**。
（用户明确要求：保持轻量，尤其不要用 Electron。）

## 它做四件事，顺序不能换

1. 挑一个能用的端口（8100 常被别的静态服务器占着 —— 仓库里就踩过一次）；
2. **保证库文件与表都在**（`ensure_schema`）—— 第一次运行时数据目录还是空的；
3. 起 uvicorn（本机回环，不监听外部网卡）；
4. 打开浏览器。数据目录会打印出来，用户要知道自己的东西在哪。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def _ensure_import_path() -> None:
    """让 `import app` 找得到 —— 开发时与打包后**根不在一个地方**。

    * 开发：`app` 在 `api/` 下（uvicorn 平时用 `--app-dir api` 指过去）；
    * 打包：它被解到 `sys._MEIPASS`（单文件）或 exe 旁边（onedir）。

    打包那两种都由 PyInstaller 的导入器管，但直接在仓库里跑这个脚本调试时
    得自己补上 —— 顺手做掉，省得"开发能跑、打包不能跑"这种最难查的偏差。
    """
    if getattr(sys, "frozen", False):
        candidates = [Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).resolve().parent]
    else:
        candidates = [Path(__file__).resolve().parent.parent / "api"]
    for candidate in candidates:
        text = str(candidate)
        if text and Path(text).is_dir() and text not in sys.path:
            sys.path.insert(0, text)


def _free_port(preferred: int) -> int:
    """优先用 `preferred`；被占了就让系统给一个空闲端口。

    本机单用户场景下"端口被占"八成是**自己已经开着一个**，此时换端口比报错好：
    报错会让人以为程序坏了。
    """
    for candidate in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    return preferred


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台是 GBK：中文没问题，但遇到它编不出的符号会直接抛异常
    # （`build/package.py` 的自检就这么崩过一次）
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="quizforge", description="本机起服务并打开界面")
    parser.add_argument("--port", type=int, default=8100, help="优先使用的端口（占了就自动换）")
    parser.add_argument("--no-browser", action="store_true", help="不自动开浏览器（自检用）")
    args = parser.parse_args(argv)

    _ensure_import_path()

    # 1) 端口
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    # 2) 库与表（第一次运行时数据目录还不存在，这里一并建好）
    from app.config import get_settings
    from app.db import ensure_schema, get_engine

    settings = get_settings()
    ensure_schema(get_engine())

    # 3) 服务
    import uvicorn

    from app.main import app as application

    tag = "内测版" if settings.is_beta else "正式版"
    print(f"quizforge 已启动：{url}（{tag}）")
    print(f"数据目录：{settings.data_dir}（库文件 {settings.database_url.rsplit('/', 1)[-1]}）")
    if settings.is_beta:
        print("内测版走站长的额度；想用自己的密钥，在设置里填。")
    print("关掉这个窗口（或按 Ctrl-C）就停。")

    # 4) 浏览器（等一拍再开，免得抢在服务就绪之前）
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="info")
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
