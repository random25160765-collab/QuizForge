"""这台机器的设置：`app_settings` 表里唯一的那一行。

**为什么还要一层**：设置整份存一个 JSON（`AppSettings.data`），
读写的调用点很多（挂载开关、主题、选题篮……），让它们各写一次
`db.get(AppSettings, 1)` 既啰嗦、又容易忘 `create=True`（于是读到一个 None，
表现是"我的开关自己重置了"）。这一层自己解决"那一行在不在、要不要建"。

（2026-09-22 之前这里是 `user_settings` 表 + `get_local_user`：
取的是"本机唯一用户"名下那一行。用户没了，那层间接也没了。）
"""

from __future__ import annotations

from typing import Any

from .models import AppSettings
from .settings_file import FILE_ONLY

#: 单例那一行固定用它 —— 表本来就是个单行表，主键只是个占位。
SETTINGS_ID = 1

#: **只在本机、不落设置文件也不跨机同步的键**（与 `theme/runtime/store.js` 的
#: `LOCAL_ONLY` 同一份名单）。名单只在这里写一次，落盘 / 上云 / 读出来都走
#: `strip_local()`：
#:
#: * `chatRead` —— 每条会话读到哪儿（实测占设置文件七成）；
#: * `graph` —— 图谱面板位置与布局参数；
#: * `basket` —— 选题篮；
#: * `skills` / `lens` —— 滤镜挂载记录（2026-11-09 搬进 `lens_records` 表：
#:   它属于数据，随对话同步、进用户数据包，不再占设置文件）。
#:
#: `notes` / `places` 不在此列 —— `marks_of()` 要从设置行里读它们。
#: `onboarding` 是每台设备自己的状态（引导做没做过），不该跟着同步走。
LOCAL_ONLY = ("chatRead", "graph", "basket", "skills", "lens", "onboarding")


def strip_local(data: dict) -> dict:
    """去掉只在本机的状态键。"""
    return {key: value for key, value in data.items() if key not in LOCAL_ONLY}


def normalize(data: dict) -> dict:
    """读 / 写都过一遍：剔掉只在本机的状态键（含滤镜记录）与废键。

    旧版把 `lens` / `skills` 写在这里；它们现在归 `lens_records` 表
    （见 `app/lens.py` 的 `_migrate_from_settings`）。设置这一侧只做丢弃，
    真正的搬运由那张表在第一次读时完成。

    `ai.enabled` 是旧的「启用 AI」开关（2026-11-09 删除）：能不能用只看
    有没有填密钥，所以这个键从任何入口进来都丢掉。

    `FILE_ONLY`（同步口令）也在这里剔掉：它只该住在设置文件里。进了这一行，
    下一次同步就会把它当设置传上 Turso —— 端到端加密就形同虚设了。
    """
    out = strip_local(dict(data))
    for key in FILE_ONLY:
        out.pop(key, None)
    ai = out.get("ai")
    if isinstance(ai, dict):
        out["ai"] = {key: value for key, value in ai.items() if key != "enabled"}
    return out


def row(db: Any, *, create: bool = False) -> Any:
    """那唯一一行。`create=True` 时不存在就建出来。

    **文件优先**（2026-11-07）：`config/settings.local.json` 存在时，这一行的
    `data` 在内存里被文件内容覆盖 —— 于是"CLI/脚本改了文件、界面立刻看得见"，
    而不会被前端同步推回来的旧值按下去。文件不存在时才是数据库那份（首次启动）。
    """
    from . import settings_file  # noqa: PLC0415 - 避免循环导入

    found = db.get(AppSettings, SETTINGS_ID)
    if found is None and create:
        found = AppSettings(id=SETTINGS_ID, data={})
        db.add(found)
        db.commit()
    if found is not None:
        on_disk = settings_file.load()
        if on_disk:
            # 老文件里可能还躺着本机状态键（2026-11-09 之前什么都往里推）——
            # 读出来就滤掉，界面与同步都看不到它们。
            found.data = normalize(on_disk)
    return found


def load(db: Any) -> dict:
    """整份设置（读不到就是空 dict）。"""
    found = row(db)
    data = getattr(found, "data", None)
    return normalize(dict(data)) if isinstance(data, dict) else {}


def marks_of(conf: dict, cid: Any) -> dict:
    """某条会话里的**痕迹**：`{"notes": [...], "places": [...]}`（可含老的 `stations`）。

    痕迹就是用户自己划下的那些笔（前端的 `QF.store`，住在设置的 `notes` / `places`
    两个键里，见 `theme/runtime/store.js` 的 `addMark` / `addPlace`）：
    `notes` = 高亮 / 删除线 / 下划线 / **批注**，`places` = **书签** / 回溯。

    为什么要有这一层：痕迹与设置同住一行 JSON，但**每条自己带 `cid`** —— 取的时候
    必须按会话过滤，否则导出的分享页会把**别的对话**的圈点也画上来（渲染只按 `mid`
    找，见 `store.js` 的 `marksOf`；`mid` 是消息 id，跨会话不会撞，但把别条对话的
    记号塞进一个"发给别人"的文件里仍然是不对的）。

    **口径只此一处**：导出网页（`routers/chat.py`）与"他自己留下过什么记号"
    （`recursion.py`）都从这里取，谁也别自己写一遍 filter。
    """
    key = str(cid)
    data = conf if isinstance(conf, dict) else {}
    out: dict = {}
    for name in ("notes", "places", "stations"):
        rows = data.get(name)
        if not isinstance(rows, list):
            continue
        hit = [one for one in rows if isinstance(one, dict) and str(one.get("cid") or "") == key]
        if hit:
            out[name] = hit
    return out


def put(db: Any, **changes: Any) -> dict:
    """改几个键。`None` = 删掉这个键（"没配过"与"配成空"是两件事，见 mounts）。"""
    found = row(db, create=True)
    data = dict(getattr(found, "data", None) or {})
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    # 任何写路径（界面 / 同步 / 脚本）都别把这些键带进去；
    # 老调用点还在写 `skills=` 的，这里顺手改成 `lens`。
    data = normalize(data)
    found.data = data
    db.commit()
    # 文件是权威：改完一起写回去（写失败不该让这次请求失败 —— 数据库那份已经落了）
    try:
        from . import settings_file  # noqa: PLC0415

        settings_file.save(data)
    except OSError:
        pass
    return data


__all__ = ["LOCAL_ONLY", "load", "marks_of", "normalize", "put", "row", "strip_local"]
