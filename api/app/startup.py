"""启动检查：把"能不能用"拆成几条**看得见**的检查，供启动页显示。

## 为什么要有这一层

双击 exe 之后，用户看到的应该是一个**有进度、有话说**的启动页，而不是一个
黑终端（用户原话："双击之后会跳出来一个终端"）。而这个页面要有东西可显示 ——
所以把启动时真正在做的检查做成一份清单：

| 检查 | 失败意味着什么 |
|---|---|
| 数据目录 | 写不进去 = 什么都没法存（权限、磁盘满、路径被占） |
| 库与表 | 库结构不对 = 读得出、写不进（`messages.id` 那次就是这样） |
| 题库 | 读不到题 = 界面空着（但还能用） |
| 前端产物 | 页面拿不到 = 白屏（**接口却是好的**，最难查） |
| 密钥（内测/自带） | 没有 = 对话那一项用不了，别的照常 |
| 重型运行时 | 没就绪 = 跑 Python 那一项暂时不可用（会自己取） |

`fail` 会挡住"可以开始用"，`warn` 只提示 —— 因为后面几条都属于"少一个能力"，
不该把人拦在门外。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


#: 表结构体检只做一次：它在进程生命周期里不会变，而启动页会反复轮询
@lru_cache(maxsize=1)
def _schema_ok() -> tuple[bool, str]:
    from .db import _rowid_pk_problems, get_engine  # noqa: PLC0415

    try:
        with get_engine().connect() as conn:
            problems = _rowid_pk_problems(conn)
    except Exception as exc:  # noqa: BLE001 —— 连不上也算一种结论，别让启动页 500
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return (not problems), "；".join(problems)[:200]


def _data_dir_step() -> Step:
    from .config import get_settings  # noqa: PLC0415

    target = Path(get_settings().data_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return Step("data", "数据目录", FAIL, f"{target} 写不进去：{exc}")
    return Step("data", "数据目录", OK, str(target))


def _resource_step() -> Step:
    """**跟着包走的只读资源**：`pipeline/`、演示套件；外加"滤镜"那份用户数据的情况。

    这一条是拿一次"桌面包里少东西"补上的：`pipeline/` 不在 `api/` 下，而是仓库根的
    兄弟目录 —— 开发时永远找得到，打包后只有 `--add-data` 带进去的才在。缺了不会白屏，
    症状是**界面上少一块**（资料页的"抓正文"点不动），而接口一切都正常 ——
    最难往"打包漏了目录"上想。

    **滤镜不在这条判据里**：它是用户自己的数据（`<数据目录>/lens/*.md`），
    桌面包第一次启动**本来就是 0 条**（用户要的默认）。这里只把它报出来，方便看。
    """
    from . import lens as lens_lib  # noqa: PLC0415
    from .config import get_settings  # noqa: PLC0415

    problems: list[str] = []
    try:
        lenses = len(lens_lib.all_lenses())
    except Exception as exc:  # noqa: BLE001
        lenses = 0
        problems.append(f"滤镜目录读不出（{type(exc).__name__}）")

    # 资料页那两个按钮走的是**延迟导入**（`pipeline/` 不在 `api/` 下）。
    # 延迟导入的好处是不拖慢启动，坏处是"漏打包"要到点了才炸 —— 这里先试一次。
    for name in ("pipeline.htmlmd", "pipeline.doc_classify"):
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name} 导入不了（{type(exc).__name__}）→ 资料页对应按钮会报错")

    kit = Path(get_settings().web_dir) / "assets" / "demo-kit" / "qf-kit.js"
    if not kit.is_file():
        problems.append("演示套件不在 web/assets/demo-kit/ → 演示图打不开")

    if problems:
        return Step("assets", "只读资源", WARN, "；".join(problems))
    return Step("assets", "只读资源", OK, f"pipeline 可用 · 演示套件在 · 滤镜 {lenses} 条")


def checks() -> list[Step]:
    """跑一遍启动检查。**每次都重新算**（除了表结构那一条，它是进程内不会变的）。"""
    from .config import get_settings  # noqa: PLC0415
    from .db import get_engine  # noqa: PLC0415
    from .models import Question, Topic  # noqa: PLC0415

    settings = get_settings()
    steps: list[Step] = [_data_dir_step()]

    # 库与表结构
    ok, detail = _schema_ok()
    if not ok:
        steps.append(Step("database", "库与表", FAIL, detail or "库结构不对，写操作会失败"))
    else:
        try:
            with get_engine().connect() as conn:
                questions = int(conn.execute(
                    select(func.count()).select_from(Question).where(Question.retired_at.is_(None))
                ).scalar() or 0)
                topics = int(conn.execute(
                    select(func.count()).select_from(Topic).where(Topic.retired_at.is_(None))
                ).scalar() or 0)
        except Exception as exc:  # noqa: BLE001
            steps.append(Step("database", "库与表", FAIL, f"{type(exc).__name__}: {exc}"[:200]))
            questions = topics = 0
        else:
            steps.append(Step("database", "库与表", OK, f"{settings.database_url.rsplit('/', 1)[-1]}"))
            steps.append(
                Step(
                    "bank",
                    "题库",
                    OK if questions else WARN,
                    f"{questions} 题 · {topics} 个主题"
                    if questions
                    else "还是空的：首轮同步会从云端取（没配同步就去资料页导入）",
                )
            )

    # 前端产物：没进包的话**接口一切正常、页面白屏**，是最难查的一种
    index = Path(settings.web_dir) / "index.html"
    steps.append(
        Step(
            "frontend",
            "前端产物",
            OK if index.is_file() else FAIL,
            "" if index.is_file() else f"缺 {index}（先 make web）",
        )
    )

    steps.append(_resource_step())

    # 密钥：只看设置里那份（2026-11-07 起没有内测通道）
    try:
        from . import settings_store  # noqa: PLC0415
        from .db import get_session_factory  # noqa: PLC0415

        session = get_session_factory()()
        try:
            conf = (settings_store.load(session) or {}).get("ai") or {}
        finally:
            session.close()
        has_key = bool(str(conf.get("apiKey") or "").strip()) and bool(conf.get("enabled"))
    except Exception:  # noqa: BLE001
        has_key = False
    if not settings.ai_enabled:
        steps.append(Step("ai", "模型通道", WARN, "实例级开关关着"))
    elif has_key:
        steps.append(Step("ai", "模型通道", OK, "设置里已填密钥"))
    else:
        steps.append(Step("ai", "模型通道", WARN, "要在设置里填自己的密钥"))

    # 重型运行时（Pyodide）：没就绪只影响"跑 Python"，而且它会自己在后台取
    try:
        from . import heavy_deps  # noqa: PLC0415

        if heavy_deps.is_ready():
            steps.append(Step("heavy", "Python 运行时", OK, "已在本机缓存里"))
        else:
            steps.append(Step("heavy", "Python 运行时", WARN, "首次使用时取一次（约 76M），之后离线可用"))
    except Exception:  # noqa: BLE001
        steps.append(Step("heavy", "Python 运行时", WARN, "状态未知"))

    return steps


def report() -> dict:
    """给启动页用的整份报告（含进度）。"""
    steps = checks()
    done = sum(1 for step in steps if step.status in (OK, WARN, FAIL))
    return {
        "ok": all(step.status != FAIL for step in steps),
        "progress": round(done / len(steps), 3) if steps else 1.0,
        "steps": [step.as_dict() for step in steps],
    }
