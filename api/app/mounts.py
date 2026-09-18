"""工具挂载开关：哪几组工具这一版亮着。

用户要的是顶栏几个图标：**亮起即挂载、熄灭即独立、全部熄灭即极简模式**。

三条边界：

* 落 `user_settings`（单用户下的单行设置，与资料根目录同一个 JSON 列）——
  **没配过就是"全都挂上"**，而不是"一个都没有"：升级上来的用户不该突然发现 AI 变哑了。
* 空列表是**明确**的"极简模式"，与"没配过"是两件事（这与文本缓存里
  `at` 与 `state` 的区分是同一个道理）。
* 分组本身只在 `tools.GROUPS` 里定义一处，这里只做读写与校验，不另立一份。
"""

from __future__ import annotations

from typing import Any

from . import tools
from .models import UserSettings

#: 设置里存挂载集的键。
KEY = "tool_mounts"


def read(db: Any, user_id: Any) -> set[str] | None:
    """当前挂载了哪几组。`None` 表示**没配过**（等价于全都挂上）。"""
    row = db.get(UserSettings, user_id)
    data = getattr(row, "data", None)
    if not isinstance(data, dict) or KEY not in data:
        return None
    stored = data.get(KEY)
    if not isinstance(stored, list):
        return None
    known = [str(part) for part in stored if str(part) in tools.GROUP_LABELS]
    return set(known)


def effective(db: Any, user_id: Any) -> set[str]:
    """实际生效的挂载集（把"没配过"翻成"全都挂上"）。"""
    current = read(db, user_id)
    return set(tools.ALL_GROUPS) if current is None else current


def write(db: Any, user_id: Any, groups: list[str]) -> set[str]:
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
    row = db.get(UserSettings, user_id)
    data = dict(getattr(row, "data", None) or {}) if row else {}
    data[KEY] = cleaned
    if row:
        row.data = data
    else:
        db.add(UserSettings(user_id=user_id, data=data))
    db.commit()
    return set(cleaned)


def describe(db: Any, user_id: Any) -> dict:
    """给界面的一份说明：五组各是什么、亮着没有、这一版实际声明了哪些工具。

    `declared` 是**从 `specs()` 真算出来的**，不是另抄一份 —— 顶栏亮着的图标
    与"模型到底看得到几个工具"必须能对得上，而这是唯一能证明它俩一致的办法。
    """
    mounted = effective(db, user_id)
    declared = [
        item["function"]["name"]
        for item in tools.specs(mounted)
    ]
    return {
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
        "configured": read(db, user_id) is not None,
        "minimal": not mounted,
    }


__all__ = ["KEY", "describe", "effective", "read", "write"]
