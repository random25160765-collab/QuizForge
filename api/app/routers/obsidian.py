"""Obsidian 桥接的只读状态：设置页那颗「检测连接」调它。

这里只做一件事：把 `vault.py` 的健康检查翻成一句话。**不提供读笔记的接口** ——
笔记只给 LLM 工具用（见 `app/vault.py` 的文件头）。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import vault
from ..deps import DbSession

router = APIRouter(prefix="/api/obsidian", tags=["obsidian"])


@router.get("/status")
def status(db: DbSession, path: str = "") -> dict:
    """`path` 是"还没保存的那份"：设置页刚打完 vault 路径就点检测，不该等同步落库。"""
    vault.configure(db)
    try:
        info = vault.ping(path)
        return {
            "ok": True,
            "vault": str(info.get("vaultName") or ""),
            "pluginVersion": str(info.get("pluginVersion") or ""),
            "capabilities": list(info.get("capabilities") or []),
        }
    except Exception as exc:  # noqa: BLE001 - 连不上就是连不上，原样告诉设置页
        return {"ok": False, "error": str(exc)}
