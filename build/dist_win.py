#!/usr/bin/env python3
"""一条命令出 Windows 的包：**同步 → 后端 exe → 桌面外壳的安装包 → 归位**。

## 为什么要有它

打包这件事拆开看有五步，而每一步都有"顺序错了就白干"的地方：

1. 同步：把 WSL 这份（唯一事实）镜像到 Windows —— **必须先做**，
   否则打出来的是上一版代码，而症状是"改的东西没生效"，离原因很远；
2. 在 Windows 上用那个 venv 的 python 跑 `build/package.py build`（含自检）；
3. 把那个 exe 套进桌面外壳（`desktop/`，Electron），出安装包；
4. 产物要**放到人找得到的地方**（桌面 + 仓库的 `build/dist/`）；
5. 要验"页面真画出来了"就 `--install-check`：静默重装、跑起来、逐页截图。

把它们串成一条命令，出包就不再依赖"记得那几步"。

## 只有一个版本

2026-11-08 起没有分发通道：产物固定 `quizforge.exe` + `QFStudio Setup <版本>.exe`，
数据固定 `~/quizforge`（老的 `~/quizforge-beta` 在首次启动时自动搬过去）。

    api/.venv/bin/python build/dist_win.py                 # 出包（后端 + 安装包）
    api/.venv/bin/python build/dist_win.py --no-sync       # 跳过同步（调试用）
    api/.venv/bin/python build/dist_win.py --no-shell      # 只出后端 exe
    api/.venv/bin/python build/dist_win.py --install-check # 装一遍、逐页截图
    api/.venv/bin/python build/dist_win.py --check         # 桌面那个包是不是当前代码
"""

from __future__ import annotations

import argparse
import json
import ntpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync_win  # noqa: E402  （同目录的兄弟脚本，复用它的镜像定位与同步）
from sync_win import _decode, _powershell  # noqa: E402  （共用：GBK 解码与 PowerShell 调用）

ROOT = Path(__file__).resolve().parent.parent
# 打包目录：**桌面上看得见的地方**（用户 2026-11-08 指定）。所有中间产物
# （源码镜像、venv、PyInstaller 工作目录、产物、安装包）都在这里面，不藏到别处。
WIN_HOME = "$env:USERPROFILE\\Desktop\\QF_dist"
DESKTOP_REL = "$env:USERPROFILE\\Desktop"

#: 桌面外壳（Electron）的构建镜像 —— 它在镜像里的位置由 `sync_win` 决定。
SHELL_DIR = f"{WIN_HOME}\\src\\desktop"
NODE_DIR = "C:\\Program Files\\nodejs"

#: npm 要用的镜像。electron 与 electron-builder 的二进制**直连必断**（实测只解出 8.7MB
#: 就停在半路），见 `desktop/README.md` 的踩坑记录。`Path` 也要自己补：从 WSL 里起的
#: PowerShell 继承的是 WSL 那份环境，看不见 Windows 装 node 时改的 PATH。
NPM_ENV = (
    f'$env:Path = "{NODE_DIR};" + $env:Path; '
    '$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"; '
    '$env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"; '
)





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


def build_shell() -> tuple[int, str]:
    """在 Windows 上把桌面外壳（Electron）打成安装包。返回 `(退出码, 安装包路径或错误输出)`。

    为什么要它：后端只是个本机服务，双击之后看到的是浏览器。外壳把那个服务装进一个
    窗口里 —— 用户点的是桌面图标，不是"先起服务再开网址"。

    **外壳不重复带前端**：页面仍由后端自己提供（`web/` 已经打进了后端包），这里只装
    `main.js` 与那个 exe（`desktop/package.json` 的 `extraResources`）。
    """
    shell = SHELL_DIR
    have = _powershell(
        f'if (Test-Path "{shell}\\node_modules\\electron\\dist\\electron.exe") {{ "YES" }} else {{ "NO" }}'
    )
    if "YES" not in have[1]:
        print("· 装外壳依赖（Electron 二进制约 100M，第一次要几分钟）…")
        code, out = _powershell(
            NPM_ENV
            + f'Set-Location "{shell}"; & npm.cmd install --registry=https://registry.npmmirror.com '
            f'--no-audit --no-fund; exit $LASTEXITCODE'
        )
        if code != 0:
            return code, "npm install 失败：\n" + out

    # **先过一遍语法**：main.js 里的探针是一段模板字面量，注释里混进一个反引号
    # 就能把模板提前结束 —— 打出来的安装包**能装上、双击就弹**
    # "A JavaScript error occurred in the main process"，
    # 而 electron-builder 自己不查语法（实测踩过，用户是看报错截图才知道的）。
    print("· 查外壳的语法（node --check）…")
    code, out = _powershell(
        NPM_ENV + f'Set-Location "{shell}"; & node.exe --check main.js; exit $LASTEXITCODE'
    )
    if code != 0:
        return code, "desktop\\main.js 有语法错误（装上去会在启动时崩）：\n" + out

    print("· 打安装包（electron-builder，约一分钟）…")
    code, out = _powershell(NPM_ENV + f'Set-Location "{shell}"; & npm.cmd run dist; exit $LASTEXITCODE')
    if code != 0:
        return code, "electron-builder 失败：\n" + out

    code, out = _powershell(
        f'Get-ChildItem "{shell}\\dist\\*.exe" -ErrorAction SilentlyContinue | '
        f'Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName'
    )
    found = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not found:
        return 1, "外壳打完了，但 `desktop\\dist\\` 里没有安装包（看上面 electron-builder 的输出）"
    return 0, found


#: 安装完之后应用在哪（NSIS 的用户级安装位置，`package.json` 里 perMachine=false）
APP_EXE = "$env:LOCALAPPDATA\\Programs\\QFStudio\\QFStudio.exe"

#: 装一遍 + 跑一遍 + 逐页截图。PowerShell 写成"平铺的一串语句"而不是 f-string：
#: 它满是大括号，塞进 f-string 要逐对翻倍，改一次错一次。
INSTALL_CHECK_PS = r"""
$ErrorActionPreference = 'Continue'
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Get-Process QFStudio,quizforge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$un = "$env:LOCALAPPDATA\Programs\QFStudio\Uninstall QFStudio.exe"
if (Test-Path $un) { Start-Process -FilePath $un -ArgumentList '/S' -Wait; Start-Sleep -Seconds 3 }
$installer = Get-ChildItem "$env:USERPROFILE\Desktop\QF_dist\src\desktop\dist\*.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $installer) { Write-Output 'NO-INSTALLER'; exit 1 }
Write-Output ("installer=" + $installer)
Start-Process -FilePath $installer -ArgumentList '/S' -Wait
Start-Sleep -Seconds 3
$app = "$env:LOCALAPPDATA\Programs\QFStudio\QFStudio.exe"
if (-not (Test-Path $app)) { Write-Output 'NO-APP'; exit 1 }
Remove-Item "$env:APPDATA\quizforge-desktop" -Recurse -Force -ErrorAction SilentlyContinue
$data = "$env:TEMP\qf-install-check"
Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
$env:QF_DATA_DIR = $data
$env:QF_HEAVY_PREFETCH = 'false'
$env:QF_SHELL_PROBE = 'chat,quiz,library'
$p = Start-Process -FilePath $app -PassThru
Start-Sleep -Seconds __WAIT__
Write-Output ("alive=" + (-not $p.HasExited))
Get-Process QFStudio,quizforge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$shots = "$env:TEMP\qf-install-shots"
Remove-Item $shots -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $shots -Force | Out-Null
Copy-Item "$env:APPDATA\quizforge-desktop\window*.png" $shots -Force -ErrorAction SilentlyContinue
Write-Output ("SHOTS=" + $shots)
Write-Output ("LOG=" + "$env:APPDATA\quizforge-desktop\shell.log")
"""


def install_check(wait_s: int = 45) -> int:
    """静默重装桌面应用，然后**把装好的那个**跑起来，逐页留下探针与截图。

    为什么非要装完再看：白屏这类问题在开发端看不出来 —— 开发端有浏览器缓存、有 DevTools、
    数据也全。这里刻意做的三件事都和真用户一致：
      ① 静默重装（用刚打出来的安装包，不是仓库里那份代码）；
      ② 清掉外壳的 userData（首启的 localStorage / 旧日志）；
      ③ 后端用**临时数据目录**起 —— 新用户的库就是空的，对话页也必须开得出来
         （用户报的就是这个："chat 页面还是纯白的，至少要有那个新对话的界面"）。

    `QF_SHELL_PROBE` 让外壳依次打开那几页、各抓一张图（见 `desktop/main.js`）。
    """
    script = INSTALL_CHECK_PS.replace("__WAIT__", str(wait_s))
    code, out = _powershell(script, timeout=wait_s + 300)
    print(out.strip())
    if code != 0:
        print(f"安装 / 启动这一步没过（退出码 {code}）")
        return code

    # 探针从**日志文件**里读，不从 PowerShell 的输出里读：
    # 那段输出过的是中文区的控制台编码（GBK），UTF-8 的日志被它转一道就成了乱码 ——
    # 乱码里夹进一个 `"` 或 `\` 就能让 `json.loads` 整行解析失败，
    # 于是"这一页明明探到了"却从结论里消失（实测：quiz 那一行就这么丢了）。
    log_path = ""
    for line in out.splitlines():
        if "LOG=" in line:
            log_path = line.split("LOG=", 1)[1].strip()
    log_text = ""
    if log_path:
        log_wsl = sync_win._to_wsl_path(log_path)  # noqa: SLF001
        if log_wsl and Path(log_wsl).is_file():
            log_text = Path(log_wsl).read_text(encoding="utf-8", errors="replace")
    for line in log_text.splitlines():
        if "probe" in line or "tour" in line or "fail-load" in line or "render-gone" in line:
            print(line)

    # 逐页判：`probe <页名> {json}`；没有页名的那几条按 `href` 归到它当时所在的页面。
    #
    # 判据是**这一页有没有字**（`text`），不是 `rootKids`：后者只说明"挂进了 #app-root"，
    # 而页面完全可以画在别处（资料页那版就把内容直接挂在 `.view` 下）——
    # 拿它当判据会把好好的页面判成空（实测就误报过一次）。
    bad: list[str] = []
    seen: dict[str, dict] = {}
    for line in log_text.splitlines():
        marker = line.find("probe ")
        if marker < 0 or "{" not in line:
            continue
        rest = line[marker + 6:].strip()
        name, _, payload = rest.partition(" ")
        if not payload.startswith("{"):
            name, payload = "", rest
        try:
            info = json.loads(payload)
        except ValueError:
            continue
        if not name:
            href = str(info.get("href") or "")
            name = str(info.get("page") or "") or (href.rsplit("/", 1)[-1].split("?")[0].removesuffix(".html"))
        if name:
            seen[name] = info
    for name, info in seen.items():
        text = str(info.get("text") or "").strip()
        if not text:
            bad.append(
                f"{name}：内容区是空的（rootKids={info.get('rootKids')}，"
                f"viewKids={info.get('viewKids')}，href={info.get('href')}）"
            )
    # 对话页输入框上方那排按钮里必须有「滤镜」：它就是用户报的那一处，
    # 只有"没有任何会话"的首屏才会漏画（见 `renderEmptyThread` 里那句注释）。
    chat_tools = str((seen.get("chat") or {}).get("tools") or "")
    if "chat" in seen and "滤镜" not in chat_tools:
        bad.append(f"chat：工具栏里没有「滤镜」药丸（现在只有：{chat_tools or '空'}）")

    shots = ""
    for line in out.splitlines():
        if "SHOTS=" in line:
            shots = line.split("SHOTS=", 1)[1].strip()
    if shots:
        local = Path("/tmp/qf-install-shots")
        shutil.rmtree(local, ignore_errors=True)
        source = sync_win._to_wsl_path(shots)  # noqa: SLF001
        if source and Path(source).is_dir():
            shutil.copytree(source, local)
            print(f"截图（看这几张就知道每页画成什么样）：{local}")

    if not seen:
        print("一条探针都没有 —— 外壳没跑起来，或者日志没写出来")
        return 1
    if bad:
        print("\n**有页面是空的**：")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print(f"\n逐页看过：{', '.join(sorted(seen))} 都有内容 ✓")
    return 0


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
    parser.add_argument("--no-sync", action="store_true", help="跳过同步（调试用）")
    parser.add_argument("--no-copy", action="store_true", help="不把产物拷到桌面与仓库")
    parser.add_argument("--no-shell", action="store_true", help="只出后端 exe，不打桌面外壳的安装包")
    parser.add_argument(
        "--check", action="store_true", help="只检查桌面那个包是不是当前代码打的（不打包）"
    )
    parser.add_argument(
        "--install-check",
        action="store_true",
        help="把装好的那个应用真跑一遍（静默重装 + 逐页截图），看每一页画成什么样",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_desktop()
    if args.install_check:
        return install_check()

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
    name = "quizforge"
    print("\n在 Windows 上出包… 这一步一两分钟")
    code, out = _powershell(
        f"& \"{WIN_HOME}\\.venv\\Scripts\\python.exe\" -u "
        f"\"{WIN_HOME}\\src\\build\\package.py\" build "
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

    # ④ 桌面外壳（Electron）→ 安装包。
    #
    # 放在后端之后：外壳装的**就是刚打出来的那个 exe**（`desktop/package.json` 的
    # extraResources 指向 `QF_dist\dist\quizforge.exe`），顺序反过来会装进上一版后端 ——
    # 症状是"界面改了、功能没改"，最难查的那一类。
    installer = ""
    if args.no_shell:
        print("（跳过了外壳安装包）")
    else:
        print("\n打桌面外壳（安装包）…")
        code, result = build_shell()
        if code != 0:
            print(result.strip()[-4000:])
            print("外壳没打出来 —— 后端 exe 本身是好的，可以先直接双击它")
            return code
        installer = result
        print(f"· 安装包：{installer}")

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

        if installer:
            # 安装包也拷到桌面：**用户要双击的是它**，不是那个 exe（exe 只是后端）
            #
            # 文件名要用 `ntpath.basename`：在 Linux 上 `Path()` 不把 `\` 当分隔符，
            # 整条 Windows 路径会被当成一个文件名 —— 于是哈希核对对着一个不存在的
            # 位置比，报"没换成"，而复制其实成功了（实测就撞了这一下）。
            installer_name = ntpath.basename(installer)
            _powershell(f'Copy-Item -Force "{installer}" "{DESKTOP_REL}"')
            code, out = _powershell(
                f"$a = (Get-FileHash \"{installer}\" -Algorithm SHA256).Hash; "
                f"$b = (Get-FileHash \"{DESKTOP_REL}\\{installer_name}\" -Algorithm SHA256).Hash; "
                f"if ($a -eq $b) {{ Write-Output 'SAME' }} else {{ Write-Output 'DIFFERENT' }}"
            )
            if "SAME" not in out:
                print(f"桌面上的安装包没换成（哈希不一致）—— 看是不是同名文件正开着：{installer_name}")
                return 1
            print(f"· 安装包已拷到桌面（{installer_name}）")
            shots = ROOT / "build" / "dist"
            shots.mkdir(parents=True, exist_ok=True)
            source_wsl = sync_win._to_wsl_path(installer)  # noqa: SLF001
            if source_wsl and Path(source_wsl).is_file():
                target = shots / installer_name
                shutil.copy2(source_wsl, target)
                size_mb = target.stat().st_size / 1024 / 1024
                print(f"· 也留在 {target}（{size_mb:.1f} MB，不进版本库）")

    if installer:
        print("\n完成。双击桌面上的安装包装一次；装完从桌面/开始菜单的 QFStudio 启动。")
        print("  装完想确认每一页真画出来了：`api/.venv/bin/python build/dist_win.py --install-check`")
    else:
        print(f"\n完成。双击 `{name}.exe` 即用；数据在 Windows 的 %USERPROFILE%\\quizforge\\")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
