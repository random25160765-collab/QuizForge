"""数据包的命令行：导出 / 导入 / 看内容。

    # 把这台机器上的用户数据打成一个包（题库不带，见下）
    python -m app.cli.pack export --out ~/quizforge-user.qfpack

    # 看一眼这个包里有什么（只读，不展开）
    python -m app.cli.pack show --in ~/quizforge-user.qfpack

    # 在另一台机器上放回去（目标库已有用户数据时要加 --force）
    python -m app.cli.pack import --in ~/quizforge-user.qfpack

**两类数据，两个包，两条路**：

* 用户数据（笔记 / 资料库元数据 / 对话 / 自出的题 / 设置 + 学习进度）走这里；
* 题库（题目 / 考纲树 / 概念图 / 材料切片）走出题机那一侧 ——
  `make bank-export` / `make bank-import`（`pipeline/bankfile.py`），用户可选择性导入。

边界（哪张表属于谁、哪些东西不带、为什么）在 `app/datapack.py` 的 `BOUNDARY`
与包里那份 `manifest.json` 的 `excluded` 段里，这里只负责命令行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..datapack import PackError, describe, export_user, import_user


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def _print_manifest(manifest: dict, out) -> None:
    out(f"包：{manifest.get('exportedAt', '')} 导出")
    source = manifest.get("source") or {}
    if source:
        out(f"  来自：{source.get('db', '')}")
    bank = manifest.get("bank") or {}
    if bank.get("contentHash"):
        out(
            f"  题库：{bank.get('contentHash', '')[:12]}… "
            f"（{bank.get('questions', 0)} 题 / {bank.get('topics', 0)} 主题，"
            f"导入于 {bank.get('importedAt', '')}）"
        )
    else:
        out("  题库：（导出时这台机器上没有装题库）")

    tables = manifest.get("tables") or {}
    if tables:
        out("  库里的用户数据：")
        for name in sorted(tables):
            out(f"    {name:<22} {tables[name]:>6} 行")

    files = manifest.get("files") or {}
    if files:
        out("  文件：")
        for name in sorted(files):
            slot = files[name]
            out(f"    {name:<22} {slot.get('files', 0):>6} 个 · {_fmt_bytes(int(slot.get('bytes', 0)))}")

    excluded = manifest.get("excluded") or {}
    if excluded:
        out("  没带的东西（以及为什么）：")
        for name in sorted(excluded):
            out(f"    {name:<22} {excluded[name]}")

    if (manifest.get("secrets") or {}).get("aiKeyScrubbed"):
        out("  密钥：已抹掉（要带上就加 --with-secrets）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.pack",
        description="用户数据打包 / 还原（题库走 make bank-import）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="把本机的用户数据打成一个包")
    export.add_argument("--out", required=True, type=Path, help="产物路径（.qfpack）")
    export.add_argument(
        "--with-library-text",
        action="store_true",
        help="连资料库抽出的正文（.text，本机约 251MB）一起带 —— 重建要源文件才需要它",
    )
    export.add_argument("--no-files", action="store_true", help="只导库里的用户数据，不带任何文件")
    export.add_argument(
        "--with-secrets",
        action="store_true",
        help="连 AI 密钥一起带（默认抹掉：包会被拷来拷去，密钥不该跟着走）",
    )
    export.add_argument("--json", action="store_true", help="只输出 manifest 的 JSON")

    restore = sub.add_parser("import", help="把包放回本机（题库一行都不动）")
    restore.add_argument("--in", dest="pack", required=True, type=Path, help="包的路径")
    restore.add_argument("--force", action="store_true", help="本机已有用户数据时也覆盖")
    restore.add_argument("--no-files", action="store_true", help="只还原库里的用户数据，不落文件")
    restore.add_argument("--json", action="store_true", help="只输出结果的 JSON")

    show = sub.add_parser("show", help="看一个包里有什么（只读）")
    show.add_argument("--in", dest="pack", required=True, type=Path, help="包的路径")
    show.add_argument("--json", action="store_true", help="只输出 JSON")

    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            manifest = export_user(
                args.out,
                with_library_text=args.with_library_text,
                with_files=not args.no_files,
                with_secrets=args.with_secrets,
            )
            if args.json:
                print(json.dumps(manifest, ensure_ascii=False, indent=1, default=str))
            else:
                pack = manifest.get("pack") or {}
                print(f"打好了一个包：{pack.get('path', '')}（{_fmt_bytes(int(pack.get('bytes', 0)))}）")
                _print_manifest(manifest, print)
                print(
                    "\n题库**不在**这个包里。换机器时：拷源码 → 构建 →"
                    " `make user-import`，要题目再 `make bank-import`。"
                )
            return 0

        if args.command == "import":
            result = import_user(
                args.pack,
                force=args.force,
                with_files=not args.no_files,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
            else:
                print(f"数据已放回：{result['db']}")
                if result["created"]:
                    print("  （本机原来没有库，按同一套表结构建了一个）")
                if result["overwrote"]:
                    print("  （覆盖了本机原有的用户数据 —— 这就是 --force 的意思）")
                moved = result.get("tables") or {}
                total = sum(int(v) for v in moved.values())
                print(f"  库：{len(moved)} 张表 · {total} 行")
                if result["files"]:
                    print(f"  文件：{result['files']} 个")
                if result["keptLocalSecret"]:
                    print("  密钥：包里没带，保留了本机原来那一把")
                bank = result.get("bank") or {}
                if bank.get("matches"):
                    print("  题库：与这份包当初用的那一份一致 ✓")
                else:
                    was = (bank.get("pack") or {}).get("contentHash") or ""
                    now = (bank.get("local") or {}).get("contentHash") or ""
                    print(
                        "  题库：对不上 —— 这份包配的是 "
                        f"{(was[:12] + '…') if was else '（导出时没有题库）'}，"
                        f"本机现在是 {(now[:12] + '…') if now else '（没装题库）'}。"
                    )
                    print("        进度是按题目 id 记的，题库不同就对不上号。")
                    print("        要一致：把那台机器的题库包拿到这边 `make bank-import`。")
                if result.get("strayBankRows"):
                    stray = result["strayBankRows"]
                    detail = "、".join(f"{t} {n} 行" for t, n in sorted(stray.items()))
                    print(f"  注意：包里居然有题库数据（{detail}）—— 按边界**没有导入**，请走题库包那条路。")
            return 0

        info = describe(args.pack)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=1, default=str))
        else:
            print(f"{args.pack}（{_fmt_bytes(info['bytes'])}）")
            _print_manifest(info["manifest"], print)
            if info["bankRowsInPack"]:
                detail = "、".join(f"{t} {n} 行" for t, n in sorted(info["bankRowsInPack"].items()))
                print(f"  ⚠ 包里有题库数据：{detail}（导入时会跳过）")
            print(f"  表结构指纹：{'与 manifest 一致 ✓' if info['schemaUnchanged'] else '对不上 ✗'}")
        return 0
    except PackError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
