"""路由注册表。

刻意用显式列表而不是自动扫描：漏注册一个路由是很容易发现的问题，
但被自动扫描悄悄吞掉一个因导入错误而失败的路由则会很难查。
新增模块时在这里登记一行。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import ai, bank, chat, graph, health, knowledge, library, mybank, notes, problem, progress


def all_routers() -> list[APIRouter]:
    return [
        health.router,
        # 账号面（auth）已随"单用户本地"整块删除 —— 见 `app/deps.py` 的文件注释
        bank.router,
        knowledge.router,
        graph.router,
        progress.router,
        ai.router,
        chat.router,
        problem.router,
        mybank.router,
        # 笔记：权威是文件（`data/notes/`），不碰数据库
        notes.router,
        # 资料：源目录只读，可写的只有元数据（`data/library/*.yaml`）与派生缓存
        library.router,
    ]


__all__ = ["all_routers"]
