#!/usr/bin/env python3
"""看一眼网页 —— 自研的网页迭代工具（常驻无头 Chromium + 直接说 CDP）。

## 为什么要自己写（用户 2026-11-11）

"这几天已经被这个折腾很久了……我要的是快速迭代的能力，对速度有要求。"

两个现成方案都不合用：

* `playwright-cli`：每次要起 daemon、`open`、`resize`、`screenshot` 四五条命令，
  一次十几秒；状态放在它的会话里，卡住时只能等，还常报 "browser is not open"。
* `agent-browser`：浏览器是常驻的，但那个进程放着一天没人动就僵 ——
  `Page.navigate` 直接超时、`close` 也不返回，命令就挂着。

它们的通病是**超时不由我们管**：卡住时没人拍板，只能干等。这里反过来：

* **浏览器常驻，我们自己是主人**：`--remote-debugging-port=0` 起一个，端口写进
  `/tmp/qf-look/state.json`；之后的每条命令只连一次 WebSocket（几十毫秒）。
* **每一段都有截止时间**：探活 0.6s、启动 8s、单条 CDP 命令 5s、截图 8s。
  到点就抛错 —— 命令永远会返回，不会挂住。
* **僵了就杀掉重来**：探活失败即视为僵死，kill 旧进程再起一个。
* **图先出，再谈加载完**：`shot` 不等 load 事件（那正是会挂的地方），只轮询
  `document.readyState` 到预算为止，到点照样截图 —— 用户要的就是"拿到那张图"。

## 用法（要用 API 的 venv：里面有 websockets）

    api/.venv/bin/python tools/look.py open "http://127.0.0.1:8100/chat.html?c=2161"
    api/.venv/bin/python tools/look.py size 390x844
    api/.venv/bin/python tools/look.py shot /tmp/phone.png
    api/.venv/bin/python tools/look.py shot /tmp/phone.png --url URL --size 390x844
    api/.venv/bin/python tools/look.py click ".modepill"
    api/.venv/bin/python tools/look.py type "textarea" "你好"
    api/.venv/bin/python tools/look.py eval "document.querySelectorAll('.chatmsg').length"
    api/.venv/bin/python tools/look.py text
    api/.venv/bin/python tools/look.py status
    api/.venv/bin/python tools/look.py stop

`size` 会记住（写进 state.json），后面每次截图都按它来 —— 改手机版式时只设一次。
"""

from __future__ import annotations

import argparse
import base64
import glob as globmod
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover - 只在用错 python 时走到
    raise SystemExit(
        "要用 API 的 venv 跑（里面才有 websockets）：\n"
        "  api/.venv/bin/python tools/look.py ...\n"
        "或者走 make：make look ARGS=\"shot /tmp/x.png\""
    ) from None

STATE_DIR = Path(tempfile.gettempdir()) / "qf-look"
PROFILE = STATE_DIR / "profile"
STATE = STATE_DIR / "state.json"
CHROME_LOG = STATE_DIR / "chrome.log"

#: 探活上限。**必须短**：它唯一的用途是区分"活着"与"僵死"，0.6 秒足够本机回答。
PROBE_TIMEOUT_S = 0.6
#: 冷启动上限：完整的 Chromium 在慢机器上 2–3 秒，8 秒已经很宽了。
BOOT_TIMEOUT_S = 8.0
#: 单条 CDP 命令的上限。截图偶尔要 1 秒多，5 秒封顶。
CMD_TIMEOUT_S = 5.0


class LookError(RuntimeError):
    """一次操作失败（**都已经发生时**，不是"可能失败"）。"""


# --------------------------------------------------------------------- 浏览器


def find_chrome() -> str:
    """找一个完整版 Chromium（headless-shell 不支持 `--headless=new` 那些开关）。"""
    env = os.environ.get("QF_CHROME")
    if env and Path(env).is_file():
        return env

    home = Path.home()
    # 顺序有讲究：这台机器上 Playwright 那个 chromium-1244 **起得来进程、就是不写
    # DevToolsActivePort**（实测：进程活着、日志空着、8 秒也没有调试端口），而
    # agent-browser 那份 chrome-153 是正常的 —— 先试能用的那个。
    patterns = [
        str(home / ".agent-browser/browsers/chrome-*/chrome"),
        str(home / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
        str(home / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    for pattern in patterns:
        found = sorted(one for one in globmod.glob(pattern) if Path(one).is_file())
        if found:
            return found[-1]

    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    raise LookError(
        "没找到 Chromium。三条路任选：\n"
        "  1. sudo apt install chromium（要你自己跑）\n"
        "  2. QF_CHROME=/path/to/chrome 指给我\n"
        "  3. playwright-cli install-browser chromium（下到缓存里我就认得）"
    )


#: **必须绕开环境里的代理**。这台机器（以及所有开了 Clash 的机器）都有
#: `http_proxy=http://127.0.0.1:7890`，而 `urllib` 默认照着它走 —— 于是连
#: `127.0.0.1:<调试端口>` 也会被塞给代理，探活永远失败（实测：端口明明在听，
#: 却一直报"没准备好"）。空 `ProxyHandler` 就是"谁也别代理我"。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(port: int, path: str, *, method: str = "GET", timeout: float = PROBE_TIMEOUT_S):  # noqa: ANN201
    """打一次 DevTools 的 HTTP 端点。**任何异常都当"不可用"**，由调用方决定下一步。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - 只连本机
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def _alive(port: int) -> bool:
    try:
        return bool(_http(port, "/json/version")["webSocketDebuggerUrl"])
    except Exception:  # noqa: BLE001 - 探活就是这样：问不到就是不在
        return False


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _kill_old(state: dict) -> bool:
    """把上一次的浏览器清掉。**返回是否清干净** —— 这点很重要，见 `ensure_browser`。"""
    pid = state.get("pid")
    if not pid:
        return True
    try:
        os.kill(int(pid), 15)
    except OSError:
        return True
    # 给它半秒自己退；不走就强杀 —— 僵死的进程不会理会 SIGTERM
    for _ in range(10):
        time.sleep(0.05)
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
    try:
        os.kill(int(pid), 9)
    except OSError:
        return True
    # SIGKILL 都不走 = 卡在 D 状态（不可中断的 I/O 等待）。这时**不要再起一个**：
    # 起得来也是卡，只会堆僵尸 —— 今天堆到 95 个 chrome 就是这么来的。
    time.sleep(0.2)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return True
    return False


def ensure_browser() -> tuple[int, dict]:
    """活着就复用，僵了就换一个。返回 (port, state)。"""
    state = _read_state()
    if state.get("port") and _alive(int(state["port"])):
        return int(state["port"]), state

    if not _kill_old(state):
        raise LookError(
            "上一次的浏览器进程杀不掉（SIGKILL 都没反应）—— 它多半卡在 D 状态。"
            + _system_hint()
            + "\n这种情况先重启 WSL；重启后 `look status` 会显示干净，再跑就行。"
        )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    # 用户目录每次留存（登录态/缓存），但启动前清掉锁文件，免得"上次没退干净"卡住
    for junk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (PROFILE / junk).unlink(missing_ok=True)

    cmd = [
        chrome,
        # **必须是 `--headless`，不能写 `--headless=new`**。132 起 `--headless` 就是
        # "新 headless"，而 `=new` 这个旧写法在 154 上**不报错、也不生效** —— 进程会
        # 退化成有头模式，于是 DevTools 端口不监听、`DevToolsActivePort` 不写、日志里
        # 一个字都没有（实测卡了两小时，最后靠四组对照才定位）。
        "--headless",
        # WSL 里没有显示服务：显式指定 headless 的 Ozone 平台，免得它去找 X。
        "--ozone-platform=headless",
        "--no-sandbox",
        # WSL 没有真 GPU：走软件光栅。**不加 `--disable-gpu`** —— 那会连软件后端一起关掉，
        # 合成器就起不来（和 agent-browser 那份能跑的启动参数对齐）。
        "--use-angle=swiftshader-webgl",
        "--enable-unsafe-swiftshader",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-features=Translate",
        # 端口交给系统挑：固定的 9222 容易和别的东西撞，撞了就是"连不上"
        "--remote-debugging-port=0",
        f"--user-data-dir={PROFILE}",
        "about:blank",
    ]
    log = CHROME_LOG.open("ab")
    proc = subprocess.Popen(  # noqa: S603 - 路径来自 find_chrome，参数是常量
        cmd,
        stdout=log,
        stderr=log,
        start_new_session=True,  # 脱离本命令：命令退出了它也要活着
    )

    port_file = PROFILE / "DevToolsActivePort"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    port = 0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise LookError(
                "Chromium 启动就退出了。它说：\n"
                + _tail(CHROME_LOG)
                + "\n（常见是缺共享库，按报错装齐即可；装包要 sudo，你跑。）"
            )
        try:
            first = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                port = int(first)
                if _alive(port):
                    break
        except Exception:  # noqa: BLE001 - 文件还没写出来
            pass
        time.sleep(0.05)
    else:
        proc.kill()
        raise LookError(
            f"Chromium 在 {BOOT_TIMEOUT_S:g} 秒内没准备好。它说：\n"
            + _tail(CHROME_LOG)
            + _system_hint()
            + "\n下次会重新起一个；反复这样就看看上面那句。"
        )

    state = {"pid": proc.pid, "port": port}
    state.update({k: v for k, v in _read_state().items() if k in ("size", "dpr", "mobile")})
    _write_state(state)
    return port, state


def _system_hint() -> str:
    """启动失败时看一眼系统负载 —— 这一步能把"工具的锅"和"机器的锅"分开。

    实测（2026-11-11）：这台 WSL 的 loadavg 冲到 37、`ip addr` / `ss` / `apt` 全卡在
    D 状态时，Chromium 会**进程活着、日志空着、连 profile 目录都不建** —— 谁来都一样，
    换 playwright 也一样。工具自己说出来，比让人对着空日志猜强。
    """
    try:
        load = float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:  # noqa: BLE001
        return ""
    if load < 8:
        return ""
    return (
        f"\n注意：这台机器的 loadavg={load:g}，已经很高了 —— 先确认是不是系统/磁盘卡了"
        "（`cat /proc/loadavg`；或者看看有没有一堆卡在 D 状态的进程）。"
        "这种情况换任何浏览器都一样，不是这个工具的问题。"
    )


def _tail(path: Path, lines: int = 6) -> str:
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except Exception:  # noqa: BLE001
        return "（没有日志）"
    return "\n".join("  " + one for one in rows[-lines:])


# ------------------------------------------------------------------------ CDP


class Page:
    """一个页面级的 CDP 连接。**每条命令都有截止时间**，这是不挂住的关键。"""

    def __init__(self, port: int) -> None:
        target = self._pick_target(port)
        ws_url = target.get("webSocketDebuggerUrl") or ""
        if not ws_url:
            raise LookError("这个页面没有调试地址（可能刚被关掉），重跑一次。")
        self._id = 0
        # `proxy=None` 同样是"别代理我"：ws 是本机端口，绕道代理只会连不上。
        # （老版本 websockets 没有这个参数，退回去就行。）
        # `legacy=True`：我们是"连上、用、关"的一次性客户端，不套 `with` ——
        # 新版 websockets 用它就不发弃用警告了。
        try:
            self.ws = ws_connect(
                ws_url,
                max_size=64 * 1024 * 1024,
                open_timeout=CMD_TIMEOUT_S,
                proxy=None,
                legacy=True,
            )
        except TypeError:
            self.ws = ws_connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=CMD_TIMEOUT_S)
        self.port = port

    @staticmethod
    def _pick_target(port: int) -> dict:
        try:
            targets = _http(port, "/json/list", timeout=CMD_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            raise LookError("问不到页面列表：" + str(exc)[:120]) from exc
        pages = [one for one in targets if one.get("type") == "page"]
        if pages:
            # **多出来的页面关掉**：profile 会恢复上次的标签页，重启几次就攒一堆
            # （实测攒到 4 个）。只留第一个，其余关掉 —— 浏览器里永远只有一个"在看"。
            for extra in pages[1:]:
                try:
                    _http(port, "/json/close/" + str(extra.get("id")), timeout=2.0)
                except Exception:  # noqa: BLE001 - 关不掉也不影响这次操作
                    pass
            return pages[0]
        # 一个页面都没有：自己开一个（Chrome 111 起 /json/new 只认 PUT）
        try:
            return _http(port, "/json/new?url=about:blank", method="PUT", timeout=CMD_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            raise LookError("开不了新页面：" + str(exc)[:120]) from exc

    def send(self, method: str, params: dict | None = None, timeout: float = CMD_TIMEOUT_S) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LookError(f"{method} 超过 {timeout:g} 秒没有回应 —— 浏览器可能僵了。")
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError:  # websockets 的 recv 超时
                raise LookError(f"{method} 超过 {timeout:g} 秒没有回应 —— 浏览器可能僵了。") from None
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue  # 事件，丢掉
            if "error" in msg:
                raise LookError(method + " 失败：" + str(msg["error"].get("message") or msg["error"])[:160])
            return msg.get("result") or {}

    def eval_js(self, expression: str, timeout: float = CMD_TIMEOUT_S):  # noqa: ANN201
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text") or "未知错误"
            raise LookError("页面里的脚本报错：" + str(text)[:200])
        return (result.get("result") or {}).get("value")

    def apply_theme(self, theme: str) -> None:
        """把主题钉死成 `light`（默认）。

        两道都要：`prefers-color-scheme` 是无头浏览器默认报 dark 的地方，而本项目的
        页面会把启动时读到的配色**写进 `<html data-theme>`**（见 `ui.js` 的
        `applyTheme`）—— 只改媒体查询，属性已经是 dark 了，图还是暗的。所以：
        媒体查询管后续判断，注入脚本管每次新文档的属性。
        """
        if not theme:
            return
        self.send(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-color-scheme", "value": theme},
                    # 入场动画会让截图撞在"半透明"那一帧上（实测：整页发灰）。
                    # 本项目的 CSS 到处都有 `prefers-reduced-motion` 分支，顺手用上。
                    {"name": "prefers-reduced-motion", "value": "reduce"},
                ]
            },
        )
        try:
            self.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "try{document.documentElement.dataset.theme=%s}catch(e){}"
                    % json.dumps(theme)
                },
            )
        except LookError:
            pass  # 老内核没有这个命令也不影响（下面还会直接设当前文档）

    def force_theme(self, theme: str) -> None:
        """当前这份文档也设一次（注入脚本只对**之后**加载的文档生效）。

        **还要写回 localStorage**：本项目的主题是存在 `quizforge.v1.settings` 里的，
        页面每次渲染都按它把 `<html data-theme>` 再盖一遍 —— 只改属性会被盖回去
        （实测：设完立刻变回 dark）。存进去才是一劳永逸。
        """
        if not theme:
            return
        try:
            self.eval_js(
                "(() => {"
                " try { const k='quizforge.v1.settings';"
                " const raw = JSON.parse(localStorage.getItem(k) || '{}');"
                " raw.theme = %s; localStorage.setItem(k, JSON.stringify(raw)); } catch (e) {}"
                " document.documentElement.dataset.theme = %s;"
                " return document.documentElement.dataset.theme; })()"
                % (json.dumps(theme), json.dumps(theme))
            )
        except LookError:
            pass

    def apply_size(self, state: dict) -> None:
        # 主题先钉死（默认浅色 —— 与用户实际看的一致），再谈尺寸
        self.apply_theme(str(state.get("theme") or "light"))
        size = state.get("size")
        if not size:
            return
        width, height = size
        dpr = float(state.get("dpr") or 2)
        mobile = bool(state.get("mobile"))
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(width),
                "height": int(height),
                "deviceScaleFactor": dpr,
                "mobile": mobile,
            },
        )

    def wait_ready(self, budget_ms: int) -> str:
        """等 `document.readyState` 到 complete，**到点就走**（不抛错）。

        为什么不听 `Page.loadEventFired`：这正是会挂的地方 —— 页面里一个长轮询
        没结束，事件永远不来。轮询 readyState 至少还受我们的预算管。
        """
        deadline = time.monotonic() + budget_ms / 1000.0
        last = ""
        while time.monotonic() < deadline:
            try:
                last = str(self.eval_js("document.readyState", timeout=2.0) or "")
            except LookError:
                # 导航过程中执行上下文会被销毁 —— 这正是"还没好"，等下一轮
                time.sleep(0.05)
                continue
            if last == "complete":
                return last
            time.sleep(0.05)
        return last

    def wait_for(self, selector: str, budget_ms: int) -> int:
        """等某个选择器**出现**（异步拉数据渲染的那种页面靠它），到点返回 0。

        `readyState=complete` 只说明文档加载完；聊天页的消息是之后 fetch 回来才画的 ——
        实测刚 complete 时 `.chatmsg` 还是 0 条，截出来是空态。等它出现再截才有意义。
        """
        deadline = time.monotonic() + budget_ms / 1000.0
        expr = "document.querySelectorAll(%s).length" % json.dumps(selector)
        while time.monotonic() < deadline:
            try:
                count = int(self.eval_js(expr, timeout=2.0) or 0)
            except LookError:
                time.sleep(0.05)
                continue
            if count:
                return count
            time.sleep(0.05)
        return 0

    def screenshot(self, path: Path, *, selector: str = "", full: bool = False) -> tuple[int, int]:
        params: dict = {"format": "png", "captureBeyondViewport": bool(full)}
        if selector:
            rect = self.eval_js(
                "(() => { const el = document.querySelector(%s);"
                " if (!el) return null; const r = el.getBoundingClientRect();"
                " return {x: r.x + window.scrollX, y: r.y + window.scrollY,"
                " width: r.width, height: r.height}; })()" % json.dumps(selector)
            )
            if not rect or not rect.get("width"):
                raise LookError("页面上没有匹配 '" + selector + "' 的可见元素。")
            params["clip"] = {
                "x": rect["x"],
                "y": rect["y"],
                "width": rect["width"],
                "height": rect["height"],
                "scale": 1,
            }
        result = self.send("Page.captureScreenshot", params, timeout=8.0)
        data = result.get("data") or ""
        if not data:
            raise LookError("截图回来了，但是空的。")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        width, height = 0, 0
        try:
            width, height = _png_size(path.read_bytes()[:32])
        except Exception:  # noqa: BLE001
            pass
        return width, height

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _png_size(head: bytes) -> tuple[int, int]:
    """从 PNG 头里读宽高（不引第三方库）。"""
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError:
        raise LookError("尺寸要写成 宽x高（例：390x844），给的是：" + text) from None


# --------------------------------------------------------------------- 命令


def cmd_open(args) -> int:
    port, state = ensure_browser()
    page = Page(port)
    try:
        if args.size:
            state["size"] = list(_parse_size(args.size))
        if args.dpr:
            state["dpr"] = args.dpr
        if args.mobile:
            state["mobile"] = True
        if args.theme:
            state["theme"] = args.theme
        _write_state({**state, "port": port})
        page.apply_size(state)
        page.send("Page.enable")
        started = time.perf_counter()
        page.send("Page.navigate", {"url": args.url})
        ready = page.wait_ready(args.wait)
        page.force_theme(str(state.get("theme") or "light"))
        extra = ""
        if args.until:
            count = page.wait_for(args.until, args.until_wait)
            extra = f"  {args.until}×{count}"
            if not count:
                extra += "（没等到，照样往下走）"
        took = (time.perf_counter() - started) * 1000
        print(f"✓ {args.url}  readyState={ready or '?'}{extra}  {took:.0f}ms")
    finally:
        page.close()
    return 0


def cmd_shot(args) -> int:
    port, state = ensure_browser()
    page = Page(port)
    try:
        if args.url:
            if args.size:
                state["size"] = list(_parse_size(args.size))
            if args.dpr:
                state["dpr"] = args.dpr
            if args.theme:
                state["theme"] = args.theme
            _write_state({**state, "port": port})
            page.apply_size(state)
            page.send("Page.enable")
            page.send("Page.navigate", {"url": args.url})
            page.wait_ready(args.wait)
            page.force_theme(str(state.get("theme") or "light"))
        else:
            page.apply_size(state)
        if args.until:
            count = page.wait_for(args.until, args.until_wait)
            if not count:
                print(f"（{args.until} 没等到，照现在这样截）", file=sys.stderr)
        # **主题放在最后一步**：页面拉完数据会按它存的设置再盖一遍
        # （实测：等消息渲染完之后，之前设的 light 又变回 dark）。
        page.force_theme(str(state.get("theme") or "light"))

        out = Path(args.path).expanduser()
        started = time.perf_counter()
        width, height = page.screenshot(out, selector=args.selector or "", full=args.full)
        took = (time.perf_counter() - started) * 1000
        size_kb = out.stat().st_size // 1024
        print(f"✓ {out}  {width}×{height}  {took:.0f}ms  {size_kb}KB")
    finally:
        page.close()
    return 0


def cmd_size(args) -> int:
    port, state = ensure_browser()
    state["size"] = list(_parse_size(args.size))
    if args.dpr:
        state["dpr"] = args.dpr
    state["mobile"] = bool(args.mobile)
    _write_state({**state, "port": port})
    page = Page(port)
    try:
        page.apply_size(state)
    finally:
        page.close()
    print(f"✓ 视口 {state['size'][0]}×{state['size'][1]} @{state.get('dpr', 2):g}x"
          + ("（手机模式）" if state["mobile"] else ""))
    return 0


def cmd_eval(args) -> int:
    port, _state = ensure_browser()
    page = Page(port)
    try:
        value = page.eval_js(args.js)
    finally:
        page.close()
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)
    return 0


def cmd_text(args) -> int:
    port, _state = ensure_browser()
    page = Page(port)
    try:
        value = page.eval_js("document.body ? document.body.innerText : ''")
    finally:
        page.close()
    print(value or "")
    return 0


def cmd_click(args) -> int:
    port, _state = ensure_browser()
    page = Page(port)
    try:
        page.eval_js(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) throw new Error('没有这个元素');"
            " el.scrollIntoView({block: 'center'});"
            " el.click(); return true; })()" % json.dumps(args.selector)
        )
    finally:
        page.close()
    print("✓ 点了 " + args.selector)
    return 0


def cmd_type(args) -> int:
    port, _state = ensure_browser()
    page = Page(port)
    try:
        page.eval_js(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) throw new Error('没有这个元素');"
            " el.focus(); el.value = %s;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return true; })()" % (json.dumps(args.selector), json.dumps(args.text))
        )
    finally:
        page.close()
    print("✓ 写进 " + args.selector)
    return 0


def cmd_status(args) -> int:
    state = _read_state()
    port = int(state.get("port") or 0)
    if not port or not _alive(port):
        print("浏览器：没在跑（下一条命令会自己起一个）")
        return 0
    pages = _http(port, "/json/list", timeout=CMD_TIMEOUT_S)
    live = [one for one in pages if one.get("type") == "page"]
    print(f"浏览器：pid={state.get('pid')} port={port} 页面={len(live)}")
    for one in live:
        print("  " + str(one.get("url") or "about:blank")[:100])
    if state.get("size"):
        print(f"视口：{state['size'][0]}×{state['size'][1]} @{state.get('dpr', 2):g}x"
              + ("（手机模式）" if state.get("mobile") else ""))
    return 0


def cmd_stop(args) -> int:
    state = _read_state()
    clean = _kill_old(state)
    STATE.unlink(missing_ok=True)
    if not clean:
        print("✗ 进程杀不掉（卡在 D 状态）—— 得重启 WSL 才能清。", file=sys.stderr)
        return 1
    print("✓ 关了")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="look", description="看一眼网页：常驻无头 Chromium，命令直接说 CDP")
    sub = parser.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("open", help="打开一个网址")
    one.add_argument("url")
    one.add_argument("--size", default="")
    one.add_argument("--dpr", type=float, default=0)
    one.add_argument("--wait", type=int, default=4000, help="等 readyState 的预算（毫秒）")
    one.add_argument("--until", default="", help="再等这个选择器出现（异步渲染的页面用）")
    one.add_argument("--until-wait", type=int, default=6000, dest="until_wait")
    one.add_argument("--mobile", action="store_true")
    one.add_argument("--theme", choices=["light", "dark"], default="",
                     help="配色档；不写 = 沿用上次（默认 light）。改暗色样式时用它")
    one.set_defaults(func=cmd_open)

    shot = sub.add_parser("shot", help="截图（可带 --url 一步到位）")
    shot.add_argument("path")
    shot.add_argument("--url", default="", help="先打开这个网址再截")
    shot.add_argument("--size", default="")
    shot.add_argument("--wait", type=int, default=4000)
    shot.add_argument("--selector", default="", help="只截这个元素（CSS 选择器）")
    shot.add_argument("--until", default="", help="等这个选择器出现再截（异步渲染的页面用）")
    shot.add_argument("--until-wait", type=int, default=6000, dest="until_wait")
    shot.add_argument("--full", action="store_true", help="整页（captureBeyondViewport）")
    shot.add_argument("--dpr", type=float, default=0, help="只在不带 --url 时被忽略；带 --url 时一并设")
    shot.add_argument("--theme", choices=["light", "dark"], default="",
                      help="配色档；不写 = 沿用上次（默认 light）")
    shot.set_defaults(func=cmd_shot)

    size = sub.add_parser("size", help="设视口（记在 state 里，之后一直用）")
    size.add_argument("size")
    size.add_argument("--dpr", type=float, default=0)
    size.add_argument("--mobile", action="store_true")
    size.set_defaults(func=cmd_size)

    ev = sub.add_parser("eval", help="在页面里跑一段 JS，打印结果")
    ev.add_argument("js")
    ev.set_defaults(func=cmd_eval)

    sub.add_parser("text", help="打印页面文本").set_defaults(func=cmd_text)

    click = sub.add_parser("click", help="点一个元素")
    click.add_argument("selector")
    click.set_defaults(func=cmd_click)

    typ = sub.add_parser("type", help="往输入框写值（会派发 input/change）")
    typ.add_argument("selector")
    typ.add_argument("text")
    typ.set_defaults(func=cmd_type)

    sub.add_parser("status", help="看浏览器/页面/视口").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="关掉浏览器").set_defaults(func=cmd_stop)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LookError as exc:
        print("✗ " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
