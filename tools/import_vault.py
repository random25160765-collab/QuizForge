"""把一个笔记库（Obsidian vault）导入应用数据目录 —— **非破坏、幂等、可核对**。

三件事必须同时成立，缺一个就会变成"跑过一次之后不敢再跑"的脚本：

* **源目录不动**：只读。搬错、删错都能重来（与 `tools/migrate_to_local.py` 同一个讲究）。
* **幂等**：第二次跑不产生副本、不覆盖你自己改过的东西。判定靠"上次我写下去的是什么"
  （记在清单里）：当前文件与它一致，才允许改写；不一致就是**你改过** → 报冲突、不动。
  这条是整个脚本的核心 —— 没有它，A2 里你在界面上编辑过的笔记会被下一次导入悄悄冲掉。
* **可核对**：末尾给一份清单（新增 / 更新 / 跳过 / 冲突 / 跳过文件 / 断链 / 未引用附件），
  数字对得上，且落到 `.import/<库名>.json`，将来能复查。

导入的边界（用户定）：**先导 `Math` 当占位数据**；另外三个库是同一条命令加 `--vault` 的事。

用法：
    python tools/import_vault.py --vault Math             # 写入
    python tools/import_vault.py --vault Math --dry-run    # 只看会做什么
    python tools/import_vault.py --vault Math --data-dir /path/to/quizforge-beta
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

def _default_roots() -> tuple[Path, ...]:
    """源库（笔记库）的候选位置 —— 它在**仓库之外**，没有一条写死的路径能通用。

    顺序：
      1. 环境变量 `QF_VAULT_ROOTS`（`os.pathsep` 分隔，想指哪都行）；
      2. WSL 下扫 `/mnt/<盘>/Vaults`（F 盘只是其中一种可能）；
      3. Windows 原生 Python 下的盘符写法。
    踩过：这里原先写死 `F:` 盘（`/mnt/f/Vaults`、`F:\\Vaults`），
    换台机器换个盘符就得改代码。都没命中时返回空，由调用方报错并提示 `--root`。
    """
    env = os.environ.get("QF_VAULT_ROOTS")
    if env:
        return tuple(Path(p).expanduser() for p in env.split(os.pathsep) if p)

    found: list[Path] = []
    try:
        # 只认单字母挂载点（`/mnt/c`、`/mnt/f` 才是 Windows 盘；
        # `/mnt/wsl`、`/mnt/wslg` 是 WSL 自己的内部挂载，不是用户盘）
        drives = sorted(
            p for p in Path("/mnt").iterdir() if p.is_dir() and len(p.name) == 1
        )
    except OSError:  # pragma: no cover - 非 WSL 环境里 /mnt 不可读
        drives = []
    found += [drive / "Vaults" for drive in drives]
    found += [Path(f"{letter}:/Vaults") for letter in ("C", "D", "E", "F", "G")]
    return tuple(found)

#: 不进语义的目录（配置、回收站之类）—— 不导入，**也不删**
SKIP_DIRS = frozenset({".obsidian", ".trash", ".git", "__pycache__", ".smart-env"})

#: Bases 文件：明确不做，但它还是用户的文件，跳过即可
SKIP_SUFFIXES = frozenset({".base"})


@dataclass
class Entry:
    """一个待处理的文件（笔记 / 画布 / 附件）。"""

    rel: str
    kind: str                   # note | canvas | attachment
    action: str                 # add | update | skip | conflict
    payload: bytes = b""
    size: int = 0
    generated_header: bool = False


@dataclass
class Plan:
    """一个库的完整导入计划（先算清楚，再动手写）。"""

    vault: str
    source: Path
    target: Path
    entries: list[Entry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)        # (路径, 原因)
    broken: list[tuple[str, str]] = field(default_factory=list)          # (来自哪篇, 指向)
    unresolved: list[tuple[str, str]] = field(default_factory=list)      # 笔记链接没对上
    unreferenced: list[tuple[str, int]] = field(default_factory=list)    # (路径, 字节)
    problems: list[tuple[str, str]] = field(default_factory=list)
    ambiguous: int = 0

    def of(self, kind: str) -> list[Entry]:
        return [item for item in self.entries if item.kind == kind]

    def count(self, kind: str, action: str) -> int:
        return sum(1 for item in self.entries if item.kind == kind and item.action == action)


# ------------------------------------------------------------------ 机械


def _configure_console() -> None:
    """Windows 控制台是 GBK（中文区默认），而这份输出里有 `✓` `→` 这类字符。

    **只放宽 errors、不改编码**：中文在 GBK 下本来就打得出来，改了编码反而让中文变乱码；
    编不出的那几个符号替换掉就够（与 `build/package.py` / `tools/make_icons.py` 同一套做法）。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):  # pragma: no cover - 管道已关闭之类
                pass


def _default_root() -> Path | None:
    """第一个真实存在的候选位置；一个都没有就给 None，交给调用方报错。"""
    for candidate in _default_roots():
        if candidate.is_dir():
            return candidate
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再改名 —— 中途出错不会留下半份文件（笔记是权威，不能有半份）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _decide(rel: str, payload: bytes, target: Path, previous: dict) -> str:
    """add / update / skip / conflict —— 幂等的判定都在这里。

    * 目标不存在 → 新增
    * 目标内容 == 我要写的 → 跳过（第二次跑就该全是这个）
    * 目标内容 != 我上次写下的 → **你改过**，报冲突、不动
    * 其余（我上次写的还在，但源文件变了）→ 更新
    """
    from app.notes import sha256_bytes, sha256_file  # noqa: PLC0415

    digest = sha256_bytes(payload)
    if not target.exists():
        return "add"
    current = sha256_file(target)
    if current == digest:
        return "skip"
    recorded = (previous.get(rel) or {}).get("sha256")
    if recorded and current != recorded:
        return "conflict"
    return "update"


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _note_payload(path: Path, rel: str) -> tuple[bytes, bool, str | None]:
    """算出要写下去的字节，以及是否补了元数据头。

    有头的文件**原样搬字节**（不做任何规范化）—— 源文件是权威，我没理由重排它。
    只有没头的才重写一次：加最小头，并在头里标明是我加的。
    """
    from app.notes import GENERATED_KEY, minimal_meta, render, split_text  # noqa: PLC0415

    raw = _read_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        problem = "不是 UTF-8，按替换字符读入（内容里的非 UTF-8 字节会变）"
    else:
        problem = None

    note = split_text(text)
    if note.broken_header:
        # 头坏了就**不碰它**：补一个头会变成两个头叠着，比原样留着更糟
        return raw, False, "元数据头没有闭合的 ---，原样导入、不补头"
    if note.had_header:
        return raw, False, problem
    meta = minimal_meta(note.body, Path(rel).stem)
    assert meta.get(GENERATED_KEY) is True
    return render(meta, note.body).encode("utf-8"), True, problem


# ------------------------------------------------------------------ 计划


def build_plan(vault: str, source: Path, target: Path, previous: dict) -> Plan:
    from app.notes import (
        CANVAS_SUFFIX,
        MD_SUFFIX,
        build_index,
        iter_links,
        resolve,
    )

    plan = Plan(vault=vault, source=source, target=target)

    notes: list[str] = []
    canvases: list[str] = []
    attachments: list[str] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            plan.skipped.append((rel, "配置目录，不导入语义也不删"))
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            plan.skipped.append((rel, "Bases：明确不做（见 docs/笔记功能对照.md）"))
            continue
        if path.suffix.lower() == MD_SUFFIX:
            notes.append(rel)
        elif path.suffix.lower() == CANVAS_SUFFIX:
            canvases.append(rel)
        else:
            attachments.append(rel)

    # ---- 引用：谁引用了哪个文件（附件按引用搬，未引用的只列清单）
    rels = {*notes, *canvases, *attachments}
    by_name = build_index(rels)
    referenced: dict[str, list[str]] = {}
    for rel in [*notes, *canvases]:
        try:
            text = (source / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            plan.problems.append((rel, f"读不动：{exc}"))
            continue
        targets = [link.target for link in iter_links(text)]
        if rel.endswith(CANVAS_SUFFIX):
            # 画布里指向别的笔记的是 `file` 节点（A3 会把这段搬去 `app/canvas.py`）。
            # 空的 `{}`、坏 JSON 都要能忍 —— 实测 `Untitled.canvas` 就是 2 字节的 `{}`。
            try:
                payload = json.loads(text or "{}")
            except ValueError:
                payload = None
                plan.problems.append((rel, "画布 JSON 解析不了，只当条目导入、不算它的引用"))
            if isinstance(payload, dict):
                for node in payload.get("nodes") or []:
                    if isinstance(node, dict) and isinstance(node.get("file"), str):
                        targets.append(node["file"])

        for link in targets:
            if not link:
                continue
            found = resolve(
                link, by_name=by_name, rels=rels, current_dir=str(Path(rel).parent)
            )
            if found.ambiguous:
                plan.ambiguous += 1
            if not found:
                (plan.broken if Path(link).suffix else plan.unresolved).append((rel, link))
                continue
            if found.rel in attachments:
                referenced.setdefault(found.rel, []).append(rel)

    # ---- 未引用的附件：只列清单（用户定：Math 先当占位数据，不为了占位拷 690MB）
    for rel in attachments:
        if rel not in referenced:
            plan.unreferenced.append((rel, (source / rel).stat().st_size))

    # ---- 待写的条目
    for rel in notes:
        payload, generated, problem = _note_payload(source / rel, rel)
        if problem:
            plan.problems.append((rel, problem))
        plan.entries.append(
            Entry(
                rel=rel,
                kind="note",
                action=_decide(rel, payload, target / rel, previous),
                payload=payload,
                size=len(payload),
                generated_header=generated,
            )
        )
    for rel in canvases:
        payload = _read_bytes(source / rel)
        plan.entries.append(
            Entry(
                rel=rel,
                kind="canvas",
                action=_decide(rel, payload, target / rel, previous),
                payload=payload,
                size=len(payload),
            )
        )
    for rel in sorted(referenced):
        payload = _read_bytes(source / rel)
        plan.entries.append(
            Entry(
                rel=rel,
                kind="attachment",
                action=_decide(rel, payload, target / rel, previous),
                payload=payload,
                size=len(payload),
            )
        )
    return plan


def apply_plan(plan: Plan, previous: dict, *, dry_run: bool = False) -> dict:
    """真正落盘，并算出清单里要记的指纹。

    **干跑必须真的一个字都不写** —— 这条踩过：`dry_run` 只挡了清单（`save_manifest`），
    没挡这里，于是"先干看看"的那一次把 101 个文件全写下去了，紧接着的正式运行
    报"新增 0"，看起来像"幂等生效"，其实是干跑自己动的手。

    指纹的含义必须**只是"我上次写下去的字节"**，否则幂等判定会骗自己：

    * 新增 / 更新 / 跳过 → 这次的内容就是我写下去的（"跳过"的原因正是内容已经一致）；
    * **冲突 → 保留上次记的那个值**：这次我一个字都没写，记成"我本来要写的内容"等于撒谎，
      下一轮会把"你改过"误判成"我写过"，于是把你的改动冲掉。
    """
    from app.notes import sha256_bytes

    written: list[str] = []
    digests: dict[str, str] = {}
    for entry in plan.entries:
        if entry.action in ("add", "update"):
            if not dry_run:
                _write_atomic(plan.target / entry.rel, entry.payload)
            written.append(entry.rel)
        if entry.action == "conflict":
            digests[entry.rel] = str((previous.get(entry.rel) or {}).get("sha256") or "")
        else:
            digests[entry.rel] = sha256_bytes(entry.payload)
    return {"written": written, "digests": digests}


# ------------------------------------------------------------------ 清单


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(plan: Plan, digests: dict[str, str], path: Path, dry_run: bool) -> None:
    if dry_run:
        return
    payload = {
        "vault": plan.vault,
        "source": str(plan.source),
        "target": str(plan.target),
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "notes": len(plan.of("note")),
        "canvases": len(plan.of("canvas")),
        "attachments": len(plan.of("attachment")),
        "generated_headers": sum(1 for e in plan.of("note") if e.generated_header),
        "skipped": [{"path": rel, "why": why} for rel, why in plan.skipped],
        "broken_links": [{"from": src, "target": tgt} for src, tgt in plan.broken],
        "unresolved_links": [{"from": src, "target": tgt} for src, tgt in plan.unresolved],
        "unreferenced": [{"path": rel, "bytes": size} for rel, size in plan.unreferenced],
        "problems": [{"path": rel, "why": why} for rel, why in plan.problems],
        # 每个文件的指纹：下次跑靠它判断"我写下的还在不在"
        "files": {entry.rel: {"sha256": digests.get(entry.rel, ""), "kind": entry.kind}
                  for entry in plan.entries},
    }
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


# ------------------------------------------------------------------ 输出


def _mb(size: float) -> str:
    return f"{size / 1e6:.1f} MB"


def report(plan: Plan, dry_run: bool) -> None:
    mode = "干跑（什么都没写）" if dry_run else "已写入"
    print(f"\n=== {plan.vault} → {plan.target}   [{mode}]")
    print(f"  源：{plan.source}")
    print(
        f"  笔记 {len(plan.of('note'))}：新增 {plan.count('note', 'add')}"
        f" · 更新 {plan.count('note', 'update')}"
        f" · 跳过 {plan.count('note', 'skip')}"
        f" · **冲突 {plan.count('note', 'conflict')}**"
    )
    print(
        f"  画布 {len(plan.of('canvas'))}：新增 {plan.count('canvas', 'add')}"
        f" · 跳过 {plan.count('canvas', 'skip')}"
    )
    total_mb = sum(e.size for e in plan.of("attachment"))
    print(
        f"  附件（被引用）：{len(plan.of('attachment'))} 个 {_mb(total_mb)}"
        f" · 新增 {plan.count('attachment', 'add')} · 冲突 {plan.count('attachment', 'conflict')}"
    )
    left = sum(size for _, size in plan.unreferenced)
    print(f"  未引用附件：{len(plan.unreferenced)} 个 {_mb(left)} —— 只列清单，不搬（源目录不动）")
    for rel, size in sorted(plan.unreferenced, key=lambda item: -item[1])[:5]:
        print(f"      {_mb(size):>10}  {rel}")
    if len(plan.unreferenced) > 5:
        print(f"      （其余 {len(plan.unreferenced) - 5} 个见清单）")
    generated = sum(1 for e in plan.of("note") if e.generated_header)
    print(f"  补了元数据头：{generated} 篇（头里带 qf_generated: true，可批量回退）")
    print(f"  跳过文件：{len(plan.skipped)}")
    if plan.broken:
        print(f"  **断链 {len(plan.broken)}**（引用里有后缀、但库里找不到）：")
        for src, target in plan.broken[:6]:
            print(f"      {src} → {target}")
        if len(plan.broken) > 6:
            print(f"      （其余 {len(plan.broken) - 6} 条见清单）")
    if plan.unresolved:
        print(f"  指向不存在的笔记：{len(plan.unresolved)}（A2 会做成「未解析链接」提示）")
    if plan.ambiguous:
        print(f"  同名歧义：{plan.ambiguous} 处（取排序第一个，清单里有原文）")
    for rel, why in plan.problems[:6]:
        print(f"  ! {rel}：{why}")


# ------------------------------------------------------------------ 入口


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="把笔记库导入应用数据目录（非破坏、幂等、可核对）"
    )
    parser.add_argument(
        "--root",
        default="",
        help="库的根目录（放各个 vault 的那个目录）。默认候选见 _default_roots()："
        "先看环境变量 QF_VAULT_ROOTS，否则扫 /mnt/<盘>/Vaults 与盘符写法",
    )
    parser.add_argument("--vault", action="append", default=[], help="要导入哪个库，可重复")
    parser.add_argument("--notes-root", default="", help="导入到哪（默认 <数据目录>/notes）")
    parser.add_argument("--data-dir", default="", help="整个应用数据目录（会覆盖 QF_DATA_DIR）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写任何文件")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["QF_DATA_DIR"] = args.data_dir

    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    notes_root = Path(args.notes_root) if args.notes_root else settings.data_dir / "notes"
    root = Path(args.root) if args.root else _default_root()
    if root is None or not root.is_dir():
        print("找不到库的根目录。用 --root 指一下（放各个 vault 的那个目录），")
        print("或设 QF_VAULT_ROOTS=/mnt/<盘>/Vaults（多个用 os.pathsep 分隔）。")
        return 2

    names = args.vault or sorted(
        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    sources = []
    for name in names:
        source = root / name
        if not source.is_dir():
            print(f"跳过 {name}：{source} 不是目录")
            continue
        sources.append((name, source))
    if not sources:
        print(f"{root} 下没有可导入的库")
        return 2

    print(f"数据目录：{settings.data_dir}")
    print(f"笔记根目录：{notes_root}")

    failures = 0
    for name, source in sources:
        manifest_path = notes_root / ".import" / f"{name}.json"
        previous = (load_manifest(manifest_path).get("files") or {})
        plan = build_plan(name, source, notes_root / name, previous)
        report(plan, args.dry_run)
        result = apply_plan(plan, previous, dry_run=args.dry_run)
        save_manifest(plan, result["digests"], manifest_path, args.dry_run)
        if plan.count("note", "conflict") or plan.count("attachment", "conflict"):
            failures += 1
            print("  → 有冲突：那些文件你改过，本次没动它们（清单里能看到是哪些）")
        if not args.dry_run:
            print(f"  清单：{manifest_path}")

    if failures:
        print(f"\n完成，但有 {failures} 个库存在冲突（那些文件你改过，本次没动它们）。")
        return 1
    print("\n干跑结束（未落盘）。" if args.dry_run else "\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
