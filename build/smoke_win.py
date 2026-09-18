#!/usr/bin/env python3
"""对 Windows 上那个 exe 跑一次**真实对话烟测**：起包 → 发一句 → 关掉。

## 为什么单独一条命令

打包自检（`package.py verify`）刻意不碰 AI —— 它只验"能启动、能读、能写"。
可"对话"这条路上有几个环节只在真跑时才暴露：用户消息落库、助手占位、
流式增量、最终回写。**内测包第一次发出去就死在这条路上**
（`messages.id` 是 `BIGINT`，SQLite 上不自增 → 一发「你好」就 IntegrityError），
而当时自检全绿。所以这一层必须有，而且必须跑在**真的那个 exe** 上。

它**会花钱**（走一次模型），所以不挂进 `make dist`，发版前手动跑一次。

    make smoke                       # 对 %USERPROFILE%\\qf-build\\dist\\quizforge-beta.exe
    build/smoke_win.py --exe <路径>   # 指定别的包

## 为什么 HTTP 请求要在 Windows 那边发

WSL 与 Windows 的 `127.0.0.1` **不是同一个**（网络命名空间不同），
从 WSL 直连 Windows 的回环地址通常不通。所以"发请求"这一步交给
Windows 那边的 python 跑 `build/smoke_chat.py`，这边只负责起包、拿地址、收尾。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_win  # noqa: E402
from sync_win import _decode, _powershell  # noqa: E402  （共用：GBK 解码与 PowerShell 调用）

WIN_HOME = "$env:USERPROFILE\\qf-build"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="build/smoke_win.py", description="对 exe 做真实对话烟测")
    parser.add_argument("--exe", help="可执行文件路径（默认 qf-build\\dist\\quizforge-beta.exe）")
    parser.add_argument("--text", default="你好", help="发什么")
    args = parser.parse_args(argv)

    mirror = sync_win._windows_src_dir()  # noqa: SLF001
    if mirror is None:
        print("找不到 Windows 侧的镜像目录")
        return 2

    if args.exe:
        exe_windows = args.exe
        exe_wsl = sync_win._to_wsl_path(args.exe)  # noqa: SLF001
    else:
        exe_windows = f"{WIN_HOME}\\dist\\quizforge-beta.exe"
        code, out = _powershell(f"Write-Output (Resolve-Path \"{exe_windows}\").Path")
        exe_wsl = sync_win._to_wsl_path(out.strip().splitlines()[-1].strip()) if out.strip() else ""
    if not exe_wsl or not Path(exe_wsl).is_file():
        print(f"找不到那个包：{exe_windows} —— 先 `make dist`")
        return 2
    # 打印**解析后**的路径：`$env:USERPROFILE\…` 那种是给 PowerShell 看的，
    # 直接打出来人读着像没解析（实测被自己绊了一次）
    print(f"要对这个包做烟测：{exe_wsl}")

    # 起包（让它自己挑端口：8100 常被占），从它的输出里读真实地址
    log = Path("/tmp/qf-smoke-run.log")
    with log.open("wb") as handle:
        process = subprocess.Popen(  # noqa: S603
            [exe_wsl, "--no-browser"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = ""
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                print("包自己退出了，输出如下：")
                print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
                return 1
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
            match = re.search(r"(http://127\.0\.0\.1:\d+/)", text)
            if match:
                url = match.group(1).rstrip("/")
                break
            time.sleep(1)
        if not url:
            print("包起来了但没打印地址（看日志 /tmp/qf-smoke-run.log）")
            return 1
        print(f"它自己挑的地址：{url}")

        # 发请求那一步交给 Windows 那边的 python（WSL 的 127.0.0.1 不是它的）
        code, out = _powershell(
            f"& \"{WIN_HOME}\\.venv\\Scripts\\python.exe\" -u "
            f"\"{WIN_HOME}\\src\\build\\smoke_chat.py\" --url {url} --text \"{args.text}\""
        )
        print(out.strip())
        if code != 0:
            print(f"烟测没过（退出码 {code}）")
            return code
        return 0
    finally:
        _powershell("taskkill /F /T /IM quizforge-beta.exe | Out-Null; taskkill /F /T /IM quizforge.exe | Out-Null")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        print("已把那个包关掉")


if __name__ == "__main__":
    raise SystemExit(main())
