"""Android 侧的启动器：把后端在 APK 里拉起来。

与 `build/launcher.py` 的差别只有三件事，其余（ensure_schema、uvicorn 参数）
刻意保持一致：

1. **数据目录由 Java 传进来**（`context.getFilesDir()` 在 Android 上才是可写且
   备份策略正确的位置），不是 `~/quizforge`；
2. **不开浏览器**，也没有终端 —— 日志写进数据目录，Java 侧失败时读它；
3. **web 根是 Java 解压出来的**（APK 的 assets 不能直接当静态目录挂载）。

本模块只做「配置 + 起服务」，不碰任何业务逻辑，也不 import `app` 之外的仓库代码：
这样它既能在设备上跑，也能在桌面用同一份仓库调试（见 `assemble-python.sh` 的说明）。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: tools 的 4 个模块（check / question_parser / topics / dataset）就在同级目录，
#: `toolkit.py` 会把这个路径插进 sys.path 再 `from check import …`。
TOOLS_DIR = HERE / "tools"

_state: dict[str, str] = {"phase": "idle", "detail": ""}


def _log(message: str) -> None:
    _state["detail"] = message
    try:
        target = Path(os.environ.get("QF_DATA_DIR", ".")) / "android_boot.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def configure(data_dir: str, web_dir: str) -> None:
    """在 import `app.config` **之前**设好环境变量 —— Settings 是模块级单例。"""
    data = Path(data_dir)
    os.environ["QF_DATA_DIR"] = str(data)
    os.environ["QF_WEB_DIR"] = str(web_dir)
    os.environ["QF_TOOLS_DIR"] = str(TOOLS_DIR)
    # 设置文件的默认路径是「仓库根/config/settings.local.json」；在 APK 里那个根是
    # 只读的 AssetFinder，必须显式指到数据目录，否则设置页写不进去。
    os.environ["QF_SETTINGS_FILE"] = str(data / "config" / "settings.local.json")
    # 对话同步的端到端密钥（2026-11-10 起）**由 Turso 的地址 + 令牌推导**，
    # 手机上没有任何要保管的口令文件 —— 这里不再设 `QF_SYNC_KEY_FILE`。
    # 手机上**首启就取** Pyodide（146M）：用户 2026-11-10 明确"大的依赖不该让它在
    # 背后自己下，首次安装时显示进度条"。启动页（SplashActivity）会轮询
    # `/api/heavy/status` 把百分比画出来；取不到也只影响"跑 Python"这一项。
    os.environ.setdefault("QF_HEAVY_PREFETCH", "1")
    # 校验清单（3KB）由 BootHelper 从 assets 拷到数据目录。**没有它就不下载**：
    # Chaquopy 里 `heavy_deps` 按 `__file__` 推出来的 ROOT 与仓库不同，推不到
    # 仓库里那份 `build/pyodide-manifest.json`，必须显式指过去。
    manifest = data / "config" / "pyodide-manifest.json"
    if manifest.is_file():
        os.environ.setdefault("QF_HEAVY_MANIFEST", str(manifest))
    # 随包的"补丁"文件（清单里有、CDN 上没有，例如自带的 schemdraw）：首启从这里拷
    bundle = data / "config" / "pyodide-bundle"
    if bundle.is_dir():
        os.environ.setdefault("QF_HEAVY_BUNDLE", str(bundle))
    # 正文**懒加载**：同步只落条目与切片坐标，点开哪片才从云端取哪块
    # （31.7MB 切片正文不再整包落地，见 `app/slice_cloud.py`）。
    os.environ.setdefault("QF_LAZY_BODIES", "1")
    # 手机不跑向量：3.8 万窗口约 168MB（占库 82%），重算一次要很久。
    os.environ.setdefault("QF_EMBED_DISABLED", "1")
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


def ensure_database() -> str:
    """建库 / 补表。工作库由 Java 侧建好，一定是空库 —— 题库不随包，首轮同步拉。"""
    from app.db import ensure_schema, get_engine, get_session_factory  # noqa: PLC0415 —— 必须在设完环境变量之后

    engine = get_engine()
    ensure_schema(engine)
    # 老版本在手机上整包落过资料正文（~39MB）与向量（~168MB）：懒加载开启后清一次
    from app.slice_cloud import slim  # noqa: PLC0415

    db = get_session_factory()()
    try:
        info = slim(db)
    finally:
        db.close()
    return f"db ok: {engine.url} slim={info}"


def crypto_check() -> str:
    """自检加密后端：Android 上不是 `cryptography`，而 Turso 同步的密文必须两端一致。

    这些输入固定，桌面侧用一段同样的代码就能对拍（见 mobile/README.md）。
    派生慢是正常的：手机上 scrypt 走 hashlib，n 比生产值小一档（2**14 vs 2**15）
    只为把启动自检控制在毫秒级，算法本身没有差别。
    """
    from app.crypto_compat import AESGCM, BACKEND, Scrypt  # noqa: PLC0415

    salt = bytes(range(16))
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(b"quizforge-probe")
    nonce = b"\x00" * 12
    blob = AESGCM(key).encrypt(nonce, b"quizforge", b"aad")
    back = AESGCM(key).decrypt(nonce, blob, b"aad")
    return (
        f"crypto backend={BACKEND} roundtrip={back == b'quizforge'} "
        f"key={key.hex()[:16]} blob={blob.hex()[:24]}"
    )


def restore_tikzjax_gz(web: Path) -> str:
    """把 `.gzz` 改回 `.gz` —— TikZJax 只认后者。

    为什么需要这一趟：AGP 在 `mergeAssets` 时会把 assets 里的 `.gz` **解压并
    去掉后缀**（实测 `core.dump.gz` 变成 160MB 的 `core.dump`，`tex_files/` 的
    633 个 `.gz` 全部失去后缀），而引擎是 `fetch('core.dump.gz')` /
    `fetch('tex_files/x.sty.gz')` —— 一个都取不到，手机上所有 TikZ 图都会变成
    `tikzjax-broken`。所以 `build-apk.sh` 进包前把它们改成 `.gzz`，到这里改回来。

    幂等：没有 `.gzz`（已恢复过 / 桌面形态）就什么也不做。
    """
    root = web / "assets" / "tikzjax"
    if not root.is_dir():
        return "tikzjax：没有这个目录（跳过载荷改名）"
    renamed = 0
    for src in root.rglob("*.gzz"):
        src.replace(src.with_name(src.name[:-4] + ".gz"))
        renamed += 1
    return f"tikzjax：{renamed} 个载荷还原为 .gz"


def status() -> str:
    """给 Java 侧轮询用的一行状态（不含异常栈，栈在日志里）。"""
    return f"{_state['phase']}: {_state['detail']}"


def start(data_dir: str, web_dir: str, port: int = 8731) -> str:
    """起服务。返回可读状态；任何失败都写进 _state，不向 Java 抛异常。"""
    _state["phase"] = "starting"
    try:
        configure(data_dir, web_dir)
        _log(f"configured data={data_dir} web={web_dir} tools={TOOLS_DIR}")
        _log(restore_tikzjax_gz(Path(web_dir)))

        message = ensure_database()
        _log(message)
        _log(crypto_check())

        import uvicorn  # noqa: PLC0415

        from app.main import create_app  # noqa: PLC0415

        application = create_app()
        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=int(port),
            log_level="warning",
            # Android 上没有 reload、没有多进程；worker 线程里装信号处理器会抛。
            workers=1,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        _state["phase"] = "serving"
        _log(f"serving on 127.0.0.1:{port}")
        server.run()  # 阻塞 —— 由 Java 侧的线程承载
    except BaseException as exc:  # noqa: BLE001 —— 启动失败必须留痕，不能静默
        _state["phase"] = "failed"
        _state["detail"] = f"{type(exc).__name__}: {exc}"
        _log("FAILED\n" + traceback.format_exc())
        return status()
    return status()


def start_in_thread(data_dir: str, web_dir: str, port: int = 8731) -> str:
    """Chaquopy 从 Java 调进来时用这个：立刻返回，服务在后台线程跑。"""
    thread = threading.Thread(
        target=start,
        args=(data_dir, web_dir, port),
        name="quizforge-server",
        daemon=True,
    )
    thread.start()
    return f"thread started, data={data_dir}"
