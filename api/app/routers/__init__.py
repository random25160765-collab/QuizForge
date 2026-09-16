"""路由注册表。

刻意用显式列表而不是自动扫描：漏注册一个路由是很容易发现的问题，
但被自动扫描悄悄吞掉一个因导入错误而失败的路由则会很难查。
新增模块时在这里登记一行。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import ai, auth, bank, health, knowledge, progress


def all_routers() -> list[APIRouter]:
    return [
        health.router,
        auth.router,
        bank.router,
        knowledge.router,
        progress.router,
        ai.router,
    ]


__all__ = ["all_routers"]
