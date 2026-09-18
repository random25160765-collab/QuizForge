#!/usr/bin/env python3
"""一条命令出 Windows 的包：**同步 → 在 Windows 上打 → 产物归位**。

## 为什么要有它

打包这件事拆开看有四步，而每一步都有"顺序错了就白干"的地方：

1. `win-sync`：把 WSL 这份（唯一事实）镜像到 Windows —— **必须先做**，
   否则打出来的是上一版代码，而症状是"改的东西没生效"，离原因很远；
2. 在 Windows 上用那个 venv 的 python 跑 `build/package.py build`；
3. 产物要**放到人找得到的地方**（桌面 + 仓库的 `build/dist/`）；
4. 自检（`package.py` 里自带）必须过。

把它们串成一条命令，出包就不再依赖"记得那几步"。

## 通道

默认 **beta（内测）**：带走内测配置，试用的人不用自带密钥，数据落在 `~/quizforge-beta`。
`--channel release` 出正式包：不带内测配置，数据落在 `~/quizforge`。

    api/.venv/bin/python build/dist_win.py                  # 内测包（默认）
    api/.venv/bin/python build/dist_win.py --channel release # 正式包
    api/.venv/bin/python build/dist_win.py --no-sync         # 跳过同步（调试用）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_win  # noqa: E402  （同目录的兄弟脚本，复用它的镜像定位与同步）
from sync_win import _decode, _powershell  # noqa: E402  （共用：GBK 解码与 PowerShell 调用）

ROOT = Path(__file__).resolve().parent.parent
WIN_HOME = "$env:USERPROFILE\\qf-build"
DESKTOP_REL = "$env:USERPROFILE\\Desktop"





def _stamp_now() -> str:
    """现在仓库里 `api/web/build.json` 的构建戳（= 开发端正在服务的那一份）。"""
    path = ROOT / "api" / "web" / "build.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("stamp") or "")
    except (OSError, ValueError):
        return ""


def write_stamp_sidecar(name: str, stamp: str) -> None:
    """在那个包**旁边**写一个 `.stamp`：记下"这个包是哪次构建打的"。

    为什么要单独一个文件：戳也写进了包（`/api/health` 会报），但"桌面这个包是不是
    当前代码打的"应当**不用启动**就能问清楚 —— 用户提过"开发端有、exe 里没有"，
    那种情况下当时没有任何地方看得出来，只能靠人问。
    """
    if not stamp:
        return
    _powershell(
        f"Set-Content -Path \"{DESKTOP_REL}\\{name}.stamp\" -Value '{stamp}' -NoNewline -Encoding ascii; "
        f"Copy-Item -Force \"{DESKTOP_REL}\\{name}.stamp\" \"{WIN_HOME}\\dist\\{name}.stamp\""
    )
    print(f"· 构建戳 {stamp} 已随包留在桌面（`make dist-check` 拿它比对）")


def check_desktop() -> int:
    """桌面那个包是不是**当前这份代码**打的（`make dist-check`）。"""
    expected = _stamp_now()
    if not expected:
        print("仓库里还没有 api/web/build.json —— 先跑 `make web`")
        return 2
    print(f"开发端（仓库 api/web）构建戳：{expected}")
    code, out = _powershell(
        f"Get-ChildItem \"{DESKTOP_REL}\\quizforge*.stamp\" -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ Write-Output ($_.BaseName + '=' + (Get-Content $_.FullName -Raw)) }}"
    )
    rows = [line.strip() for line in out.splitlines() if ".stamp" not in line and "=" in line]
    rows = [line for line in rows if line.startswith("quizforge")]
    if not rows:
        print("桌面上没有带戳的包 —— 要么还没打过包，要么那个包是加这套机制之前打的。")
        print("跑一次 `make dist` 就有了。")
        return 1
    bad = 0
    for row in rows:
        label, _, stamp = row.partition("=")
        same = stamp.strip() == expected
        print(f"  {label}：{stamp.strip() or '（没记）'}"
              + ("  ✓ 与开发端一致" if same else "  ✗ 与开发端不一致 —— 重跑 `make dist`"))
        bad += 0 if same else 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="build/dist_win.py", description="出 Windows 包（同步 + 打包 + 归位）")
    parser.add_argument("--channel", choices=("beta", "release"), default="beta", help="通道（默认 beta 内测）")
    parser.add_argument("--no-sync", action="store_true", help="跳过同步（调试用）")
    parser.add_argument("--no-copy", action="store_true", help="不把产物拷到桌面与仓库")
    parser.add_argument(
        "--check", action="store_true", help="只检查桌面那个包是不是当前代码打的（不打包）"
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_desktop()

    mirror = sync_win._windows_src_dir()  # noqa: SLF001
    if mirror is None:
        print("找不到 Windows 侧的镜像目录")
        return 2

    # ① 同步（唯一事实 → 构建镜像）
    if not args.no_sync:
        code = sync_win.sync(mirror)
        if code != 0:
            print("同步没过 —— 先把它弄干净再出包（不然打出来的是上一版代码）")
            return code
    else:
        print("（跳过了同步）")

    # ② 在 Windows 上打（用那个 venv 的 python，**必须**）
    name = "quizforge-beta" if args.channel == "beta" else "quizforge"
    print(f"\n在 Windows 上出包（通道 {args.channel}）… 这一步一两分钟")
    code, out = _powershell(
        f"& \"{WIN_HOME}\\.venv\\Scripts\\python.exe\" -u "
        f"\"{WIN_HOME}\\src\\build\\package.py\" build "
        f"--channel {args.channel} "
        f"--dist \"{WIN_HOME}\\dist\" --work \"{WIN_HOME}\\work\""
    )
    if code != 0:
        # 失败时把**整段**输出给出来：自检的判定就在里面，截断会让人看不到原因
        # （踩过：日志只剩 PyInstaller 的尾巴，"为什么不过"一个字都没有）
        print(out.strip())
        print(f"打包失败（退出码 {code}）")
        return code

    # 成功时只留要紧的行：PyInstaller 会打几百行 INFO，而**自检的结论**夹在中间，
    # 直接 tail 出来的全是它的噪声、"到底过没过"反而看不见（实测撞过好几次）
    noise = re.compile(r"^\s*\d+\s+(INFO|WARNING|DEBUG):")
    keep = [line for line in out.splitlines() if line.strip() and not noise.match(line)]
    print("\n".join(keep))

    # ③ 产物归位（桌面 + 仓库的 build/dist/）
    artifact_rel = f"{WIN_HOME}\\dist\\{name}.exe"
    code, out = _powershell(f"if (Test-Path \"{artifact_rel}\") {{ Write-Output (Resolve-Path \"{artifact_rel}\").Path }}")
    windows_artifact = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or not windows_artifact:
        print("打包说成功了，但没找着产物 —— 去看上面那段输出")
        return 1
    print(f"\n产物：{windows_artifact}")
    # 戳要**在打包成功后立刻写**，不能等桌面拷贝那一步 —— 拷贝可能因为"包正在运行"失败，
    # 而"这个包是哪次构建打的"这件事不该跟着一起丢（实测就丢过一次，留下一个 0 字节的戳）。
    write_stamp_sidecar(name, _stamp_now())

    if not args.no_copy:
        print("· 拷到桌面…")
        _powershell(f"Copy-Item -Force \"{artifact_rel}\" \"{DESKTOP_REL}\\{name}.exe\"")
        # **拷完必须核对哈希**，别信"命令没报错"。
        # 踩过：目标 exe 正在运行 → 文件被锁 → 复制被静默拒绝，
        # 而脚本照样往下走、最后打印"完成"，于是桌面上一直躺着一个旧包，
        # 用户双击看到的是旧行为，还得来回问"为什么没修"。
        code, out = _powershell(
            f"$a = (Get-FileHash \"{artifact_rel}\" -Algorithm SHA256).Hash; "
            f"$b = (Get-FileHash \"{DESKTOP_REL}\\{name}.exe\" -Algorithm SHA256).Hash; "
            f"if ($a -eq $b) {{ Write-Output 'SAME' }} else {{ Write-Output 'DIFFERENT' }}"
        )
        if "SAME" not in out:
            print("桌面那份**没换成**（哈希不一致）—— 多半是它正在运行、文件被锁。")
            print("把它关掉再重跑一次；构建产物本身是好的：")
            print(f"  {windows_artifact}")
            return 1
        print("· 桌面已更新（哈希核对一致 ✓）")
        local_target = ROOT / "build" / "dist" / f"{name}.exe"
        local_target.parent.mkdir(parents=True, exist_ok=True)
        # 从 /mnt/c 拷回 WSL（走的是 drvfs，普通文件拷贝）
        code, out = _powershell(f"Write-Output (Resolve-Path \"{artifact_rel}\").Path")
        source_wsl = sync_win._to_wsl_path(out.strip().splitlines()[-1].strip())  # noqa: SLF001
        if source_wsl and Path(source_wsl).is_file():
            shutil.copy2(source_wsl, local_target)
            size_mb = local_target.stat().st_size / 1024 / 1024
            print(f"· 也留在 {local_target}（{size_mb:.1f} MB，不进版本库）")

    print(f"\n完成。双击 `{name}.exe` 即用；数据在 Windows 的 %USERPROFILE%\\"
          f"{'quizforge-beta' if args.channel == 'beta' else 'quizforge'}\\")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
