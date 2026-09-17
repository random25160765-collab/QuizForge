"""把历史消息里"工具调用之前的那段铺垫"补上 `process` 标记。

## 为什么需要它

铺垫（"我先查一下…"）与结论（"查到了，是这样的…"）在库里长得一模一样，
都是 `text` 零件。区别只在**位置上**：铺垫紧跟着一次工具调用。

新消息现在由 `routers/chat.py` 落库时直接打标（所以刷新也不会散开）；
这个脚本是给**打标之前**存下来的那些补的 —— 一次性的，跑完即可，
但留在仓库里：同样的判断标准以后还会用到（比如导入旧数据）。

用法：
    python3 tools/backfill_process.py            # 只报告
    python3 tools/backfill_process.py --apply    # 真的写回去
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.db import get_session_factory  # noqa: E402
from app.models import Message  # noqa: E402


def mark(parts: list) -> int:
    """给零件序列打标，返回改了几处（判据与 `routers/chat.py` 完全一致）。"""
    changed = 0
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        if part.get("process") or not str(part.get("text") or "").strip():
            continue
        nxt = parts[index + 1] if index + 1 < len(parts) else None
        if isinstance(nxt, dict) and nxt.get("type") == "tool_call":
            part["process"] = True
            part["label"] = "过程"
            changed += 1
    return changed


def main() -> int:
    apply = "--apply" in sys.argv
    factory = get_session_factory()
    total = 0
    touched = 0
    with factory() as db:
        rows = db.scalars(select(Message).where(Message.role == "assistant")).all()
        for row in rows:
            parts = row.parts or []
            if not isinstance(parts, list):
                continue
            changed = mark(parts)
            if changed:
                total += changed
                touched += 1
                if apply:
                    flag_modified(row, "parts")
        if apply:
            db.commit()
    verb = "已回填" if apply else "需要回填"
    print(f"[INFO] {verb}：{total} 处铺垫（分布在 {touched} 条助手消息里）")
    if not apply and total:
        print("       加上 --apply 真正写回去")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
