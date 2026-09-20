"""工具挂载开关：哪几组工具这一版亮着。

用户要的是顶栏几个图标：**亮起即挂载、熄灭即独立、全部熄灭即极简模式**。

三条边界：

* 落 `settings_store`（单用户下的单行设置，与资料根目录同一个 JSON 列）——
  **没配过就是"全都挂上"**，而不是"一个都没有"：升级上来的用户不该突然发现 AI 变哑了。
* 空列表是**明确**的"极简模式"，与"没配过"是两件事（这与文本缓存里
  `at` 与 `state` 的区分是同一个道理）。
* 分组本身只在 `tools.GROUPS` 里定义一处，这里只做读写与校验，不另立一份。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import settings_store, tools

#: 设置里存挂载集的键（自定义：只存"哪些组"）。
KEY = "tool_mounts"

#: 存模式的键。有它就以模式为准，`KEY` 只在自定义模式下用。
KEY_MODE = "tool_mode"

#: **三个模式 = "组 × 权限档"**，软件内部的那份简单 MCP 清单。
#:
#: 名字 / 说明 / 挂哪几组 / 放到哪一档。模式是**唯一**的用户可见单位 ——
#: 组和档是它的零件，界面只让他选模式（用户的原话："工具和访问权限是模式的
#: 底层元素，不同的组合构成不同的模式"）。
#: 判定顺序见 `mode_of()`：配过模式 → 模式；配过组 → 自定义（老数据）；
#: 都没配过 → 学习（升级上来的用户不该突然发现 AI 变哑了）。
@dataclass(frozen=True)
class Mode:
    """一个模式：名字、说明、挂哪几组、放到哪一档、以及**它的颜色**。

    `color` 是这套东西里**唯一**的颜色来源（用户："注意不要硬编码颜色 —— 未来如果
    我想改颜色呢？那应该是一键式的"）。前端把它注入成 `--mode-<key>` 这个 CSS 变量，
    CSS 里只写 `var(--mode-*)`，一处都不写色号。想换颜色就改这里；
    想不碰代码就写设置里的 `mode_colors`（见 `colors()`）—— 将来做个取色器，
    它只需要往那个键里写一个十六进制。
    """

    key: str
    label: str
    hint: str
    groups: tuple[str, ...]
    access: tuple[str, ...]
    #: 一个值要在**深浅两套主题**下都能当字色看（不用挑主题，也就不用两套色号）。
    color: str = "#8B98A6"


MODES: tuple[Mode, ...] = (
    Mode(
        key="minimal",
        label="极简",
        hint="不挂任何工具，只聊天",
        groups=(),
        access=("read",),
        color="#8B98A6",  # 中性灰：什么都不挂
    ),
    Mode(
        key="query",
        label="查询",
        hint="只查不改：翻笔记、读资料原文、走知识图谱",
        groups=("notes", "library", "graph"),
        access=("read",),
        color="#3B82F6",  # 蓝：翻找/查看
    ),
    Mode(
        key="study",
        label="学习",
        hint="全功能：外加题库、推题与批改、沙箱演示",
        groups=tuple(tools.ALL_GROUPS),
        access=tuple(tools.ACCESS_LEVELS),
        color="#12A594",  # 青（品牌那一档）：全功能
    ),
)

MODE_KEYS: tuple[str, ...] = tuple(one.key for one in MODES)
MODE_BY_KEY: dict[str, Mode] = {one.key: one for one in MODES}
MODE_LABELS: dict[str, str] = {one.key: one.label for one in MODES}

#: 设置里可以覆盖颜色的键：`{"study": "#12A594", ...}`。
COLORS_KEY = "mode_colors"


def colors(db: Any = None) -> dict[str, str]:
    """三个模式的颜色（**唯一来源**在这里，设置里的覆盖优先）。

    `db` 给 `None` 时只回默认值 —— 调用方一般在有库的时候传 `db` 进来。
    """
    found = {one.key: one.color for one in MODES}
    if db is None:
        return found
    stored = settings_store.load(db).get(COLORS_KEY)
    if isinstance(stored, dict):
        for key, value in stored.items():
            name = str(key)
            if name in found and isinstance(value, str) and value.strip():
                found[name] = value.strip()[:32]
    return found


def read(db: Any) -> set[str] | None:
    """当前挂载了哪几组。`None` 表示**没配过**（等价于全都挂上）。"""
    data = settings_store.load(db)
    if KEY not in data:
        return None
    stored = data.get(KEY)
    if not isinstance(stored, list):
        return None
    known = [str(part) for part in stored if str(part) in tools.GROUP_LABELS]
    return set(known)


def read_mode(db: Any) -> str | None:
    """配过的模式 key。`None` = 没配过。"""
    key = str(settings_store.load(db).get(KEY_MODE) or "")
    return key if key in MODE_LABELS else None


def mode_of(db: Any) -> str:
    """当前生效的模式 key。

    * 配过模式 → 那个模式；
    * 只配过组（老数据 / 以后的自定义）→ `"custom"`；
    * 都没配过 → `"study"`（**没配过 = 全都挂上**，与 `read()` 的语义一致：
      升级上来的用户不该突然发现 AI 变哑了）。
    """
    key = read_mode(db)
    if key:
        return key
    current = read(db)
    if current is None:
        return "study"
    # 老数据（只存过"哪几组"，没存模式）：能对上一个模式的**认成那个模式** ——
    # 否则用户之前那套配置在界面上会显示成"什么都没选中"，看着像坏了。
    # 对不上任何模式才是真正的自定义。
    for one in MODES:
        if sorted(one.groups) == sorted(current):
            return one.key
    return "custom"


def effective(db: Any) -> set[str]:
    """实际生效的挂载集（把"没配过"翻成"全都挂上"）。"""
    key = mode_of(db)
    if key in MODE_BY_KEY:
        return set(MODE_BY_KEY[key].groups)
    current = read(db)
    return set(tools.ALL_GROUPS) if current is None else current


def access_of(db: Any) -> tuple[str, ...]:
    """当前允许的权限档。模式自带；自定义与没配过 = 四档全放（老行为不变）。"""
    key = mode_of(db)
    if key in MODE_BY_KEY:
        return tuple(MODE_BY_KEY[key].access)
    return tuple(tools.ACCESS_LEVELS)


def mode_of_groups(groups: Any) -> str:
    """哪几组 → 哪个模式。对不上任何预设就是 `"custom"`（老配置 / 手动组合）。"""
    want = sorted(str(one) for one in (groups or ()))
    for one in MODES:
        if sorted(one.groups) == want:
            return one.key
    return "custom"


def from_snapshot(raw: Any) -> tuple[set[str], tuple[str, ...]] | None:
    """把消息上记的那份快照还原成"这一轮用哪几组、放到哪一档"。

    为什么由**输入**来决定（用户定的规则）：

    > 一条输入可以有多个输出，输入锚定模式，因此所有的输出都是一个模式，
    > 要想改模式，必须更改输入。

    所以生成时读的是那条用户消息当时记下的快照，而不是"现在设置里是什么" ——
    否则同一句话的两个输出可能落在两个模式下，轨迹就没法解释了。

    记的是**组名列表**（`Message.mounts` 里的一个 JSON 数组）。认不出任何预设
    （自定义）就四档全放，与 `access_of` 对自定义的处理一致。老消息没记过 → `None`。
    """
    if not raw:
        return None
    try:
        groups = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(groups, list):
        return None
    names = {str(one) for one in groups}
    key = mode_of_groups(names)
    if key in MODE_BY_KEY:
        one = MODE_BY_KEY[key]
        return set(one.groups), tuple(one.access)
    return names, tuple(tools.ACCESS_LEVELS)


def write_mode(db: Any, key: str) -> str:
    """把模式钉在设置里。**顺手清掉自定义的组**：两者同时存在会说不清谁算数。"""
    name = str(key or "").strip()
    if name not in MODE_LABELS:
        raise ValueError(f"没有这个模式：{name}")
    settings_store.put(db, **{KEY_MODE: name, KEY: None})
    return name


def write(db: Any, groups: list[str]) -> set[str]:
    """存一份挂载集。**不认识的组名一律拒掉** —— 静默丢弃会让人以为开关坏了。"""
    cleaned: list[str] = []
    for part in groups:
        key = str(part).strip()
        if not key:
            continue
        if key not in tools.GROUP_LABELS:
            raise ValueError(f"没有这一组工具：{key}")
        if key not in cleaned:
            cleaned.append(key)
    settings_store.put(db, **{KEY: cleaned})
    return set(cleaned)


def describe(db: Any) -> dict:
    """给界面的一份说明：五组各是什么、亮着没有、这一版实际声明了哪些工具。

    `declared` 是**从 `specs()` 真算出来的**，不是另抄一份 —— 顶栏亮着的图标
    与"模型到底看得到几个工具"必须能对得上，而这是唯一能证明它俩一致的办法。
    """
    mounted = effective(db)
    allow = access_of(db)
    # 三个颜色：**唯一的来源**（`Mode.color`，或设置里的 `mode_colors` 覆盖）。
    # 只在这里算一次，下面 modes 里逐条引用它。
    palette = colors(db)
    declared = [
        item["function"]["name"]
        for item in tools.specs(mounted, allow)
    ]
    mode = mode_of(db)
    return {
        "mode": mode,
        "modeLabel": MODE_LABELS.get(mode, "自定义"),
        "access": list(allow),
        "modes": [
            {
                "key": one.key,
                "label": one.label,
                "hint": one.hint,
                "groups": list(one.groups),
                "access": list(one.access),
                # **颜色随模式一起发出去**：前端只把它注入成 CSS 变量，自己不存色号。
                "color": palette.get(one.key, one.color),
                "tools": len(tools.specs(set(one.groups), one.access)) if one.groups else 0,
                "on": one.key == mode,
            }
            for one in MODES
        ],
        # 一份单独的色表：想一次改三个颜色（或将来做取色器）只认这一处
        "colors": palette,
        "groups": [
            {
                "key": key,
                "label": label,
                "hint": hint,
                "mounted": key in mounted,
                "tools": [name for name in tools.REGISTRY if tools.group_of(name) == key],
            }
            for key, label, hint in tools.GROUPS
        ],
        "mounted": [key for key, _, _ in tools.GROUPS if key in mounted],
        "declared": declared,
        "configured": read(db) is not None,
        "minimal": not mounted,
    }


__all__ = [
    "COLORS_KEY", "KEY", "KEY_MODE", "MODES", "MODE_BY_KEY", "MODE_KEYS", "MODE_LABELS",
    "Mode", "access_of", "colors", "describe", "effective", "from_snapshot",
    "mode_of", "mode_of_groups", "read", "read_mode", "write", "write_mode",
]
