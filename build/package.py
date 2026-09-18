#!/usr/bin/env python3
"""把应用打成**单文件可执行程序**（PyInstaller），重型组件留在包外。

## 取舍（用户定的：保持轻量）

* **不用 Electron**：前端是普通网页、服务在本机同进程里，套一层 Chromium 只会让
  安装包从 ~25M 变成 150M+。双击即用靠的是"单文件 + 系统自带浏览器"。
* **Pyodide（76M）不进包**：第一次打开时取一次进本机缓存并逐个校验 sha256
  （见 `api/app/heavy_deps.py`），清单 `build/pyodide-manifest.json` 跟着包走。
* **前端产物（`api/web`，约 5M）进包**：它是应用本体，不该让首启去等。
* `vendor/` 仍然自包含（开发与离线开发用），但**不整份进包**。

## 子命令

    python3 build/package.py manifest            # 从 vendor/pyodide 生成哈希清单（要提交）
    python3 build/package.py build               # 出单文件可执行程序（含自检）
    python3 build/package.py build --no-verify   # 只出包
    python3 build/package.py verify <可执行文件>  # 单独跑一次自检

## 必须在本平台构建

PyInstaller **不能交叉编译**：Windows 的 exe 要在 Windows 上跑这条命令。
所以本脚本只出"当前平台"的包；要两个平台就各自跑一次。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
DIST_DIR = BUILD_DIR / "dist"
WORK_DIR = BUILD_DIR / ".pyinstaller"
VENDOR_PYODIDE = ROOT / "vendor" / "pyodide"
MANIFEST_FILE = BUILD_DIR / "pyodide-manifest.json"
WEB_DIR = ROOT / "api" / "web"
LAUNCHER = BUILD_DIR / "launcher.py"

#: uvicorn 是**按字符串**装配协议/循环实现的，PyInstaller 的静态分析看不到它们
HIDDEN_IMPORTS = (
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "app.main",
    "app.routers",
)


def _tolerant_stdout() -> None:
    """别让**控制台编码**把脚本搞崩。

    Windows 的控制台是 GBK（中文区默认），而这份输出里有 `✓` / `✗` ——
    直接打印会 `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`，
    而且它会在**自检跑到一半**的时候炸掉（实测撞过：exe 出来了、自检没跑完，
    看起来像"打包失败了"，其实是打印挂了）。

    只放宽 errors，不改编码：中文在 GBK 下本来就打得出来，编不出的那几个符号
    降级成 `?` 就够了。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):  # 被重定向到奇怪的东西时别硬来
                pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ manifest


def make_manifest() -> int:
    """从本机 `vendor/pyodide/` 生成 `文件名 → sha256` 清单并写进仓库。

    为什么清单要进版本库：它让"下载到的东西对不对"这件事**离线可判断** ——
    校验不该依赖"再去网上取一份期望值"（那等于没校验）。
    """
    if not VENDOR_PYODIDE.is_dir():
        print(f"没有 {VENDOR_PYODIDE} —— 先 `make vendor` 把运行时取回来")
        return 2

    files: dict[str, str] = {}
    total = 0
    for src in sorted(VENDOR_PYODIDE.iterdir()):
        if not src.is_file() or src.name == "SOURCE.md":
            continue
        files[src.name] = _sha256(src)
        total += src.stat().st_size

    payload = {
        "version": "0.26.4",
        "note": "Pyodide 运行时清单：首启下载后逐个校验（build/package.py manifest 生成）",
        "bytes": total,
        "files": files,
    }
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"清单已写出：{MANIFEST_FILE}")
    print(f"  {len(files)} 个文件 · {total / 1024 / 1024:.1f} MB")
    for name in files:
        print(f"    {name}")
    return 0


# --------------------------------------------------------------------- build


#: 内测配置（**不进版本库**，见 .gitignore）：beta 通道的包把它带进去，
#: 于是"没有自带密钥的人也能聊"。正式通道**不带**。
BETA_CONFIG = ROOT / "config" / "ai.local.json"


def build(
    verify_after: bool = True,
    dist: Path | None = None,
    work: Path | None = None,
    channel: str = "beta",
) -> int:
    """出包。`dist` / `work` 可换到别处，`channel` 决定通道（默认内测）。

    为什么 `dist` / `work` 需要能换：仓库如果在 WSL 里，Windows 侧看到的是 UNC 路径
    （`\\\\wsl.localhost\\…`），而 PyInstaller 在 UNC 上写工作目录不稳。
    把工作目录与产物落到本机盘上、源码仍从 UNC 读，就绕开了这件事。

    ## 通道怎么进包（内测和正式要分开）

    冻结之后读不到环境变量，所以把通道**写进包**：

    * `channel.json`（包根）—— 应用启动时靠它认自己是哪条通道（见 `config._default_channel`）；
    * `config/ai.local.json`（内测配置）—— **只有 beta 包带**，正式包连文件都没有，
      所以正式包不可能用到站长的额度；
    * 产物名字也分开：`quizforge-beta` / `quizforge`。
    """
    dist_dir = dist or DIST_DIR
    work_dir = work or WORK_DIR
    if channel not in ("beta", "release"):
        print(f"通道只认识 beta / release，收到的是 {channel!r}")
        return 2
    if not LAUNCHER.is_file():
        print(f"找不到入口 {LAUNCHER}")
        return 2
    if not (WEB_DIR / "index.html").is_file() and not any(WEB_DIR.glob("*.html")):
        print(f"{WEB_DIR} 里没有页面 —— 先 `make web`")
        return 2

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没有 PyInstaller。装一下（只装一次，它是构建期依赖）：")
        print(f"  {sys.executable} -m pip install -r build/requirements-build.txt")
        return 2

    # 这次要带进包的只读资源先摆到一个临时目录里，`--add-data` 只认现成的路径
    staging = work_dir / "bundle"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    (staging / "config").mkdir(parents=True, exist_ok=True)
    (staging / "channel.json").write_text(
        json.dumps(
            {"channel": channel, "builtAt": time.strftime("%Y-%m-%dT%H:%M:%S")},
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )

    if channel == "beta":
        if not BETA_CONFIG.is_file():
            print(f"beta 通道要带内测配置，但 {BETA_CONFIG} 不在 —— 先把它放好再打内测包")
            return 2
        shutil.copy2(BETA_CONFIG, staging / "config" / "ai.local.json")
        print("通道：beta（内测）—— 带上内测配置，试用的人不用自带密钥")
    else:
        print("通道：release（正式）—— **不带**内测配置，用各人自己的密钥")

    name = "quizforge-beta" if channel == "beta" else "quizforge"
    separator = os.pathsep  # Windows 是 ';'，POSIX 是 ':' —— 别写死
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        # **关键**：让静态分析找得到 `app` 包 —— 它在 `api/` 下，而分析时的工作目录
        # 是仓库根，不指这条路它会静默地收不进去（`--hidden-import app.main`
        # 也跟着失效），表现是跑起来才报 `No module named 'app'`（实测撞过）
        "--paths",
        str(ROOT / "api"),
        # 前端产物放到解包根下的 web/：`app.config` 里 web_dir = API_DIR/web，
        # 而打包后 API_DIR 就是解包根（见 heavy_deps._root 的说明）
        "--add-data",
        f"{WEB_DIR}{separator}web",
        # 哈希清单要跟着包走，否则打包后校验不了（会退化成"每次都下"）
        "--add-data",
        f"{MANIFEST_FILE}{separator}build",
        # 通道声明与内测配置：冻结后读不到环境变量，通道只能靠包里的文件认
        "--add-data",
        f"{staging / 'channel.json'}{separator}.",
        "--add-data",
        f"{staging / 'config'}{separator}config",
        # 题库解析器与校验器（352K）。**不能省**：出题流水线用它
        # （`app/toolkit.py` → `tools/question_parser` / `check`），
        # 而 A 段的"资料即入库口"会让桌面应用去跑那条流水线
        "--add-data",
        f"{ROOT / 'tools'}{separator}tools",
        "--console",  # 留个窗口：URL、数据目录、日志都在那儿（关掉窗口就是退出）
    ]
    # 循环变量**别叫 name**：上面那个 `name` 是产物名（quizforge-beta / quizforge），
    # 被覆盖之后"找产物"这一步会去找 `app.routers.exe`（实测：打包明明成功，
    # 却报"没找到产物"，而报出来的名字是最后一个 hidden import）
    for hidden in HIDDEN_IMPORTS:
        args += ["--hidden-import", hidden]
    args.append(str(LAUNCHER))

    print("打包中（这一步要一分多钟）…")
    proc = subprocess.run(args, cwd=str(ROOT))
    if proc.returncode != 0:
        print("打包失败")
        return proc.returncode

    suffix = ".exe" if os.name == "nt" else ""
    artifact = dist_dir / f"{name}{suffix}"
    if not artifact.is_file():
        print(f"打包命令成功了，但没找到产物 {artifact}")
        return 1

    size_mb = artifact.stat().st_size / 1024 / 1024
    print(f"\n产物：{artifact}（{size_mb:.1f} MB）· 通道 {channel}")
    print("  包外的东西：Pyodide 运行时（76M）—— 第一次打开时取一次进本机缓存")

    if verify_after:
        return verify(artifact, expected_channel=channel)
    return 0


# -------------------------------------------------------------------- verify


def _terminate(proc: subprocess.Popen) -> None:
    """结束自检起的那份应用 —— **要连子孙一起**。

    PyInstaller 的单文件模式是"引导进程再起一个真身"，只 `terminate()` 外层，
    内层会继续跑。实测后果很难看：两次自检漏下两个 90MB 的 `quizforge.exe` 没退，
    它们把 exe 文件锁住，**下一次打包直接 PermissionError**（看起来像打包坏了，
    其实是上一次没收拾干净）。

    * Windows：`taskkill /T`（带整棵树）；
    * 其它：起进程时开了新会话组，直接对整组发信号。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict | None:
    """打一次 JSON 接口。写探针要用它（POST）。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _schema_problems(db_path: Path) -> list[str]:
    """对**包跑出来的那个库**做一次结构体检（规则与应用里同一份）。

    这一条是拿一次真实翻车补上的：内测包 chat 一发消息就
    `NOT NULL constraint failed: messages.id`（`messages.id` 是 `BIGINT`，
    SQLite 上不自增）。当时自检只看"接口能不能应答、题库读没读出来" ——
    于是"能启动、能读、**不能写**"的包照样发了出去。
    """
    if not db_path.is_file():
        return []
    if str(ROOT / "api") not in sys.path:
        sys.path.insert(0, str(ROOT / "api"))
    try:
        from app.db import sqlite_rowid_pk_problems  # noqa: PLC0415
    except ImportError as exc:  # 镜像里缺东西时别把它当成"结构没问题"
        return [f"体检规则导入不了：{exc}"]
    return sqlite_rowid_pk_problems(db_path)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def verify(
    artifact: Path, timeout_s: float = 120.0, expected_channel: str | None = None
) -> int:
    """把产物**真的跑起来**，看它能不能读出一个真实题库。

    这一步验的是打包最容易坏、又最晚才被发现的三件事：

    ① **静态前端进没进包** —— 没进的话接口一切正常、页面却白屏；
    ② **空数据目录能不能自建库与表** —— 首启即用；
    ③ **能不能读出真实题库** —— 拿开发库（`data/quizforge.db`）当样本拷进去，
       数一数题。少了这条，一个"能启动但读不到数据"的包也能过自检。

    刻意用一个临时数据目录（`QF_DATA_DIR`）：**不碰用户真实的库**。
    也关掉重型组件的预取（`QF_HEAVY_PREFETCH=false`）—— 自检不该下载 76M。

    超时给 120 秒而不是 60：**刚写出来的 exe 会被 Windows 先扫一遍**
    （Defender / 索引），首次启动能拖到一分多钟，于是"接口没应答"看起来像打包坏了 ——
    实测就撞过（同一份 exe 隔一会儿单独跑，20 秒就起来了）。
    """
    import tempfile

    if not artifact.is_file():
        print(f"找不到 {artifact}")
        return 2

    data_dir = Path(tempfile.mkdtemp(prefix="qf-pkg-check-"))
    seed = ROOT / "data" / "quizforge.db"
    if seed.is_file():
        # WAL 的附属文件一起拷：库是 WAL 模式，只拷主文件会丢掉还没回写的那些页
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{seed}{suffix}")
            if source.is_file():
                shutil.copy2(source, Path(f"{data_dir / 'quizforge.db'}{suffix}"))
        print(f"（拿 {seed.name} 当样本：{seed.stat().st_size / 1024 / 1024:.0f} MB）")
    port = _free_port()
    env = {
        **os.environ,
        "QF_DATA_DIR": str(data_dir),
        "QF_HEAVY_PREFETCH": "false",
        "QF_DEBUG": "true",
    }
    print(f"\n自检：跑起来（端口 {port} · 临时数据目录 {data_dir}）")
    proc = subprocess.Popen(
        [str(artifact), "--port", str(port), "--no-browser"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # 自成一组：退出时能整组带走（见 `_terminate`）
        start_new_session=os.name != "nt",
    )

    base = f"http://127.0.0.1:{port}"
    health: dict | None = None
    page_ok = False
    write_ok = False
    write_detail = ""
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                print("进程自己退出了，输出如下：")
                print((proc.stdout.read() if proc.stdout else "")[-2000:])
                return 1
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=3) as response:
                    health = json.loads(response.read().decode("utf-8"))
                # 静态前端：拿一个真页面（不是在测 404 页）
                with urllib.request.urlopen(f"{base}/quiz.html", timeout=5) as response:
                    page_ok = response.status == 200 and b"<html" in response.read(4096).lower()
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                time.sleep(0.5)

        # **写入探针**：在真包上写一次库（走 progress sync，不碰 AI 那条路）。
        # "能启动、能读"不等于"能写" —— 这个包就是死在写上（见 `_schema_problems`）。
        if health:
            import uuid

            probe = _http_json(
                f"{base}/api/progress/sync",
                {
                    "attempts": [
                        {
                            "id": str(uuid.uuid4()),
                            "questionId": "probe-0001",
                            "at": int(time.time() * 1000),
                            "status": "correct",
                            "score": 1,
                            "response": "写入探针",
                            "topicKey": "",
                        }
                    ]
                },
            )
            write_ok = bool(probe and probe.get("attemptsAccepted") == 1)
            if not write_ok:
                write_detail = str(probe)[:200] if probe is not None else "接口没响应"
    finally:
        _terminate(proc)

    db_file = data_dir / "quizforge.db"
    bank = (health or {}).get("bank") or {}
    ai = (health or {}).get("ai") or {}
    questions = int(bank.get("questions") or 0)
    channel = str((health or {}).get("channel") or "")
    schema_problems = _schema_problems(db_file)
    print(f"  接口：{'通了' if health else '没通'} · 静态页面：{'在' if page_ok else '没拿到'}")
    print(f"  题库：{questions} 题 · 主题 {bank.get('topics', 0)}")
    print(f"  通道：{channel or '（没报）'} · 内测额度：{'可用' if ai.get('betaEnabled') else '不可用'}"
          f"（密钥{'已带' if ai.get('betaConfigured') else '未带'}）")
    print(f"  库文件：{'在' if db_file.is_file() else '不在（有问题）'}（{db_file.name}）")
    print(f"  写一次库：{'成功' if write_ok else '失败 —— ' + (write_detail or '没试成')}")
    print(f"  库结构体检：{'没问题' if not schema_problems else '**有问题**'}")
    for item in schema_problems:
        print(f"      {item}")
    shutil.rmtree(data_dir, ignore_errors=True)

    problems: list[str] = []
    if not health:
        problems.append("接口没应答")
    if not page_ok:
        problems.append("静态页面拿不到（前端没进包）")
    if seed.is_file() and questions < 100:
        problems.append(f"题库没读出来（样本里应当有上千道，实际 {questions} 道）")
    if not write_ok:
        problems.append(f"写库失败（能启动能读、但写不进去）：{write_detail}")
    if schema_problems:
        problems.append("库结构体检没过：" + "；".join(schema_problems))
    if expected_channel and channel != expected_channel:
        problems.append(f"通道不对：期望 {expected_channel}，包自称 {channel or '（空）'}")
    if expected_channel == "beta":
        # 内测包的立身之本：**不带密钥也能聊**。密钥没进包 = 这个包是废的
        if not ai.get("betaEnabled"):
            problems.append("内测包里内测额度不可用（通道没认成 beta？）")
        if not ai.get("betaConfigured"):
            problems.append("内测包里没有内测密钥（config/ai.local.json 没打进去？）")
    if expected_channel == "release" and ai.get("betaEnabled"):
        problems.append("正式包里内测额度却是可用的 —— 内测和正式没分开")
    if problems:
        print("自检没过：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("自检通过 ✓")
    return 0


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    _tolerant_stdout()
    parser = argparse.ArgumentParser(prog="build/package.py", description="单文件打包（不含重型组件）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("manifest", help="从 vendor/pyodide 生成哈希清单")
    build_parser = sub.add_parser("build", help="出单文件可执行程序")
    build_parser.add_argument("--no-verify", action="store_true", help="不出包后自检")
    build_parser.add_argument("--dist", help="产物目录（默认 build/dist）")
    build_parser.add_argument("--work", help="PyInstaller 工作目录（默认 build/.pyinstaller）")
    build_parser.add_argument(
        "--channel",
        choices=("beta", "release"),
        default="beta",
        help="通道：beta（内测，带走内测配置，默认）/ release（正式，不带）",
    )
    verify_parser = sub.add_parser("verify", help="把产物跑起来自检")
    verify_parser.add_argument("artifact", help="可执行文件路径")
    verify_parser.add_argument("--channel", choices=("beta", "release"), help="期望的通道")

    args = parser.parse_args(argv)
    if args.cmd == "manifest":
        return make_manifest()
    if args.cmd == "build":
        return build(
            verify_after=not args.no_verify,
            dist=Path(args.dist) if args.dist else None,
            work=Path(args.work) if args.work else None,
            channel=args.channel,
        )
    if args.cmd == "verify":
        return verify(Path(args.artifact), expected_channel=args.channel)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
