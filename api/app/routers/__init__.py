"""路由注册表。

刻意用显式列表而不是自动扫描：漏注册一个路由是很容易发现的问题，
但被自动扫描悄悄吞掉一个因导入错误而失败的路由则会很难查。
新增模块时在这里登记一行。
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    ai,
    bank,
    chat,
    graph,
    health,
    knowledge,
    material,
    mybank,
    obsidian,
    plans,
    problem,
    progress,
    schedule_ops,
    settings_api,
    sync_api,
)


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
        # 笔记：只给 LLM 工具用（走 Obsidian 桥接），这里只开一个"连上没有"的只读状态
        obsidian.router,
        # 整份设置（文件是权威，可拷贝）
        settings_api.router,
        # 资料：源目录只读，可写的只有元数据（`data/library/*.yaml`）与派生缓存；
        # 上传进来自动排进后台流水线（`app/jobs.py`）
        material.router,
        # 计划与日程：日程页的读写 + 对话凭条确认后落库（`proposalId` 是幂等键）
        plans.router,
        # 日程的操作面：健康（打卡 / 档位 / 精力曲线）与自动重排。
        # 与 `health.router`（`/api/health` 服务自检）是两件事，别名不同路径不冲突。
        schedule_ops.router,
        # Turso 同步：`shell.js` 早就在调 `/api/sync/status`，但这个路由一直没登记
        # （前端 catch 掉了，所以表现为"同步面板永远是空的"）。2026-11-08 补上。
        sync_api.router,
    ]


__all__ = ["all_routers"]
