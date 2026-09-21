"""把**资料库**的条目接进知识空间：**登记 + 切片，止步于此**。

## 为什么它比材料那条链短得多

材料（B）是**信任语料**：值得抽候选点、归知识点、归概念、出题、进图谱。
资料库（A）是**待查语料**：拿来读、拿来引用、拿来对照 —— 但"灌水论文我也要出成
题目做吗？"（用户原话）。

所以这条路**只做两件事**：把条目登记进 `materials`（这样切片 / 向量 / 检索
全都有一套现成代码可用），然后切片。**抽点 / 知识点 / 概念 / 题，一行都不写。**

## 但档位是**按件**的，不是按来源的

`materials.depth` 决定要不要继续往下走：`检索` 止步于此，`出题` 才进链。
**资料库里的东西也可以标成 `出题`**（值得精读的那几篇）—— 用户原话：
"有的资料值得出题，有的资料看看就好，同样都是资料管理器里的资料，
它们需要的工艺深度是完全不一样的"。所以是**一条链 + 一个开关**，
不是"两个库两条链"。

⚠️ 这条命令**只在新建时**把 `depth` 定成 `检索`；已经改成 `出题` 的
（人手工提上去的）**不许被它打回**。

## 关键的归一：`source_path` 指**抽取缓存**

原件千奇百怪（PDF / 网页 / 代码目录），没有统一的"位置"可回 —— 这点和材料不同，
材料本身就是一份带行号的纯文本。所以进 `materials` 时 `source_path` 一律指
**`.text/{citekey}.txt`**（能按行读的那份），原件路径留在 yaml 的 `source` 里
（只给人"打开原件"用）。

这样切片 / `read_lines` / "引用与前端同源"**全都照旧**，一条都不用改 ——
正是 `ingest` 那句"**入口归一，链上无感**"。

用法：

    python -m pipeline.intake                 # 全部可用条目
    python -m pipeline.intake --dry-run
    python -m pipeline.intake --citekey dao2022flashattentionfastme
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from sqlalchemy import delete, select

from . import config, ingest

sys.path.insert(0, str(config.ROOT / "api"))

from app import library as lib  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Material, MaterialSlice  # noqa: E402

#: 正文**可用**的判据。和 `lib.search` 用的是同一条（`library.py:1314` 那里
#: `state in ("ok", "poor")`）—— 不自造一套更严的，否则同一条资料在搜索框里
#: 看得见、在向量里看不见，那种不一致最难查。
USABLE_STATES = ("ok", "poor")


def _meta_dir() -> Path:
    return Path(get_settings().data_dir) / "library"


def _text_dir() -> Path:
    return _meta_dir() / lib.TEXT_DIR


def _subject_of(meta: dict) -> str:
    """从原件路径取学科（`/mnt/f/Documents/cuda/x.pdf` → `cuda`）。

    取 `Documents` 之下的**第一级目录** —— 资料根的约定就是"一个学科一个目录"
    （实测 59 条正好落在 swe / cuda / nvidia / stm32 / riscv / ic / python /
    amd / toolchain / linux / other / math）。取不到就退回 `library`，**不猜**。
    """
    parts = [
        part
        for part in str(meta.get("source") or "").replace("\\", "/").split("/")
        if part
    ]
    for index, part in enumerate(parts):
        if part.lower() == "documents" and index + 1 < len(parts):
            return parts[index + 1]
    return "library"


def _upsert(db, citekey: str, meta: dict, cache: Path, lines: list[str]) -> Material:  # noqa: ANN001
    """登记进 `materials`（新建时才定 `depth`）。"""
    row = db.scalars(select(Material).where(Material.slug == citekey)).first()
    if row is None:
        # **depth 只在新建时定**：已经改成 `出题` 的（人手工提上去的）不许被这条命令打回
        row = Material(slug=citekey, depth="检索")
        db.add(row)
    row.subject = _subject_of(meta)
    row.title = str(meta.get("title") or citekey)
    # 指**抽取缓存**而不是原件 —— 见模块 docstring 那条"关键的归一"
    row.source_path = str(cache)
    row.sha256 = hashlib.sha256(cache.read_bytes()).hexdigest()
    row.lines = len(lines)
    db.flush()
    return row


def _replace_slices(db, material: Material, lines: list[str]) -> int:  # noqa: ANN001
    """切段。**复用 `ingest.split_slices`** —— 入口归一，链上无感。

    PDF 抽出来的文本没有 Markdown 标题，`split_slices` 会退化成"按长度切"
    （`MAX_LINES` / `SPLIT_AT` 那两条兜底），这正是想要的：**格式的差异止步于入口**。
    """
    cuts = ingest.split_slices(lines)
    db.execute(delete(MaterialSlice).where(MaterialSlice.material_id == material.id))
    for index, (start, end, title) in enumerate(cuts, start=1):
        db.add(
            MaterialSlice(
                material_id=material.id,
                slice_id=f"sl-{index:03d}",
                path=[title] if title else [],
                start_line=start,
                end_line=end,
                tokens="",
                figures=[],
                summary=title,
            )
        )
    db.flush()
    return len(cuts)


def run(args) -> int:  # noqa: ANN001
    meta_dir, text_dir = _meta_dir(), _text_dir()
    all_meta = lib.load_all_metadata(meta_dir)
    if not all_meta:
        print(f"资料库里没有条目：{meta_dir}")
        return 1
    if args.citekey:
        all_meta = {k: v for k, v in all_meta.items() if k == args.citekey}
        if not all_meta:
            print(f"没有这个引用键：{args.citekey}")
            return 1

    db = get_session_factory()()
    try:
        skipped_no_cache: list[str] = []
        skipped_unusable: list[str] = []
        done: list[tuple[str, int, bool]] = []

        for citekey in sorted(all_meta):
            state = lib.text_state(text_dir, citekey)
            cache = text_dir / f"{citekey}.txt"
            if not cache.is_file():
                skipped_no_cache.append(citekey)
                continue
            if state.get("state") not in USABLE_STATES:
                skipped_unusable.append(f"{citekey}({state.get('state')})")
                continue

            lines = cache.read_text(encoding="utf-8", errors="replace").splitlines()
            digest = hashlib.sha256(cache.read_bytes()).hexdigest()
            row = db.scalars(select(Material).where(Material.slug == citekey)).first()
            fresh = (
                row is not None
                and row.sha256 == digest
                and db.scalars(
                    select(MaterialSlice.id).where(MaterialSlice.material_id == row.id).limit(1)
                ).first()
                is not None
            )
            if fresh and not args.rebuild:
                done.append((citekey, 0, True))
                continue
            if args.dry_run:
                cuts = ingest.split_slices(lines)
                done.append((citekey, len(cuts), False))
                continue

            material = _upsert(db, citekey, all_meta[citekey], cache, lines)
            count = _replace_slices(db, material, lines)
            db.commit()
            done.append((citekey, count, False))

        new = [one for one in done if not one[2]]
        print(
            f"资料库 {len(all_meta)} 条：登记/切片 {len(new)}"
            f"、已是最新跳过 {len(done) - len(new)}"
            + (f"、**没有正文缓存 {len(skipped_no_cache)}**" if skipped_no_cache else "")
            + (f"、**抽不干净跳过 {len(skipped_unusable)}**" if skipped_unusable else "")
        )
        for citekey, count, fresh in done[:12]:
            print(f"   {'·' if fresh else '+'} {citekey:44s} {'（已最新）' if fresh else f'{count} 段'}")
        if len(done) > 12:
            print(f"   …… 还有 {len(done) - 12} 条")
        if skipped_no_cache:
            print("   没有缓存：" + "、".join(skipped_no_cache[:6]))
        if skipped_unusable:
            print("   抽不干净：" + "、".join(skipped_unusable[:6]))
        if not args.dry_run and new:
            print("\n下一步（**只到向量为止，不进出了题链**）：")
            print("   make embed")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="资料库条目登记 + 切片（止步于此，不进出了题链）")
    parser.add_argument("--citekey", default="", help="只做这一条")
    parser.add_argument("--dry-run", action="store_true", help="只说要做什么，不动库")
    parser.add_argument("--rebuild", action="store_true", help="整批重切（缓存没变也重来）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
