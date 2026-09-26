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
import multiprocessing
import sys
from pathlib import Path

from sqlalchemy import delete, select

from . import config, ingest

sys.path.insert(0, str(config.ROOT / "api"))

from app import library as lib  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import Material, MaterialSlice, SliceEmbedding  # noqa: E402

#: 正文**可用**的判据。和 `lib.search` 用的是同一条（`library.py:1314` 那里
#: `state in ("ok", "poor")`）—— 不自造一套更严的，否则同一条资料在搜索框里
#: 看得见、在向量里看不见，那种不一致最难查。
USABLE_STATES = ("ok", "poor")


def _meta_dir() -> Path:
    return Path(get_settings().data_dir) / "library"


def _text_dir() -> Path:
    return _meta_dir() / lib.TEXT_DIR


def _subject_of(meta: dict) -> str:
    """从原件路径取学科（`<资料根>/cuda/x.pdf` → `cuda`）。

    实现按"路径里有一段叫 `documents`"来认资料根（`/mnt/f/Documents`、
    `F:\\Documents` 都命中），取它的**下一级目录**当学科 —— 资料根的约定就是
    "一个学科一个目录"（实测 59 条正好落在 swe / cuda / nvidia / stm32 / riscv /
    ic / python / amd / toolchain / linux / other / math）。取不到就退回
    `library`，**不猜**。

    ⚠️ 这个启发式**绑着目录名**（已记进 `docs/STATUS.md` 的未决事项）：
    资料放在不叫 `Documents` 的根下时，会全部退回 `library`。要根治，
    得让"资料根"成为一个配置项，而不是从路径里猜一段。
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


def _subject_of_meta(meta: dict) -> str:
    """学科：**材料自己写了就用它**，没写才按路径猜（`_subject_of`）。

    加这一档是因为"一键补充向量"要收的常常是库外目录（`/mnt/f/Books` 这种），
    路径里没有 `documents` 那一段 —— 照旧猜的话，收多少份都会挤进 `library` 一个兜底档。
    """
    given = str(meta.get("subject") or "").strip()
    return given or _subject_of(meta)


def _upsert(db, citekey: str, meta: dict, cache: Path, lines: list[str]) -> Material:  # noqa: ANN001
    """登记进 `materials`（新建时才定 `depth`）。"""
    row = db.scalars(select(Material).where(Material.slug == citekey)).first()
    if row is None:
        # **depth 只在新建时定**：已经改成 `出题` 的（人手工提上去的）不许被这条命令打回
        row = Material(slug=citekey, depth="检索")
        db.add(row)
    row.subject = _subject_of_meta(meta)
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
    # **先删这一片上的向量，再删切片。** `slice_embeddings.slice_id` 有外键指过来，
    # 顺序反了数据库会当场拦下（实测 `FOREIGN KEY constraint failed`）。删掉是对的：
    # 行区间变了，旧向量对应的是**旧正文**，留着就是错的 —— 新切片会按新指纹重算。
    #
    # 这个坑以前撞不到，因为 intake 只在"还没有向量"的新材料上跑过；`make add`
    # （文件被改过）与 `make images`（图的说明写回正文）都会重切**已入库**的材料。
    db.execute(
        delete(SliceEmbedding).where(
            SliceEmbedding.slice_id.in_(
                select(MaterialSlice.id).where(MaterialSlice.material_id == material.id)
            )
        )
    )
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


def _extract_job(payload: tuple[str, float, str, str, int, str, str]) -> tuple[dict, str]:
    """子进程里干的事：抽一份正文。**纯文件进、纯数据出，不碰数据库。**

    为什么单独拎成模块级函数：进程池要能 pickle 它，而且它必须不依赖调用方的任何
    状态（`args`、DB 会话都不能带进去 —— 带着 DB 会话 fork 出去最危险）。
    `(path, mtime, rel, root, size, text_dir, citekey)` 就是它要的全部东西；
    返回 `(抽出的状态, 错误文本)`，失败不抛：单份坏文件不该毁掉整批。
    """
    path, mtime, rel, root, size, text_dir, citekey = payload
    item = lib.Item(
        path=Path(path), root=Path(root), rel=rel, size=int(size), mtime=float(mtime)
    )
    try:
        return lib.extract_text(item, Path(text_dir), citekey), ""
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def _collect(args, meta_dir: Path, text_dir: Path) -> int:  # noqa: ANN001
    """把 `--from` 那些目录里的**新文件**收进来：抽正文 + 冻结元数据。

    这是"往 Windows 那边一放、打一条命令就能在前端查到"里的**前半截**（后半截是
    `make embed` 算向量）。

    **新文件的判据只有一个：这份正文抽过没有**（缓存里的 `at` 标记）—— 与资料库页
    那条"建索引"（`routers/library.py` 的 `/index`）用同一条，理由是同一个：拿
    "元数据冻结没有"判的话，手工补过元数据的条目会**永远不抽正文**（那里踩过）。
    拿"文件新不新"判也不行：文件可以是老文件、只是刚放进这个目录。

    元数据的标题先用**文件名**（`clean_title`），不问模型 —— 这条命令要能离线、要快，
    而且模型判标题是**花钱**的（`--judge` 那种事留给资料库页的"建索引"，它在做）。
    """
    total = collected = skipped = failed = 0
    for source in args.source:
        root = Path(source).expanduser()
        if not root.is_dir():
            print(f"  ! 不是目录（新文件请放进一个目录再指过来）：{root}")
            failed += 1
            continue
        entries = [
            entry
            for entry in lib.entries([root], meta_dir, text_dir)
            # `entries` 还会把它认得的所有"只有正文"的条目一并返回（那是给资料库列表用的），
            # 这里只要**这个目录底下**的 —— 不然会把库外的旧条目再算一遍。
            if Path(entry.item.path).is_relative_to(root)
        ]
        fresh = [entry for entry in entries if not entry.text_state.get("at") or args.force]
        print(f"看 {root}：{len(entries)} 个文件，其中没抽过正文的 {len(fresh)} 个")
        fresh_set = {id(entry) for entry in fresh}
        if args.dry_run:
            for entry in entries:
                total += 1
                if id(entry) in fresh_set:
                    print(f"  + {entry.item.rel}")
                    collected += 1
                else:
                    skipped += 1
            continue

        # **抽正文并行做。** 这是这条链里唯一单进程 CPU 密集的活 —— 网页那条尤其：
        # `htmlmd` 是纯 Python 的 DOM 解析，实测一个 11MB 的公众号页面在 16 个核的
        # 机器上只用一个核、跑好几分钟（用户看到的"CPU 在 90%、GPU 没动"就是它）。
        # 它又恰好是纯函数式的（给路径、写缓存、回状态，**不碰数据库**），所以放进
        # 进程池；写库与冻结元数据留在主进程，避免并发写。
        payloads = [
            (str(entry.item.path), entry.item.mtime, entry.item.rel, str(entry.item.root),
             entry.item.size, str(text_dir), entry.citekey)
            for entry in fresh
        ]
        outcomes: list[tuple[dict, str]]
        if args.workers > 1 and len(fresh) > 1:
            with multiprocessing.Pool(processes=min(args.workers, len(fresh))) as pool:
                outcomes = pool.map(_extract_job, payloads)
        else:
            outcomes = [_extract_job(one) for one in payloads]

        for entry in entries:
            total += 1
            if id(entry) not in fresh_set:
                skipped += 1
        for entry, (state, err) in zip(fresh, outcomes):
            item, key = entry.item, entry.citekey
            print(f"  + {item.rel}")
            if err:
                # 单份坏文件不该毁掉整批：报出来、记一笔、继续
                print(f"    ! 抽正文失败：{err}")
                failed += 1
                continue
            if not entry.frozen:
                # 冻结 = 定键 + 去重（撞键时它会挪开，两条不会共用一把键）
                meta = {
                    "citekey": key,
                    # `_pretty`（文件名 → 可读标题）是私有名，但资料库页那条"建索引"
                    # 同样用它当兜底标题（`routers/library.py`），这里跟着用，不另造一套。
                    "title": lib.clean_title(lib._pretty(item.stem)),  # noqa: SLF001
                    "kind": item.kind,
                    "source": str(item.path),
                    "origin": "filename",
                }
                if args.subject:
                    meta["subject"] = args.subject
                lib.freeze(meta_dir, text_dir, meta, item.rel, origin="filename")
            collected += 1
            print(f"    正文 {state.get('state')} · {state.get('chars')} 字")
    print(
        f"收下 {collected} 个新文件（跳过已抽过的 {skipped} 个）"
        + (f"、**失败 {failed} 个**" if failed else "")
    )
    return 1 if failed and not collected else 0


def run(args) -> int:  # noqa: ANN001
    meta_dir, text_dir = _meta_dir(), _text_dir()
    # **先收新文件**，再读元数据 —— 顺序反了的话，这一轮刚冻结的那几份要等下一轮才登记。
    if getattr(args, "source", None) and _collect(args, meta_dir, text_dir) != 0 and not args.dry_run:
        return 1

    db = get_session_factory()()
    try:
        all_meta = lib.load_all_metadata(meta_dir)
        # **`data/library/*.yaml` 不是唯一入口**：网站抓的那批与抽出来的书是直接进库的
        # （从来没生成过 yaml），而权威在**库** —— 所以缺元数据的，按库里那一行补一条
        # 最小元数据。不补的话症状很隐蔽：`make images` 把图的说明写回了正文，却没人
        # 重切它们，于是"改了正文但检索没变"。
        for row in db.scalars(select(Material).order_by(Material.slug)):
            if row.slug in all_meta:
                continue
            all_meta[row.slug] = {
                "citekey": row.slug,
                "title": row.title,
                "source": str(row.source_path or ""),
                "subject": row.subject or "",
                "origin": "db",
            }
        if not all_meta:
            print(f"资料库里没有条目：{meta_dir}")
            return 1
        if args.citekey:
            all_meta = {k: v for k, v in all_meta.items() if k == args.citekey}
            if not all_meta:
                print(f"没有这个引用键：{args.citekey}")
                return 1

        skipped_no_cache: list[str] = []
        skipped_unusable: list[str] = []
        done: list[tuple[str, int, bool]] = []

        for citekey in sorted(all_meta):
            state = lib.text_state(text_dir, citekey)
            # 正文两种命名都要认（见 `_text_variants`）：只认 `.txt` 的话，走 `.md`
            # 那条的网站页与书会被判成"没有正文缓存"。
            cache = next(
                (path for path in lib._text_variants(text_dir, citekey)[0] if path.is_file()),
                text_dir / f"{citekey}.txt",
            )
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
    parser.add_argument("--from", dest="source", action="append", default=[], metavar="DIR",
                        help="先从哪个目录收新文件（可给多次）；收完顺带登记 + 切片")
    parser.add_argument("--subject", default="", help="新条目归到哪个学科（默认按原件路径猜）")
    parser.add_argument("--force", action="store_true", help="正文抽过也重抽")
    parser.add_argument("--workers", type=int, default=4,
                        help="抽正文的并行度（默认 4；`1` = 串行，方便看 traceback）")
    parser.add_argument("--citekey", default="", help="只做这一条")
    parser.add_argument("--dry-run", action="store_true", help="只说要做什么，不动库")
    parser.add_argument("--rebuild", action="store_true", help="整批重切（缓存没变也重来）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
