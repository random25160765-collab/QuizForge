"""派工：读 maps/<材料>/ 下的 slices.yaml 与 figures.yaml，按**缺口**生成任务（幂等）。

用法：
    api/.venv/bin/python -m pipeline.dispatch --map maps/tt-metal/METALIUM_GUIDE
    api/.venv/bin/python -m pipeline.dispatch --map maps/... --kind vision
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

from . import config, dbstore, store, worker


def _map_dir(arg: str) -> Path:
    path = Path(arg)
    return path if path.is_absolute() else (config.ROOT / path)


def slices_from_db(slug: str) -> list[dict]:
    """切片现在住在库里（`material_slices`），形状与 YAML 一致，调用方不必分两套。

    `maps/<材料>/slices.yaml` 是早期产物：派工要问的是"哪些切片还没抽过"，
    这个问题在几十个材料、上千个切片之后只能问库。
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.slice_id, s.path, s.start_line, s.end_line, s.tokens, s.figures, s.summary"
                " FROM material_slices s JOIN materials m ON m.id = s.material_id"
                " WHERE m.slug = :slug ORDER BY s.start_line"
            ),
            {"slug": slug},
        ).fetchall()
    slices: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        slices.append(
            {
                "id": data["slice_id"],
                "path": data["path"],
                "loc": {"lines": [data["start_line"], data["end_line"]]},
                "tokens": data["tokens"],
                "figures": data["figures"] or [],
                "summary": data["summary"],
            }
        )
    return slices


def dispatch_extract(conn, map_dir: Path, doc: dict) -> tuple[int, int]:
    source_path = Path(doc["source_path"])
    prompt_ver = worker.prompt_version("extract")
    new = old = 0
    for slice_ in slices_from_db(str(doc.get("material") or "")) or doc.get("slices") or []:
        start, end = slice_["loc"]["lines"]
        digest = worker.slice_hash(source_path, start, end, prompt_ver)
        payload = {
            "map": doc["material"],
            "material": Path(doc["source_path"]).name,
            "source_path": str(source_path),
            "slice_id": slice_["id"],
            "path": slice_.get("path"),
            "summary": slice_.get("summary"),
            "tokens": slice_.get("tokens"),
            "figures": slice_.get("figures") or [],
            "lines": [start, end],
            "prompt_version": prompt_ver,
            "result_dir": str(map_dir / "extract"),
        }
        if store.add_task(conn, "extract", digest, payload):
            new += 1
        else:
            old += 1
    return new, old


def dispatch_vision(conn, map_dir: Path, doc: dict, figures: dict) -> tuple[int, int]:
    root = Path(doc["source_path"]).parent
    prompt_ver = worker.prompt_version("vision")
    new = old = 0
    for fig in figures.get("figures") or []:
        if fig.get("explanation_subject") is False:
            continue
        src = fig["src"]
        image = root / src
        if not image.is_file():
            print(f"[skip] 缺图：{image}", file=sys.stderr)
            continue
        digest = hashlib.sha256(
            f"{image.stat().st_size}|{src}|{prompt_ver}".encode("utf-8")
        ).hexdigest()[:16]
        payload = {
            "map": figures["material"],
            "figure_id": fig["id"],
            "src": src,
            "image_path": str(image),
            "loc": fig.get("loc"),
            "figure": {k: fig.get(k) for k in ("caption", "lead", "kind", "keys")},
            "prompt_version": prompt_ver,
            "result_dir": str(map_dir / "figures"),
        }
        if store.add_task(conn, "vision", digest, payload):
            new += 1
        else:
            old += 1
    return new, old


def covered_point_keys(material_slug: str) -> set[str]:
    """这个材料里**已经出过题**的点 key。

    两条来源，取并集：
    ① **题↔点的边**（`question_points`）—— 精确，题目挂着哪个点就是哪个点；
    ② 题的 `topic` 直接等于点 key —— 历史近似，覆盖早期还没写挂接的那批题。

    派工必须跳过它们：否则每跑一轮都把全部点重出一遍，产出大量同一知识点、
    同一层的近似题。注意这里**只能问库** —— 题库已经不在仓库里了，
    以前那版扫 `questions/**/*.md` 的实现会静默返回空集（实测会重复出题）。
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        linked = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT DISTINCT kp.key FROM question_points qp"
                    " JOIN knowledge_points kp ON kp.id = qp.point_id"
                    " JOIN materials m ON m.id = kp.material_id"
                    " WHERE m.slug = :slug"
                ),
                {"slug": material_slug},
            ).all()
        }
        topics = {
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT topic FROM questions WHERE status IN ('published', 'verified')")
            ).all()
        }
    return linked | topics


def dispatch_verify(conn, map_dir: Path, doc: dict) -> tuple[int, int]:
    """给「还没有通过判定」的题补校验任务。

    校验任务平时由出题 worker 串联入队。问题是：它们一旦被归档/清掉（重跑、改提示词），
    **没有任何东西能把它们找回来** —— 队列空着、题目躺在暂存区没人验，
    命令却回显「已存在 N 个」。这里按「暂存区里没有 pass 判定的题」重新派工，
    和出题的「只补缺口」是同一条原则：**派工必须能从产物反推，而不是只靠上一环串联**。
    """
    prompt_ver = worker.prompt_version("verify")
    new = old = 0
    for question_id in dbstore.draft_ids():
        record = dbstore.load_question(question_id)
        if record is None:
            continue
        payload = record.get("payload") or {}
        ranges = payload.get("verify_ranges") or payload.get("sources") or []
        verify_payload = {
            "map": doc["material"],
            "question_id": question_id,
            "source_path": doc["source_path"],
            "ranges": ranges,
            "out_dir": "",
            "prompt_version": prompt_ver,
            # 只补校验，不触发重出：重出要带上原任务包的 points，这里没有那份上下文，
            # 硬造一个空包只会产出一道垃圾题。round=2 让串联逻辑不再重出（见 worker 的判定）。
            "origin": {"map": doc["material"], "round": 2, "ranges": ranges,
                       "source_path": doc["source_path"], "prompt_version": prompt_ver},
        }
        digest = hashlib.sha256(
            (question_id + json.dumps(ranges, ensure_ascii=False) + prompt_ver).encode("utf-8")
        ).hexdigest()[:16]
        if store.add_task(conn, "verify", digest, verify_payload):
            new += 1
        else:
            old += 1
    return new, old


def dispatch_author(conn, map_dir: Path, doc: dict, coverage: dict) -> tuple[int, int]:
    """把覆盖矩阵的知识点打成任务包（按材料位置排序，每包 3–6 个点）。

    **四层共用这一条流水线**：不按层分批，也没有「覆盖批 / 精修批」之分。
    每个点该出哪一层由它自己的 layers 决定，出题员在同一次任务里一并处理 ——
    层只影响「题怎么写」（题型与判定标准），不影响「走哪条流程」。
    """
    done_topics = covered_point_keys(str(doc.get("material") or ""))
    pool = [p for p in (coverage.get("points") or []) if p.get("key") not in done_topics]
    skipped = len(coverage.get("points") or []) - len(pool)
    if skipped:
        print(f"派工：跳过 {skipped} 个已覆盖的点（只补缺口）")
    pool.sort(key=lambda p: ((p.get("sources") or [[0, 0]])[0][0], p["key"]))
    # 每包 3 个点：四层同批之后，一个包要写的题变多了，而服务端的输出上限是硬的
    # （实测 5 个点 × 四层会在 7.2KB 处被截断 → JSON 解析失败、整包作废）。
    # 宁可多派几个包并行，也不要一个包写不完。
    packs = [pool[i : i + 3] for i in range(0, len(pool), 3)]

    prompt_ver = worker.prompt_version("author")
    bank = sorted((config.ROOT / "questions" / "tt-arch").glob("*.md"))
    existing = [f"{p.name}" for p in bank]
    # 起始编号从**正式题库的最大编号 +1** 继续：否则新一轮会重新产出 tt-arch-0002…
    # 之类的编号，把它们和已入库的题撞在一起（暂存区覆盖、提升时又被当重复跳过）。
    max_id = 1
    for path in bank:
        match = re.search(r"-(\d{4})", path.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    new = old = 0
    for index, pack in enumerate(packs, start=1):
        pack_id = f"pack-{index:02d}"
        payload = {
            "map": doc["material"],
            "material": Path(doc["source_path"]).name,
            # 题目落在哪个学科目录（= 题号前缀）。编号由库里分配，这里只定学科。
            "subject": "tt-arch",
            "source_path": doc["source_path"],
            "pack_id": pack_id,
            "points": [
                {k: point.get(k) for k in ("key", "name", "kind", "thickness", "layers", "terms", "note")}
                for point in pack
            ],
            "layer_target": sorted({layer for point in pack for layer in (point.get("layers") or [])}),
            "start_index": 2 + (index - 1) * 10,
            "ranges": [list(r) for point in pack for r in (point.get("sources") or [])],
            "existing": existing,
            "out_dir": str(map_dir / "staged"),
            "prompt_version": prompt_ver,
        }
        digest = hashlib.sha256(
            (
                f"{pack_id}|"
                + "|".join(f"{p['key']}@{p.get('sources')}" for p in pack)
                + f"|{prompt_ver}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        if store.add_task(conn, "author", digest, payload):
            new += 1
        else:
            old += 1
    return new, old


def doc_from_db(slug: str) -> dict | None:
    """材料元数据 + 切片，从库里组装（形状与 slices.yaml 一致，调用方不必分两套）。"""
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT slug, subject, title, source_path, lines FROM materials WHERE slug = :slug"),
            {"slug": slug},
        ).fetchone()
    if row is None:
        return None
    data = dict(row._mapping)
    return {
        "material": data["slug"],
        "subject": data["subject"],
        "title": data["title"],
        "source_path": data["source_path"],
        "lines": data["lines"],
        "slices": slices_from_db(slug),
    }


def figures_from_db(slug: str) -> dict | None:
    """图清单，从库里组装（形状与 figures.yaml 一致）。"""
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT f.figure_id, f.src, f.slice_id, f.start_line, f.end_line, f.caption,"
                " f.kind, f.keys, f.plan FROM material_figures f"
                " JOIN materials m ON m.id = f.material_id WHERE m.slug = :slug"
                " ORDER BY f.start_line, f.figure_id"
            ),
            {"slug": slug},
        ).fetchall()
    if not rows:
        return None
    figures: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        figures.append(
            {
                "id": data["figure_id"],
                "src": data["src"],
                "slice": data["slice_id"],
                "loc": {"lines": [data["start_line"], data["end_line"]]},
                "caption": data["caption"],
                "kind": data["kind"],
                "keys": data["keys"] or [],
                "plan": data["plan"],
            }
        )
    return {"material": slug, "figures": figures}


def coverage_from_db(slug: str) -> dict | None:
    """知识点（含出处）从库里组装，形状与 coverage.yaml 一致 —— 出题派工要用。

    出处按 `key` 从点候选里并起来（合并后的点就是把同一 key 的候选出处取并集），
    于是不必依赖任何文件。
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.db import get_engine  # noqa: PLC0415

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT kp.key, kp.name, kp.kind, kp.thickness, kp.layers, kp.producible, kp.note,
                       COALESCE((
                         SELECT jsonb_agg(
                           DISTINCT jsonb_build_array(ps.start_line, ps.end_line)
                           ORDER BY jsonb_build_array(ps.start_line, ps.end_line)
                         )
                         FROM point_sources ps WHERE ps.point_id = kp.id
                       ), '[]'::jsonb) AS sources
                FROM knowledge_points kp
                JOIN materials m ON m.id = kp.material_id
                WHERE m.slug = :slug
                ORDER BY kp.key
                """
            ),
            {"slug": slug},
        ).fetchall()
    if not rows:
        return None
    points: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        points.append(
            {
                "key": data["key"],
                "name": data["name"],
                "kind": data["kind"],
                "thickness": data["thickness"],
                "layers": data["layers"] or [],
                "producible": data["producible"],
                "note": data["note"],
                "sources": data["sources"] or [],
            }
        )
    return {"material": slug, "points": points}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.dispatch", description="按缺口派工")
    parser.add_argument("--map", help="材料目录（历史用法），如 maps/tt-metal/METALIUM_GUIDE")
    parser.add_argument(
        "--material", help="材料 slug（推荐）：库里材料的身份，如 CNNs-cnn_optimizations"
    )
    parser.add_argument(
        "--kind", default="all", choices=["extract", "vision", "author", "verify", "all"]
    )
    parser.add_argument("--requeue-dead", action="store_true")
    args = parser.parse_args(argv)

    if args.material:
        slug = str(args.material)
        map_dir = config.MAPS_DIR / slug  # 只用于日志与历史兜底；文件不是权威
    elif args.map:
        map_dir = _map_dir(args.map)
        slug = map_dir.relative_to(config.MAPS_DIR).as_posix()
    else:
        raise SystemExit("要么给 --material <slug>，要么给 --map <目录>")
    slices_file = map_dir / "slices.yaml"
    figures_file = map_dir / "figures.yaml"
    coverage_file = map_dir / "coverage.yaml"

    # 材料元数据、切片、图清单、知识点都以**库**为准（materials / material_slices /
    # material_figures / knowledge_points）。`maps/` 下的文件是可重建的中间产物：
    # 没有它们也能派工 —— 这是"上下游彻底对齐"的验收条件；文件只作历史兜底。
    doc = doc_from_db(slug)
    if doc is None:
        if not slices_file.is_file():
            raise SystemExit(f"库里没有材料 {slug}，也没有 {slices_file}")
        doc = yaml.safe_load(slices_file.read_text(encoding="utf-8"))
    conn = store.connect()

    if args.requeue_dead:
        print(f"打回 dead：{store.requeue_dead(conn)} 个")

    if args.kind in ("extract", "all"):
        new, old = dispatch_extract(conn, map_dir, doc)
        print(f"extract：新建 {new} 个任务，已存在 {old} 个（幂等，不重复）")
    if args.kind in ("vision", "all"):
        figures = figures_from_db(slug)
        if figures is None and figures_file.is_file():
            figures = yaml.safe_load(figures_file.read_text(encoding="utf-8"))
        if figures:
            new, old = dispatch_vision(conn, map_dir, doc, figures)
            print(f"vision ：新建 {new} 个任务，已存在 {old} 个")
    if args.kind in ("verify", "all"):
        new, old = dispatch_verify(conn, map_dir, doc)
        print(f"verify ：新建 {new} 个任务（补未通过的题），已存在 {old} 个")
    # 必须是 in ("author", "all")：写成 == "author" 的话，`--kind all` 永远不派出题包
    # （实测：drive 每轮因此只消费旧队列，跑完第 2 轮就"收敛"了 ✗）
    if args.kind in ("author", "all"):
        coverage = coverage_from_db(slug)
        if coverage is None:
            if not coverage_file.is_file():
                raise SystemExit(f"库里没有 {slug} 的知识点，也没有 {coverage_file}")
            coverage = yaml.safe_load(coverage_file.read_text(encoding="utf-8"))
        new, old = dispatch_author(conn, map_dir, doc, coverage)
        print(f"author ：新建 {new} 个任务包，已存在 {old} 个")

    summary = store.summary(conn)
    print("任务总览：")
    print(json.dumps(summary["by_kind"], ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
