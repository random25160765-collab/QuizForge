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
import os
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


def _log_file() -> Path | None:
    """日志落在数据目录下。

    Windows 的包是 `--noconsole`（没有终端窗口），所以"出问题时去哪儿看"这件事
    得有个答案 —— 就在数据目录里，和库文件并排放着。
    """
    try:
        from app.config import get_settings

        target = Path(get_settings().data_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target / "quizforge.log"
    except Exception:  # noqa: BLE001 —— 日志都写不了就只能放弃写日志，别因此起不来
        return None


def _ensure_streams() -> None:
    """`--noconsole` 的包里 `sys.stdout` / `stderr` 是 **None** —— 直接 print 会抛。

    这是去掉终端之后最先撞上的坑：窗口没了，`print` 仍在，于是"启动即闪退"。
    有流就用流（开发时看得见），没有就丢进日志文件。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            log = _log_file()
            try:
                setattr(sys, name, log.open("a", encoding="utf-8") if log else open(os.devnull, "w"))
            except OSError:
                setattr(sys, name, open(os.devnull, "w"))


def _say(message: str) -> None:
    """打印一份，同时落进日志（这样"用户看不到终端"也不等于"没有记录"）。"""
    print(message, flush=True)
    log = _log_file()
    if log is None:
        return
    try:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def _fatal(title: str, detail: str) -> None:
    """起不来的时候：写日志 + 在 Windows 上弹一个框。

    没有终端之后，"静默失败"是最糟的结果 —— 用户会以为程序坏了却什么也看不到。
    """
    _say(f"[启动失败] {title}\n{detail}")
    log = _log_file()
    if log is not None:
        _say(f"日志：{log}")
    if os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415

            text = f"{title}\n\n{detail[-800:]}"
            if log is not None:
                text += f"\n\n日志：{log}"
            ctypes.windll.user32.MessageBoxW(None, text, "QuizForge 启动失败", 0x10)
        except Exception:  # noqa: BLE001 —— 弹不出来也没关系，日志已经在磁盘上了
            pass


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
    # ① 先把输出接住：`--noconsole` 的包里 stdout/stderr 是 None，print 会当场抛
    _ensure_streams()
    # ② Windows 控制台是 GBK：中文没问题，但它编不出的符号会直接抛异常
    #    （`build/package.py` 的自检就这么崩过一次）
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="QuizForge", description="本机起服务并打开界面")
    parser.add_argument("--port", type=int, default=8100, help="优先使用的端口（占了就自动换）")
    parser.add_argument("--no-browser", action="store_true", help="不自动开浏览器（自检用）")
    args = parser.parse_args(argv)

    _ensure_import_path()

    # 1) 端口
    port = _free_port(args.port)
    base = f"http://127.0.0.1:{port}/"

    # 2) 库与表（第一次运行时数据目录还不存在，这里一并建好）
    from app.config import get_settings
    from app.db import ensure_schema, get_engine

    settings = get_settings()
    tag = "内测版" if settings.is_beta else "正式版"
    _say(f"QuizForge {tag} · 数据目录：{settings.data_dir}")
    try:
        ensure_schema(get_engine())
    except Exception as exc:  # noqa: BLE001 —— 起不来的话要说清楚，不能静默退出
        import traceback

        _fatal("库没能初始化", f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-1200:]}")
        return 1

    # 3) 服务
    import uvicorn

    from app.main import app as application

    _say(f"已启动：{base}（{tag}）")
    if settings.is_beta:
        _say("内测版走站长的额度；想用自己的密钥，在设置里填。")

    # 4) 浏览器打开的是**启动页**，不是主界面：
    #    它自己会做启动检查、画进度条，就绪后再跳进去（用户要的 splash + 进度条）。
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(base + "starting.html")).start()

    uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="info")
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
