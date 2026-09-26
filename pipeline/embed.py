"""切片向量化：把每个切片切成若干**窗口**，逐窗算向量写进 `slice_embeddings`。

## 为什么是窗口，不是切片

`docs/检索与向量化.md` §3.1 那条"粒度直接用切片"**对 embedding 是错的**，实测：

* 切片平均 **6979 字**、最长 **36846 字**；
* 模型的可用窗口是 **1024 token**（约 2000~3000 字）—— 一 片一条向量，
  意味着**只嵌进了开头一小段**；
* 于是「前言」那种短切片能被**完整**嵌入 → **完整赢过残缺** →
  所有中文问句都返回「前言」（实测：`环形缓冲区` 0.8536 命中前言，真命中在 0.7730）。

切成窗口之后同一个实验就对了（实测：`环形缓冲区` → `FlashDecode.md:100` 0.642，
赢过前言 0.615）。**窗口仍带自己的行区间** —— "出处只认行区间"那条照样不破，
而且比整片更精确。

## 模型与设备

* 模型是**本地**的 bge-m3 int8（`app/local_embed.py`），首次跑时下载约 600MB，
  之后离线可用、零边际成本。
* 设备固定 **CPU**：本机有 RTX 5060，但实测 GPU 比 CPU 还慢（每片 57ms vs 45ms，
  这个 int8 模型的算子没有 CUDA 核，ORT 每层来回搬张量 336 个 Memcpy）。

**它是派生产物，随时可以重跑**。判据是"这个切片所有窗口的正文哈希没变就跳过"；
`--rebuild` 是全量重算（换了模型就用它）。

用法：

    python -m pipeline.embed --fetch      # 只把模型取到本机（首启下载那条路）
    python -m pipeline.embed              # 增量：只算缺的与正文变了的
    python -m pipeline.embed --rebuild    # 全量重算
    python -m pipeline.embed --dry-run    # 只说要算什么（**不需要模型**）
    python -m pipeline.embed --material ViT-TTNN-vit_bh
    python -m pipeline.embed --limit 8    # 先拿 8 片的窗口试
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import delete, func, select

from . import config

sys.path.insert(0, str(config.ROOT / "api"))

from app import local_embed  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models import EmbedRun, Material, MaterialSlice, SliceEmbedding  # noqa: E402

#: 一次推理送多少窗。本地推理不按次计费，批量纯粹是为了把 CPU 吃满。
BATCH = 16

#: 一个窗口最多多少字。
#:
#: **这是精度选择，不是速度选择** —— 实测纠正过一次：
#: 单窗耗时大致**正比于 token 数**，所以"窗口减半"只是把同样多的 token 切成更多份，
#: 总时间几乎不变（多出来的是每窗的固定开销，**反而略慢**）。
#: 真正换来的是**更精确的行区间**：1200 字的窗口命中时，引用指的是更小的一段。
#:
#: （我在上一版里写过"注意力是 O(seq²)、所以窗口减半总成本减半"—— 实测把这个说法
#: 否了：这个尺寸下逐层的 `O(seq·d²)` 矩阵乘占大头，压根不走 O(seq²) 那一项。
#: 实测：2200 字 / 840ms 每窗，1200 字 / 515ms 每窗，而段数翻了近三倍。）
#:
#: 上界仍是模型的可用窗口（1024 token）：1200 字（英文约 300~400 token）留足余量、
#: **不截断** —— 截断正是"前言永远赢"那次翻车的根因。
WINDOW_CHARS = 1200
#: 相邻窗口重叠多少字：免得一句关键的话正好被切开、两边都不完整。
WINDOW_OVERLAP = 200


@lru_cache(maxsize=16)
def _source_lines(path: str) -> tuple[str, ...]:
    """材料正文。**按路径缓存**：一份材料常常有好几片，读四次文件是白读。"""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return tuple(handle.read().splitlines())


def _windows(slice_row: MaterialSlice, lines: tuple[str, ...]) -> list[tuple[int, int, int, str]]:  # noqa: ANN001
    """把一片切成若干窗口：`[(ordinal, start_line, end_line, text)]`。

    **按行切，不从行中间劈开** —— 行是切片的原子单位（材料是 Markdown，
    一行常常就是一整句或一条列表项）。所以"攒到超过 `WINDOW_CHARS` 就切"，
    实际长度会略有出入，这是刻意的。

    每一窗都带上切片的 `summary`（那一片的 `##` 标题）：检索时"三角掩码在哪一节"
    这种问法，标题里的词比正文里散落的词更能定位。
    """
    start = max(1, int(slice_row.start_line or 1))
    end = int(slice_row.end_line or start)
    if not lines or end < start:
        return []
    end = min(end, len(lines))
    title = str(slice_row.summary or "").strip()

    out: list[tuple[int, int, int, str]] = []
    cursor = start
    ordinal = 0
    while cursor <= end:
        begin = cursor
        chars = 0
        stop = cursor
        # 至少收一行（否则超长的单行会让这个循环原地打转）
        while stop <= end and (chars < WINDOW_CHARS or stop == begin):
            chars += len(lines[stop - 1]) + 1
            stop += 1
        last = min(end, stop - 1)
        body = "\n".join(lines[begin - 1 : last])
        ordinal += 1
        out.append((ordinal, begin, last, f"{title}\n{body}" if title else body))
        if last >= end:
            break
        # 下一窗从**重叠处**起：往前退 WINDOW_OVERLAP 字（按行对齐），
        # 但至少前进一行 —— 否则窗口边界不动，就成了死循环。
        back = 0
        cursor = last
        while cursor > begin and back < WINDOW_OVERLAP:
            cursor -= 1
            back += len(lines[cursor - 1]) + 1
        cursor = max(begin + 1, cursor)
    return out


def _slice_digest(windows: list[tuple[int, int, int, str]]) -> str:
    """整片的指纹：**任何一窗的正文变了，整片重算**。

    粒度故意给到"片"而不是"窗"：窗口是按正文长度动态切的，改一段字就会让
    后面的窗口边界全部平移 —— 逐窗比对哈希会得出"全都变了"，不如整片重算干净，
    而且**不会留下孤儿行**（重建时先删这一片的旧行，见 `_store`）。
    """
    payload = "|".join(text for _o, _s, _e, text in windows)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]


def _model_name() -> str:
    """写进 `slice_embeddings.model` 的名字。换模型**或换精度** = 整批重算，判据就是它。

    走 `local_embed.model_label()`：它会把 `QF_EMBED_ONNX` 指定的精度也拼进名字里 ——
    否则换了 int8/fp16 而名字不变，两种不可混用的向量会共用一把名字，检索照样出结果。
    """
    return local_embed.model_label()


def _pending(db, *, material: str, rebuild: bool, limit: int) -> tuple[list, dict]:  # noqa: ANN001
    """算出**这一次要向量化哪些切片**。返回 (待办, 统计)。

    待办里每一项是 `(切片, 材料, 窗口列表, 指纹)`。判据三条，缺一条就要重算：
      ① 还没算过（这一片一条向量都没有）；
      ② 指纹变了（正文被改过 / 切片范围被重切过）；
      ③ 存量是用**别的模型**算的（维度都对不上，留着就是错的）。
    """
    stmt = (
        select(MaterialSlice, Material)
        .join(Material, Material.id == MaterialSlice.material_id)
        .order_by(Material.slug, MaterialSlice.start_line)
    )
    if material:
        stmt = stmt.where(Material.slug == material)
    rows = db.execute(stmt).all()

    model = _model_name()
    # 每片已有的向量：`(指纹, 模型, 存了几行)`。
    #
    # 指纹与模型取一行就够（同片的行是一起写的、一定相同）；**行数却必须单独数** ——
    # 它是"这片刻完了没有"的唯一凭据，理由见下面那条 `previous[2] < len(windows)`。
    existing: dict[int, tuple[str, str, int]] = {}
    for row in db.execute(
        select(
            SliceEmbedding.slice_id,
            func.min(SliceEmbedding.text_hash),
            func.min(SliceEmbedding.model),
            func.count(),
        ).group_by(SliceEmbedding.slice_id)
    ).all():
        existing.setdefault(row[0], (str(row[1]), str(row[2]), int(row[3])))

    todo: list = []
    stats = {"slices": len(rows), "unchanged": 0, "missing_file": 0, "stale_model": 0,
             "partial": 0, "slices_to_do": 0, "windows": 0}
    for slice_row, mat in rows:
        try:
            lines = _source_lines(mat.source_path)
        except OSError:
            # 材料文件不在这台机器上（库只存坐标，正文在文件里）。
            # **不静默跳过**：这一片检索不到是事实，要报出来。
            stats["missing_file"] += 1
            continue
        windows = _windows(slice_row, lines)
        if not windows:
            continue
        digest = _slice_digest(windows)
        previous = existing.get(slice_row.id)
        if previous is not None and not rebuild:
            if previous[0] != digest:
                pass  # ② 正文变了
            elif previous[1] != model:
                stats["stale_model"] += 1  # ③ 换了模型
            elif previous[2] < len(windows):
                # ④ **存的行数不够** —— 跑了一半被打断的那种片。
                #
                # 这一条不能省：指纹（`text_hash`）说的是"这一片该有哪几窗"，而它在**第一窗**
                # 落库时就写进去了 —— 于是半片与整片长得一模一样，下一轮判成"没变"、跳过，
                # 缺的那几窗**永远补不回来**。实测：三本跑过一半的书（该 50 窗只存 1 窗、
                # 存的还是最后一窗），而 `--dry-run` 报的是"要算 0 片 / 没变跳过 445"。
                # 行数才是"刻完了没有"；指纹只说明"刻的是哪一份"。
                stats["partial"] += 1
            else:
                stats["unchanged"] += 1
                continue
        todo.append((slice_row, mat, windows, digest))
        stats["windows"] += len(windows)

    stats["slices_to_do"] = len(todo)
    if limit:
        todo = todo[:limit]
    return todo, stats


def _close_run(db, *, status: str, note: str = "", written: int = 0) -> None:  # noqa: ANN001
    """把这次跑单收尾（开跑登记见 `_run` 里那段）。

    用"找 pid 是自己、还挂着 `running` 的那一行"来收尾，而不是把 `run` 对象传遍全场：
    出错可能发生在登记**之前**（模型没下下来就直接走人了），那时根本没有对象可传，
    而库里也不该留下一条永远 `running` 的行 —— 那正是"分不清在跑还是死了"的来源。
    """
    try:
        row = (
            db.execute(
                select(EmbedRun)
                .where(EmbedRun.pid == os.getpid())
                .where(EmbedRun.status == "running")
                .order_by(EmbedRun.id.desc())
            )
            .scalars()
            .first()
        )
        if row is None:
            return
        row.status = status
        row.note = note
        if written:
            row.windows_written = written
        row.ended_at = datetime.now(UTC)
        db.commit()
    except Exception:  # noqa: BLE001
        # 收尾失败不该盖住真正的错：这条只是账，不是结果。
        db.rollback()


def _other_embed_running() -> str:
    """还有别的 `pipeline.embed` 在跑吗？返回它的 pid（没有就是空串）。

    **为什么要有这道闸**（2026-09-26 实测踩过）：两个 embed 同时跑会抢同一批切片 ——
    一个刚把某片的向量删掉、另一个正往里插，于是报出来的是

        INSERT INTO slice_embeddings … 上一个看不懂的约束错

    而**真正的原因（两个进程在算同一批片）在报错里一个字都没提**，现场只剩一个
    卡住不动、`make watch` 里数字不涨的进程。判活走 `/proc` 而**不查跑单账本**：
    手工起的进程本来就不在账本里（面板上那句"旧版/手工起的进程"就是它）。
    """
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "pipeline.embed" in cmdline and "python" in cmdline:
            return entry.name
    return ""


def _run(args) -> int:  # noqa: ANN001
    if args.fetch:
        ok, detail = local_embed.ensure(log=lambda message: print("   ", message))
        print(("  ✓ " if ok else "  ✗ ") + detail)
        return 0 if ok else 1

    other = _other_embed_running()
    if other:
        print(
            f"已经有一个 embed 在跑（pid {other}）—— 两个进程算同一批切片会互相插队，\n"
            f"  报出来的是 `INSERT INTO slice_embeddings` 上一个看不懂的约束错。\n"
            f"  等它跑完；或者确认它已经死了再重来：kill {other}"
        )
        return 2

    db = get_session_factory()()
    try:
        todo, stats = _pending(
            db, material=args.material, rebuild=args.rebuild, limit=args.limit
        )
        print(
            f"切片 {stats['slices']} 片：要算 {stats['slices_to_do']} 片 / "
            f"{stats['windows']} 个窗口、没变跳过 {stats['unchanged']}"
            + (f"、**半片要补 {stats['partial']}**" if stats["partial"] else "")
            + (f"、换模型 {stats['stale_model']}" if stats["stale_model"] else "")
            + (f"、**正文不在本机 {stats['missing_file']}**" if stats["missing_file"] else ""),
            # **立刻落盘**：这句是进度，被重定向到文件时 python 会缓冲 —— 实测那份日志
            # 跑了半小时还是空的，运维的人只能看着一个 CPU 在转却不知道到哪一步。
            flush=True,
        )
        if not todo:
            print("没有要算的 —— 向量已经是最新的。")
            return 0
        if args.dry_run:
            shown = 0
            for slice_row, mat, windows, _digest in todo:
                print(f"  · {mat.slug}/{slice_row.slice_id} L{slice_row.start_line}-{slice_row.end_line}"
                      f" → {len(windows)} 窗  {str(slice_row.summary)[:34]}")
                shown += 1
                if shown >= 8:
                    print(f"  …… 还有 {len(todo) - shown} 片")
                    break
            return 0

        # 第一次跑会在这里下载模型（约 600MB）—— 这就是"首次使用时下载"发生的地方
        print("准备本地模型：")
        ok, detail = local_embed.ensure(log=lambda message: print("   ", message))
        if not ok:
            print("  ✗ " + detail)
            return 1
        print("  ✓ " + detail)

        model = _model_name()
        # 所有窗口摊成一个流，按 BATCH 送模型 —— 片边界在写回时再还原
        flat: list[tuple] = []
        for slice_row, _mat, windows, digest in todo:
            for ordinal, start, end, text in windows:
                flat.append((slice_row, ordinal, start, end, text, digest))

        # **按正文长度排序再分批**：`enable_padding()` 会补到**批内最长**那一条，
        # 长短混在一批里，短的那些全是白算（实测混排时每批有一两成的 padding 浪费）。
        # 排序不影响正确性 —— 写回是按片分组做的（见下面的 `cleared`）。
        flat.sort(key=lambda item: len(item[4]))

        # 每片**该有几个窗口**：写回时顺手记进 `material_slices.windows` —— 运维对账
        # （`python -m pipeline.ops audit`）靠它先把话说出来，不必每次读一遍正文。
        want = {slice_row.id: len(windows) for slice_row, _mat, windows, _digest in todo}

        # **开一张跑单**：从这一刻起"谁在跑、跑到哪"库里有账，不必再去 `ps` 与临时日志里猜
        #（见 `EmbedRun` 的 docstring；`pipeline.ops` 就是读它）。
        run = EmbedRun(
            material=str(args.material or "(全部)"),
            batch=int(args.batch),
            pid=os.getpid(),
            host=socket.gethostname(),
            status="running",
            slices_planned=len(todo),
            windows_planned=len(flat),
        )
        db.add(run)
        db.commit()

        cleared: set[int] = set()  # 这一轮已经清过旧行的切片
        written = 0
        for offset in range(0, len(flat), args.batch):
            chunk = flat[offset : offset + args.batch]
            started = time.perf_counter()
            vectors = local_embed.embed([item[4] for item in chunk], kind="passage")
            elapsed = (time.perf_counter() - started) * 1000
            if len(vectors) != len(chunk):
                raise SystemExit(
                    "本地模型返回的向量条数对不上（要 %d、给了 %d）—— 不敢往库里写。"
                    % (len(chunk), len(vectors))
                )
            for (slice_row, ordinal, start, end, _text, digest), vector in zip(chunk, vectors):
                if not vector:
                    raise SystemExit("本地模型给了空向量（%s 第 %d 窗）—— 不敢往库里写。"
                                     % (slice_row.slice_id, ordinal))
                # **先删后插**：窗口边界会随正文变化平移，留着旧行就是孤儿 ——
                # 它们带着过期的行区间，检索时会把引用指到错的地方。
                # 一片的窗口可能跨批，所以用 `cleared` 保证只清一次。
                if slice_row.id not in cleared:
                    db.execute(
                        delete(SliceEmbedding).where(SliceEmbedding.slice_id == slice_row.id)
                    )
                    # 顺手把分母记上（对账用；`ops audit --exact` 会校准它）
                    slice_row.windows = int(want.get(slice_row.id) or 0)
                    cleared.add(slice_row.id)
                db.add(
                    SliceEmbedding(
                        slice_id=slice_row.id,
                        ordinal=ordinal,
                        start_line=start,
                        end_line=end,
                        model=model,
                        dim=len(vector),
                        vec=local_embed.pack(vector),
                        text_hash=digest,
                    )
                )
                written += 1
            # 跑单上的进度**每批落一次**：于是"跑到哪一步"是查得到的，不必只看进程还在不在。
            run.windows_written = written
            if not run.device:
                # 第一批算完就知道自己落在哪块芯片上了 —— 会话是**懒建**的，
                # 所以开跑那一刻还问不出来（见 `local_embed._load`）。
                run.device = str(local_embed.active_provider() or "")
            db.commit()
            print(
                f"  已写 {written}/{len(flat)} 窗（这批 {len(chunk)} 窗 / {elapsed:.0f} ms）",
                flush=True,  # 同上：进度被重定向进文件时也要看得见
            )
        _close_run(db, status="ok", written=written)
        print(f"完成：{len(cleared)} 片 / {written} 个窗口入库，模型 {model}。", flush=True)
        return 0
    except BaseException as exc:  # noqa: BLE001
        # **失败也要落账**（见 `_close_run`）：留着一条永远 `running` 的行，
        # 运维就分不清"还在跑"和"早就死了"。
        _close_run(db, status="failed", note=f"{type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把材料切片切成窗口并向量化（派生产物，可重建）")
    parser.add_argument("--material", default="", help="只做这一份材料（slug）")
    parser.add_argument("--rebuild", action="store_true", help="全量重算（换了模型就用它）")
    parser.add_argument("--dry-run", action="store_true", help="只说要算什么，不动库")
    parser.add_argument("--fetch", action="store_true", help="只把本地模型取到本机，不向量化")
    parser.add_argument("--limit", type=int, default=0, help="只算前 N 片")
    parser.add_argument("--batch", type=int, default=BATCH, help=f"每批多少窗（默认 {BATCH}）")
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
