#!/usr/bin/env python3
"""准备 Windows 侧的构建环境：Python + venv + 依赖（**幂等**，可以反复跑）。

## 为什么要有它

打包必须在 Windows 上跑（PyInstaller 不能交叉编译），而这件事**只做一次**却很容易漏步骤：
装 Python（还要装对版本）、建 venv、装两份 requirements、确认 PyInstaller 在。
漏一步的代价是"打包跑到一半报奇怪的错"，于是又把坑重踩一遍。

所以把这四条固化：每条都先检查再动手，已就绪就跳过。

## 它不装什么

不装应用本身、不下载 Pyodide、不动数据 —— 那些是运行时的事（首启自己搞定）。

用法（在 WSL 里跑，它会去驱动 Windows）：

    api/.venv/bin/python build/setup_win.py            # 检查 + 缺什么报什么，不动手
    api/.venv/bin/python build/setup_win.py --install  # 缺 Python 就装（winget）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_win import _decode, _powershell  # noqa: E402  （共用：GBK 解码与 PowerShell 调用）
#: 构建目录（相对 Windows 的 `%USERPROFILE%`）。**必须与 `dist_win.py` 的 `WIN_HOME`
#: 一致**：一个说 `qf-build`、另一个说 `Desktop\QF_dist` 时，venv 与镜像会各建一份，
#: 症状只是"打包时找不到 python"（实测为它白查过一轮）。
WIN_HOME_REL = "Desktop\\QF_dist"
VENV_DIR = WIN_HOME_REL + "\\.venv"
MIRROR_REL = WIN_HOME_REL + "\\src"
PYTHON_VERSION = "3.12"
WINGET_ID = f"Python.Python.{PYTHON_VERSION}"





def _windows_venv_python() -> str:
    """Windows 侧 venv 里那个 python.exe —— **带双引号**，能直接塞进 PowerShell 命令。

    必须是双引号：PowerShell 里单引号不做变量展开，`& '$env:USERPROFILE\\…'`
    会当成一个名叫 `$env:USERPROFILE\…` 的程序去找（实测报 CommandNotFoundException）。
    """
    return '"$env:USERPROFILE\\' + VENV_DIR + '\\Scripts\\python.exe"'



def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="build/setup_win.py", description="准备 Windows 侧构建环境")
    parser.add_argument("--install", action="store_true", help="缺 Python 就用 winget 装")
    args = parser.parse_args(argv)

    problems: list[str] = []

    # ① Windows 上的 Python
    #
    # **不查 `py.exe` 在不在 PATH 上**：PATH 的改动不会传播进"已经在跑的"进程，
    # 从 WSL 里起的 PowerShell 继承的是 WSL 那份环境 —— 装完 Python 之后立刻查
    # 会得到"没装"，然后把人引去重装一遍（实测撞过）。直接看安装位置才准。
    code, out = _powershell(
        "$found = @("
        f"  \"$env:LOCALAPPDATA\\Programs\\Python\\Python{PYTHON_VERSION.replace('.', '')}\\python.exe\","
        f"  \"$env:ProgramFiles\\Python{PYTHON_VERSION.replace('.', '')}\\python.exe\""
        ") | Where-Object { Test-Path $_ } | Select-Object -First 1\n"
        "if ($found) { Write-Output $found } else { Write-Output 'MISSING' }"
    )
    python_exe = out.strip().splitlines()[-1].strip() if out.strip() else "MISSING"
    if code != 0 or not python_exe or "MISSING" in python_exe:
        if args.install:
            print(f"没有 Python {PYTHON_VERSION}，用 winget 装…")
            code, out = _powershell(
                f"winget install --id {WINGET_ID} --exact --source winget "
                f"--accept-package-agreements --accept-source-agreements --disable-interactivity"
            )
            print(out.strip()[-400:])
            if code != 0:
                problems.append(f"winget 装 Python 失败（{code}）")
            # 装完 PATH 在**当前**进程里还没刷新，用绝对路径找
            guess = f"$env:LOCALAPPDATA\\Programs\\Python\\Python{PYTHON_VERSION.replace('.', '')}\\python.exe"
            code, out = _powershell(f"Write-Output '{guess}'")
            python_exe = out.strip().splitlines()[-1].strip() if out.strip() else ""
        else:
            problems.append(
                f"Windows 上没有 Python {PYTHON_VERSION} —— 先跑 "
                f"`winget install --id {WINGET_ID} --exact --accept-package-agreements "
                f"--accept-source-agreements`，或给本脚本加 --install"
            )
            python_exe = ""

    if python_exe:
        print(f"✓ Python：{python_exe}")

    # ② venv
    venv_python = _windows_venv_python()
    if python_exe:
        code, _ = _powershell(
            f"if (Test-Path {venv_python} ) {{ exit 0 }} else {{ exit 1 }}"
        )
        if code != 0:
            print("· 建 venv…")
            code, out = _powershell(
                f"& \"{python_exe}\" -m venv \"$env:USERPROFILE\\{VENV_DIR}\""
            )
            if code != 0:
                problems.append(f"建 venv 失败：{out.strip()[-300:]}")
        if code == 0:
            print(f"✓ venv：%USERPROFILE%\\{VENV_DIR}")

    # ③ 依赖（判据：能不能 import 那几样，而不是"跑没跑过 pip"）
    #
    # 从**镜像**里读 requirements（`%USERPROFILE%\Desktop\QF_dist\src\…`）：
    # 那是 Windows 本地路径，pip 读得稳；从 WSL 仓库读要走 UNC，
    # 而 UNC 在 pip / PyInstaller 这类工具上时好时坏（踩过）。所以顺序是
    # `win-sync` 再 `win-setup`——镜像没有就如实说。
    mirror = "$env:USERPROFILE\\" + MIRROR_REL
    if not problems:
        code, out = _powershell(
            f"if (Test-Path \"{mirror}\\api\\requirements.txt\") {{ exit 0 }} else {{ exit 1 }}"
        )
        if code != 0:
            problems.append("镜像还没建（先 `make win-sync`），依赖清单得从镜像里读")
        else:
            # 这段 Python **一个引号都不带**：PowerShell 往原生命令传参时会把双引号吃掉
            # （实测：`print("ok", …)` 传过去变成 `print(ok, …)` → NameError，
            # 于是每次都被判成"依赖没装"，反复重装几十秒）。判据用退出码就够。
            probe = "import fastapi, sqlalchemy, uvicorn, PyInstaller"
            code, out = _powershell(f"& {venv_python} -c '{probe}'")
            if code != 0:
                print("· 装依赖（两份 requirements 一起）…")
                code, out = _powershell(
                    f"& {venv_python} -m pip install -q --default-timeout=180 "
                    f"-r \"{mirror}\\api\\requirements.txt\" "
                    f"-r \"{mirror}\\build\\requirements-build.txt\""
                )
                if code != 0:
                    problems.append(f"装依赖失败：{out.strip()[-400:]}")
                else:
                    # 装完**再验一次**：刚才那次失败可能只是"没装上"，也可能另有原因
                    code, out = _powershell(f"& {venv_python} -c '{probe}'")
                    if code != 0:
                        problems.append(f"依赖装完仍然 import 不了：{out.strip()[-300:]}")
            if code == 0 and not problems:
                print("✓ 依赖 + PyInstaller")

    # ④ 桌面外壳要的 Node —— 装包那一步（electron-builder）跑在它上面
    #
    # 不装它：那是**用户机器上的全局软件**，不该由这个脚本悄悄装（也说不清装哪个版本）。
    # 只如实报出来，缺了给出安装命令。
    code, out = _powershell(
        'if (Test-Path "C:\\Program Files\\nodejs\\node.exe") { '
        '& "C:\\Program Files\\nodejs\\node.exe" -v } else { "MISSING" }'
    )
    node = out.strip().splitlines()[-1].strip() if out.strip() else "MISSING"
    if "MISSING" in node or code != 0:
        problems.append(
            "Windows 上没有 Node —— 外壳的安装包要它：`winget install --id OpenJS.NodeJS.LTS "
            "--exact --accept-package-agreements --accept-source-agreements`"
        )
    else:
        print(f"✓ Node：{node}")

    if problems:
        print("\n没准备好：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("\nWindows 侧构建环境就绪 —— 接着 `make win-sync` 同步、`make dist` 出包")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
